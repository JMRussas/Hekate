"""Prometheus MCP — project and task management for Claude Code sessions.

Replaces mcp__orchestration__. Calls the Hekate Engine API (localhost:5200)
instead of accessing the DB directly.

Tools:
  - create_project: Create a new project with requirements
  - list_projects: List all projects with status
  - project_status: Get project detail with tasks
  - list_tasks: List tasks for a project
  - task_detail: Get full task detail
  - start_project: Trigger planning + execution
  - approve_task: Approve a needs_review task
  - retry_task: Retry a failed task
  - cancel_project: Cancel a project

Usage: Register in .mcp.json as "prometheus"
"""

import json
import logging
import os
import sys
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prometheus")

# Hekate Engine API
ENGINE_URL = os.environ.get("HEKATE_ENGINE_URL", "http://localhost:5200")
API = f"{ENGINE_URL}/api"
_HEKATE_EMAIL = os.environ.get("HEKATE_EMAIL", "admin@local.dev")
_HEKATE_PASSWORD = os.environ.get("HEKATE_PASSWORD", "")
_PORT = int(os.environ.get("MCP_PORT", "0"))

mcp = FastMCP("prometheus", port=_PORT) if _PORT else FastMCP("prometheus")

# Cached auth token
_token: str | None = None


async def _login() -> str:
    """Login and return a Bearer token. Raises on failure."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{API}/auth/login",
            json={"email": _HEKATE_EMAIL, "password": _HEKATE_PASSWORD},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"]


async def _auth_headers() -> dict:
    global _token
    if not _token:
        _token = await _login()
    return {"Authorization": f"Bearer {_token}"}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _get(path: str) -> dict | list:
    global _token
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{API}{path}", headers=headers)
        if resp.status_code == 401:
            _token = None
            headers = await _auth_headers()
            resp = await client.get(f"{API}{path}", headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, data: dict | None = None) -> dict:
    global _token
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=30.0) as client:
        if data:
            resp = await client.post(f"{API}{path}", json=data, headers=headers)
        else:
            resp = await client.post(f"{API}{path}", content=b"", headers=headers)
        if resp.status_code == 401:
            _token = None
            headers = await _auth_headers()
            if data:
                resp = await client.post(f"{API}{path}", json=data, headers=headers)
            else:
                resp = await client.post(f"{API}{path}", content=b"", headers=headers)
        resp.raise_for_status()
        return resp.json()


async def _delete(path: str) -> None:
    global _token
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.delete(f"{API}{path}", headers=headers)
        if resp.status_code == 401:
            _token = None
            headers = await _auth_headers()
            resp = await client.delete(f"{API}{path}", headers=headers)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_project(
    name: str,
    requirements: str,
    repo_path: Optional[str] = None,
    target_level: str = "auto",
    tdd: bool = True,
) -> str:
    """Create a new project. Returns project ID and status.

    Args:
        name: Project name
        requirements: What needs to be built (detailed requirements)
        repo_path: Path to the code repository (where CLI will execute)
        target_level: Planning depth — auto, L1-L5 (default: auto)
        tdd: Enable test-driven development (default: true)
    """
    result = await _post("/projects", {
        "name": name,
        "requirements": requirements,
        "repo_path": repo_path,
        "config": {
            "target_level": target_level,
            "tdd": tdd,
            "narration": True,
        },
    })
    pid = result.get("id", "?")
    return json.dumps({
        "project_id": pid,
        "name": name,
        "status": result.get("status", "draft"),
        "message": f"Project created. Use start_project('{pid}') to begin planning and execution.",
    }, indent=2)


@mcp.tool()
async def list_projects(status: Optional[str] = None) -> str:
    """List all projects, optionally filtered by status.

    Args:
        status: Filter by status (draft, planning, executing, completed, failed)
    """
    path = "/projects"
    if status:
        path += f"?status={status}"
    projects = await _get(path)
    summary = []
    for p in projects:
        summary.append({
            "id": p["id"],
            "name": p["name"],
            "status": p["status"],
        })
    return json.dumps(summary, indent=2)


@mcp.tool()
async def project_status(project_id: str) -> str:
    """Get project detail including all tasks and their status.

    Args:
        project_id: The project ID
    """
    result = await _get(f"/projects/{project_id}")
    tasks = result.get("tasks", [])

    task_summary = []
    for t in tasks:
        task_summary.append({
            "id": t["id"],
            "title": t.get("title", ""),
            "status": t.get("status", ""),
            "wave": t.get("wave", 0),
            "model_tier": t.get("model_tier", ""),
        })

    done = len([t for t in tasks if t.get("status") == "completed"])
    total = len(tasks)

    return json.dumps({
        "project_id": result["id"],
        "name": result["name"],
        "status": result["status"],
        "progress": f"{done}/{total} tasks completed",
        "tasks": task_summary,
    }, indent=2)


@mcp.tool()
async def list_tasks(project_id: str, status: Optional[str] = None) -> str:
    """List tasks for a project.

    Args:
        project_id: The project ID
        status: Filter by status (pending, running, completed, failed, needs_review, blocked)
    """
    path = f"/tasks/project/{project_id}"
    if status:
        path += f"?status={status}"
    tasks = await _get(path)
    return json.dumps(tasks, indent=2)


@mcp.tool()
async def task_detail(task_id: str) -> str:
    """Get full detail for a specific task including output.

    Args:
        task_id: The task ID
    """
    result = await _get(f"/tasks/{task_id}")
    return json.dumps({
        "id": result.get("id"),
        "title": result.get("title"),
        "description": result.get("description"),
        "status": result.get("status"),
        "wave": result.get("wave"),
        "task_type": result.get("task_type"),
        "model_tier": result.get("model_tier"),
        "retry_count": result.get("retry_count"),
        "verification_status": result.get("verification_status"),
        "output": (result.get("output_text") or "")[:2000],
        "error": result.get("error"),
    }, indent=2)


@mcp.tool()
async def start_project(project_id: str) -> str:
    """Start planning and execution for a project.

    The gods pipeline will: plan (Athena) → dispatch (Odin) →
    execute (Hermes) → verify (Mimir) → complete.

    Args:
        project_id: The project ID to start
    """
    result = await _post(f"/projects/{project_id}/execute")
    return json.dumps({
        "project_id": project_id,
        "status": result.get("status", "executing"),
        "message": "Project started. Athena is planning via Gemini. Use project_status() to monitor.",
    }, indent=2)


@mcp.tool()
async def approve_task(task_id: str, feedback: str = "") -> str:
    """Approve a task in needs_review status.

    Args:
        task_id: The task ID to approve
        feedback: Optional feedback to store
    """
    result = await _post(f"/tasks/{task_id}/review", {
        "action": "approve",
        "feedback": feedback,
    })
    return json.dumps({
        "task_id": task_id,
        "status": result.get("status", "completed"),
        "message": "Task approved. Pipeline will continue.",
    }, indent=2)


@mcp.tool()
async def retry_task(task_id: str) -> str:
    """Retry a failed or needs_review task.

    Args:
        task_id: The task ID to retry
    """
    result = await _post(f"/tasks/{task_id}/retry")
    return json.dumps({
        "task_id": task_id,
        "status": result.get("status", "pending"),
        "message": "Task reset to pending. Will be re-dispatched.",
    }, indent=2)


@mcp.tool()
async def cancel_project(project_id: str) -> str:
    """Cancel a project and all its tasks.

    Args:
        project_id: The project ID to cancel
    """
    result = await _post(f"/projects/{project_id}/cancel")
    return json.dumps({
        "project_id": project_id,
        "status": "cancelled",
        "message": "Project cancelled.",
    }, indent=2)


@mcp.tool()
async def submit_verification(
    task_id: str,
    verdict: str,
    feedback: str,
    confidence: float = 1.0,
) -> str:
    """Submit a verification verdict for a completed task.

    Call this after reading task_detail to report whether the output satisfies
    the task requirements. The pipeline will act on your verdict immediately.

    Args:
        task_id: The task ID to verify
        verdict: One of: "passed", "gaps_found", "human_needed"
            - passed: output satisfies all requirements
            - gaps_found: output has issues and should be retried with feedback
            - human_needed: output is ambiguous or requires human judgment
        feedback: Explanation of your verdict — required for gaps_found/human_needed,
                  useful for passed (summarize what was correct)
        confidence: Your confidence in the verdict, 0.0-1.0 (default: 1.0)
    """
    if verdict not in ("passed", "gaps_found", "human_needed"):
        return json.dumps({"error": f"Invalid verdict '{verdict}'. Must be: passed, gaps_found, human_needed"})

    result = await _post(f"/tasks/{task_id}/verify", {
        "verdict": verdict,
        "feedback": feedback,
        "confidence": confidence,
    })
    return json.dumps({
        "task_id": task_id,
        "verdict": verdict,
        "accepted": result.get("accepted", True),
        "message": result.get("message", "Verification submitted."),
    }, indent=2)


@mcp.tool()
async def submit_plan_children(node_id: str, children: list) -> str:
    """Submit child plan nodes for a planning node you are deepening.

    Call this when you have finished generating the children for a node.
    The engine will save the children and continue planning recursively.

    Args:
        node_id: The plan node ID you are deepening (provided in your prompt)
        children: Array of child node dicts. Each must have at minimum:
            - title: str
            - task_type: str (code|research|test|docs)
            - description: str
            Additional fields (affected_files, depends_on_indices, complexity,
            implementation_notes, test_strategy, changes, etc.) are welcomed
            and stored for deeper planning levels.
    """
    result = await _post(f"/internal/plan-nodes/{node_id}/children", {"children": children})
    return json.dumps({
        "node_id": node_id,
        "saved": result.get("saved", 0),
        "node_ids": result.get("node_ids", []),
        "message": f"Saved {result.get('saved', 0)} children. Planning will continue recursively.",
    }, indent=2)


@mcp.tool()
async def get_events(project_id: str, limit: int = 20) -> str:
    """Get recent events for a project (narration, dispatch, verification, etc).

    Args:
        project_id: The project ID
        limit: Max events to return (default: 20)
    """
    events = await _get(f"/events/{project_id}?limit={limit}")
    summary = []
    for e in events:
        summary.append({
            "type": e.get("event_type", ""),
            "source": e.get("source", ""),
        })
    return json.dumps(summary, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if _PORT:
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
