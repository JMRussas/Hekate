#  Orchestration Engine - MCP Server
#
#  FastMCP stdio server for Claude Code integration.
#  Provides tools for project lifecycle management and external task execution.
#
#  Config: backend/mcp/config.json (api_url, api_key, timeout)
#
#  Depends on: mcp (FastMCP), httpx
#  Used by:    Claude Code (via MCP settings)

import json
import logging
import os
import sys
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("orchestration.mcp")


def create_server(config_path: Path | None = None) -> FastMCP:
    """Create and configure the MCP server with all tools.

    Args:
        config_path: Path to config.json. Defaults to backend/mcp/config.json.

    Returns:
        Configured FastMCP server instance.
    """
    # -----------------------------------------------------------------------
    # Config
    # -----------------------------------------------------------------------
    cfg_path = config_path or Path(__file__).parent / "config.json"
    if not cfg_path.exists():
        log.error("Config not found: %s — copy config.example.json", cfg_path)
        sys.exit(1)

    with open(cfg_path, encoding="utf-8") as f:
        config = json.load(f)

    api_url = config.get("api_url", "http://localhost:5200").rstrip("/")
    api_key = config.get("api_key", "")
    timeout = config.get("timeout", 300)

    # Resolve env var references like ${BEETHOVEN_API_KEY}
    if api_key.startswith("${") and api_key.endswith("}"):
        env_name = api_key[2:-1]
        api_key = os.environ.get(env_name, "")
    if api_url.startswith("${") and api_url.endswith("}"):
        env_name = api_url[2:-1]
        api_url = os.environ.get(env_name, api_url)

    if not api_key:
        log.error("api_key is required in MCP config")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # HTTP client
    # -----------------------------------------------------------------------
    client = httpx.AsyncClient(
        base_url=api_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )

    mcp = FastMCP("orchestration")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _get(path: str, params: dict | None = None) -> dict | list:
        """GET request to the engine API."""
        resp = await client.get(f"/api{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(path: str, json_body: dict | None = None) -> dict:
        """POST request to the engine API."""
        resp = await client.post(f"/api{path}", json=json_body or {})
        resp.raise_for_status()
        return resp.json()

    def _fmt_error(e: Exception) -> str:
        """Format an error for tool output."""
        if isinstance(e, httpx.HTTPStatusError):
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = e.response.text or str(e)
            return f"Error {e.response.status_code}: {detail}"
        return f"Error: {e}"

    # -----------------------------------------------------------------------
    # Project lifecycle tools
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="create_project",
        description="Create a new project with requirements. Automatically sets hybrid execution mode.",
    )
    async def create_project(
        name: str,
        requirements: str,
        planning_rigor: str = "L2",
    ) -> str:
        """Create a project. planning_rigor: L1 (quick), L2 (standard), L3 (thorough)."""
        try:
            result = await _post("/projects", {
                "name": name,
                "requirements": requirements,
                "planning_rigor": planning_rigor,
                "config": {"execution_mode": "hybrid"},
            })
            return (
                f"--- Project Created ---\n"
                f"ID: {result['id']}\n"
                f"Name: {result['name']}\n"
                f"Status: {result['status']}\n"
                f"Planning Rigor: {planning_rigor}\n"
                f"Execution Mode: hybrid\n\n"
                f"Next: Use plan_project to generate an execution plan."
            )
        except Exception as e:
            return _fmt_error(e)

    @mcp.tool(
        name="plan_project",
        description="Generate an AI execution plan for a project. May take 15-45 seconds.",
    )
    async def plan_project(project_id: str) -> str:
        """Generate a plan. The project must be in DRAFT status."""
        try:
            result = await _post(f"/projects/{project_id}/plan")
            plan = result.get("plan", {})
            tasks = plan.get("tasks", [])
            phases = plan.get("phases", [])
            summary = plan.get("summary", "No summary")

            out = "--- Plan Generated ---\n"
            out += f"Plan ID: {result.get('plan_id', result.get('id', '?'))}\n"
            out += f"Model: {result.get('model_used', '?')}\n"
            out += f"Cost: ${result.get('cost_usd', 0):.4f}\n\n"
            out += f"Summary: {summary}\n\n"

            if phases:
                for phase in phases:
                    out += f"\n## {phase.get('name', 'Phase')}\n"
                    for t in phase.get("tasks", []):
                        out += f"  - [{t.get('model_tier', '?')}] {t.get('title', '?')}\n"
            elif tasks:
                for t in tasks:
                    out += f"  - [{t.get('model_tier', '?')}] {t.get('title', '?')}\n"

            out += "\nNext: Review the plan, then use start_project to begin execution."
            return out
        except Exception as e:
            return _fmt_error(e)

    @mcp.tool(
        name="start_project",
        description="Approve the latest plan and start execution. Combines plan approval + execute.",
    )
    async def start_project(project_id: str) -> str:
        """Approve the latest plan and start executing."""
        try:
            # Get latest plan
            plans = await _get(f"/projects/{project_id}/plans")
            if not plans:
                return "Error: No plans found. Use plan_project first."
            latest_plan = plans[-1]

            # Approve plan if still draft
            if latest_plan.get("status") == "draft":
                await _post(f"/projects/{project_id}/plans/{latest_plan['id']}/approve")

            # Start execution
            result = await _post(f"/projects/{project_id}/execute")
            return (
                f"--- Project Started ---\n"
                f"Status: {result['status']}\n\n"
                f"The project is now EXECUTING. Use next_task to claim tasks."
            )
        except Exception as e:
            return _fmt_error(e)

    @mcp.tool(
        name="list_projects",
        description="List your projects with status summaries.",
    )
    async def list_projects() -> str:
        """List all projects owned by the authenticated user."""
        try:
            projects = await _get("/projects")
            if not projects:
                return "No projects found."

            out = "--- Projects ---\n\n"
            for p in projects:
                summary = p.get("task_summary") or {}
                total = summary.get("total", 0)
                completed = summary.get("completed", 0)
                out += (
                    f"  {p['name']} ({p['id'][:12]}...)\n"
                    f"    Status: {p['status']} | Tasks: {completed}/{total}\n\n"
                )
            return out
        except Exception as e:
            return _fmt_error(e)

    @mcp.tool(
        name="project_status",
        description="Get detailed status of a project including task breakdown.",
    )
    async def project_status(project_id: str) -> str:
        """Get detailed project status."""
        try:
            p = await _get(f"/projects/{project_id}")
            summary = p.get("task_summary") or {}

            out = f"--- Project: {p['name']} ---\n"
            out += f"ID: {p['id']}\n"
            out += f"Status: {p['status']}\n"
            out += f"Tasks: {summary.get('total', 0)} total"
            for k in ("completed", "running", "pending", "failed", "blocked"):
                v = summary.get(k, 0)
                if v > 0:
                    out += f", {v} {k}"
            out += "\n"

            config = p.get("config", {})
            out += f"Execution Mode: {config.get('execution_mode', 'auto')}\n"
            out += f"Planning Rigor: {p.get('planning_rigor', 'L2')}\n"

            return out
        except Exception as e:
            return _fmt_error(e)

    # -----------------------------------------------------------------------
    # Task tools
    # -----------------------------------------------------------------------

    @mcp.tool(
        name="list_tasks",
        description="List tasks for a project, with optional status filter.",
    )
    async def list_tasks(
        project_id: str,
        status_filter: str = "",
    ) -> str:
        """List tasks. Optional status_filter: pending, running, completed, failed, etc."""
        try:
            params = {}
            if status_filter:
                params["status"] = status_filter
            tasks = await _get(f"/tasks/project/{project_id}", params=params)
            if not tasks:
                return "No tasks found."

            out = f"--- Tasks ({len(tasks)}) ---\n\n"
            for t in tasks:
                out += (
                    f"  [{t['status']:>12}] {t['title']} ({t['id'][:12]}...)\n"
                    f"               Wave {t.get('wave', 0)} | {t['model_tier']} | Priority {t['priority']}\n"
                )
            return out
        except Exception as e:
            return _fmt_error(e)

    @mcp.tool(
        name="next_task",
        description="Claim the highest-priority claimable task from a project.",
    )
    async def next_task(project_id: str) -> str:
        """Find and claim the next available task."""
        try:
            claimable = await _get(f"/external/{project_id}/claimable")
            if not claimable:
                return "No claimable tasks available. All tasks may be completed, in progress, or blocked."

            # Claim the first one (highest priority)
            task_id = claimable[0]["id"]
            result = await _post(f"/external/tasks/{task_id}/claim")

            out = "--- Task Claimed ---\n"
            out += f"ID: {result['id']}\n"
            out += f"Title: {result['title']}\n"
            out += f"Type: {result['task_type']} | Tier: {result['model_tier']}\n"
            out += f"Wave: {result['wave']} | Priority: {result['priority']}\n"
            if result.get('phase'):
                out += f"Phase: {result['phase']}\n"
            out += f"\n--- Description ---\n{result['description']}\n"

            if result.get('context'):
                out += f"\n--- Context ({len(result['context'])} entries) ---\n"
                for ctx in result['context']:
                    ctype = ctx.get('type', 'unknown')
                    if ctype == 'dependency_output':
                        out += f"  From: {ctx.get('source_task_title', '?')}\n"
                        content = ctx.get('content', '')
                        out += f"  {content[:500]}{'...' if len(content) > 500 else ''}\n\n"
                    else:
                        out += f"  [{ctype}] {json.dumps(ctx)[:200]}\n"

            if result.get('system_prompt'):
                out += f"\n--- System Prompt ---\n{result['system_prompt']}\n"

            out += "\nWhen done, use submit_result with the task output."
            return out
        except Exception as e:
            return _fmt_error(e)

    @mcp.tool(
        name="claim_task",
        description="Claim a specific task by ID for external execution.",
    )
    async def claim_task(task_id: str) -> str:
        """Claim a specific task. Must be in PENDING status."""
        try:
            result = await _post(f"/external/tasks/{task_id}/claim")

            out = "--- Task Claimed ---\n"
            out += f"ID: {result['id']}\n"
            out += f"Title: {result['title']}\n"
            out += f"Type: {result['task_type']} | Tier: {result['model_tier']}\n"
            out += f"\n--- Description ---\n{result['description']}\n"

            if result.get('context'):
                out += f"\n--- Context ({len(result['context'])} entries) ---\n"
                for ctx in result['context']:
                    ctype = ctx.get('type', 'unknown')
                    if ctype == 'dependency_output':
                        out += f"  From: {ctx.get('source_task_title', '?')}\n"
                        content = ctx.get('content', '')
                        out += f"  {content[:500]}{'...' if len(content) > 500 else ''}\n\n"

            out += "\nWhen done, use submit_result with the task output."
            return out
        except Exception as e:
            return _fmt_error(e)

    @mcp.tool(
        name="task_detail",
        description="Get full details of a task including description, context, and dependencies.",
    )
    async def task_detail(task_id: str) -> str:
        """Get full task details without claiming it."""
        try:
            t = await _get(f"/tasks/{task_id}")

            out = f"--- Task: {t['title']} ---\n"
            out += f"ID: {t['id']}\n"
            out += f"Status: {t['status']} | Type: {t['task_type']} | Tier: {t['model_tier']}\n"
            out += f"Wave: {t.get('wave', 0)} | Priority: {t['priority']}\n"
            if t.get('phase'):
                out += f"Phase: {t['phase']}\n"
            out += f"\n--- Description ---\n{t['description']}\n"

            if t.get('output_text'):
                preview = t['output_text'][:1000]
                out += f"\n--- Output ---\n{preview}{'...' if len(t['output_text']) > 1000 else ''}\n"

            if t.get('error'):
                out += f"\n--- Error ---\n{t['error']}\n"

            if t.get('verification_status'):
                out += f"\nVerification: {t['verification_status']}"
                if t.get('verification_notes'):
                    out += f" — {t['verification_notes']}"
                out += "\n"

            if t.get('depends_on'):
                out += f"\nDepends on: {', '.join(t['depends_on'])}\n"

            return out
        except Exception as e:
            return _fmt_error(e)

    @mcp.tool(
        name="submit_result",
        description="Submit the output of an externally-executed task. Triggers verification and context forwarding.",
    )
    async def submit_result(
        task_id: str,
        output_text: str,
        model_used: str = "claude-code",
    ) -> str:
        """Submit task output after external execution."""
        try:
            result = await _post(f"/external/tasks/{task_id}/result", {
                "output_text": output_text,
                "model_used": model_used,
            })

            out = "--- Result Submitted ---\n"
            out += f"Task: {result['task_id']}\n"
            out += f"Status: {result['status']}\n"

            if result.get('verification_status'):
                out += f"Verification: {result['verification_status']}\n"
                if result.get('verification_notes'):
                    out += f"Notes: {result['verification_notes']}\n"

            if result.get('next_claimable_task_id'):
                out += f"\nNext claimable task: {result['next_claimable_task_id']}\n"
                out += "Use claim_task or next_task to continue."
            else:
                out += "\nNo more claimable tasks at this time."

            return out
        except Exception as e:
            return _fmt_error(e)

    @mcp.tool(
        name="release_task",
        description="Release a claimed task back to pending without counting as a failure.",
    )
    async def release_task(task_id: str) -> str:
        """Release a claimed task. Does not increment retry count."""
        try:
            result = await _post(f"/external/tasks/{task_id}/release")
            return f"Task {result['task_id']} released back to pending."
        except Exception as e:
            return _fmt_error(e)

    return mcp


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[orchestration-mcp] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

if __name__ == "__main__":
    server = create_server()
    server.run(transport="stdio")
