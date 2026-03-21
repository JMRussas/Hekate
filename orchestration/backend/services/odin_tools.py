#  Odin Tools — LLM-callable tool definitions for the system overseer
#
#  22 tools: 7 observation, 7 intervention, 3 learning, 3 lifecycle, 2 mcp.
#  Each tool has an OpenAI function-calling schema (for qwen3.5 via Ollama)
#  and an async executor that operates on the DB directly.
#
#  Depends on: backend/db/connection.py, backend/services/sentinel/bus.py
#  Used by:    Odin overseer loop

from __future__ import annotations

import json
import logging
import time
import uuid

from backend.db.connection import Database
from backend.services.sentinel.bus import SentinelBus
from backend.services.sentinel.models import SentinelMessage

logger = logging.getLogger(__name__)

VALID_TIERS = {"claude_code", "gemini_cli", "ollama"}

# ---------------------------------------------------------------------------
# OpenAI function-calling schemas
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    # --- Observation tools ---
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": "Get status of all projects with task counts by status, plus resource health summary.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_detail",
            "description": "Get all tasks for a project grouped by wave and status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "The project ID to inspect.",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_detail",
            "description": "Get full details for a single task including error, retry count, timing, and model tier.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to inspect.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_health",
            "description": "Check availability of Ollama, context store, and Claude CLI.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_provider_status",
            "description": "Check availability and quota status of all LLM providers (Claude, Gemini, Ollama).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_god_health",
            "description": "Check heartbeat status of all registered gods (Odin, Huginn, etc). Shows uptime, last heartbeat, and dependency health.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # --- Intervention tools ---
    {
        "type": "function",
        "function": {
            "name": "retry_task",
            "description": "Reset a task to pending status, clear its error, and increment retry count.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to retry.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this task should be retried.",
                    },
                },
                "required": ["task_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "release_task",
            "description": "Clear a task's claim and set it back to pending so another worker can pick it up.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to release.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this task's claim should be released.",
                    },
                },
                "required": ["task_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_task",
            "description": "Cancel a task so the project can proceed past it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to skip/cancel.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this task should be skipped.",
                    },
                },
                "required": ["task_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reassign_tier",
            "description": "Change a task's model tier and reset it to pending. Use when a tier is failing repeatedly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to reassign.",
                    },
                    "new_tier": {
                        "type": "string",
                        "enum": ["claude_code", "gemini_cli", "ollama"],
                        "description": "The new model tier to assign.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the tier should be changed.",
                    },
                },
                "required": ["task_id", "new_tier", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "modify_prompt",
            "description": "Append guidance text to a task's system prompt and reset it to pending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task ID to modify.",
                    },
                    "guidance": {
                        "type": "string",
                        "description": "Guidance text to append to the task's system prompt.",
                    },
                },
                "required": ["task_id", "guidance"],
            },
        },
    },
    # --- Learning & iteration tools ---
    {
        "type": "function",
        "function": {
            "name": "review_completed_project",
            "description": "Review a completed project: what was built, what failed, what was learned. Use this to decide what to improve next.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The completed project ID to review."},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_knowledge",
            "description": "Get accumulated knowledge and learnings from a project — decisions, gotchas, patterns discovered during execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID to get knowledge for."},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_completions",
            "description": "List recently completed projects that haven't been reviewed yet. Use this to find what to iterate on.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # --- Project lifecycle tools ---
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": "Create a new orchestration project with requirements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short project name."},
                    "requirements": {"type": "string", "description": "What the project should accomplish. Be specific."},
                    "repo_path": {"type": "string", "description": "Git repo path for the project. Use C:/Users/jruss/Documents/GitHub/Hekate for Hekate work, or omit to use the default workspace D:/Conversations/Odin/."},
                },
                "required": ["name", "requirements"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_project",
            "description": "Generate a task plan for a draft project using Claude. The project must be in 'draft' status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID to plan."},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_project",
            "description": "Start executing a planned project. Decomposes the plan into tasks and begins wave dispatch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID to start."},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_observation",
            "description": "Record an observation about project health or system state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "The project this observation relates to.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Brief summary of the observation.",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["info", "warning", "critical"],
                        "description": "Severity level of the observation.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category tag (e.g. stall, failure, resource, progress).",
                    },
                },
                "required": ["project_id", "summary", "severity", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_task",
            "description": "Dispatch a pending task for execution. Sets it to queued status so the executor picks it up.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "Task ID to dispatch.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this task should be dispatched now.",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wave_readiness",
            "description": "Check which tasks in a project are ready for dispatch (pending, dependencies met, in current wave).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project ID to check.",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advance_wave",
            "description": "Check if the current wave is complete and advance to the next wave. Unblocks pending tasks in the next wave.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project ID to advance.",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    # --- MCP server management tools ---
    {
        "type": "function",
        "function": {
            "name": "spawn_mcp_server",
            "description": "Spawn an MCP server subprocess for task execution. Returns available tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Unique session name.",
                    },
                    "command": {
                        "type": "string",
                        "description": "Command to run (e.g., 'node server.js' or 'python server.py').",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the server process.",
                    },
                },
                "required": ["name", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_mcp_sessions",
            "description": "List active MCP server sessions.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

INTERVENTION_TOOLS: set[str] = {
    "retry_task",
    "release_task",
    "skip_task",
    "reassign_tier",
    "modify_prompt",
    "log_observation",
    "dispatch_task",
    "advance_wave",
    "create_project",
    "plan_project",
    "start_project",
    "spawn_mcp_server",
}

# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------


async def _get_system_status(db: Database, bus: SentinelBus, args: dict) -> str:
    projects = await db.fetchall(
        "SELECT id, name, status FROM projects ORDER BY created_at DESC", ()
    )
    if not projects:
        return "No projects found."

    lines = []
    for p in projects:
        pid = p["id"]
        counts = await db.fetchall(
            "SELECT status, COUNT(*) as cnt FROM tasks "
            "WHERE project_id = $1 GROUP BY status",
            (pid,),
        )
        status_map = {r["status"]: r["cnt"] for r in counts}
        total = sum(status_map.values())
        summary = ", ".join(f"{s}={c}" for s, c in sorted(status_map.items()))
        lines.append(
            f"- {p['name']} [{p['status']}] ({total} tasks: {summary})"
        )

    return "Projects:\n" + "\n".join(lines)


async def _get_project_detail(db: Database, bus: SentinelBus, args: dict) -> str:
    project_id = args.get("project_id")
    if not project_id:
        return "Error: project_id is required."

    project = await db.fetchone(
        "SELECT id, name, status FROM projects WHERE id = $1", (project_id,)
    )
    if not project:
        return f"Error: project {project_id} not found."

    tasks = await db.fetchall(
        "SELECT id, title, status, wave, model_tier, error, retry_count "
        "FROM tasks WHERE project_id = $1 ORDER BY wave, status",
        (project_id,),
    )
    if not tasks:
        return f"Project '{project['name']}' has no tasks."

    # Group by wave
    waves: dict[int, list] = {}
    for t in tasks:
        w = t["wave"]
        waves.setdefault(w, []).append(t)

    lines = [f"Project: {project['name']} [{project['status']}]"]
    for wave_num in sorted(waves):
        lines.append(f"\nWave {wave_num}:")
        for t in waves[wave_num]:
            err = f" ERROR: {t['error'][:80]}" if t["error"] else ""
            lines.append(
                f"  [{t['status']}] {t['title']} "
                f"(tier={t['model_tier']}, retries={t['retry_count']}{err})"
            )

    return "\n".join(lines)


async def _get_task_detail(db: Database, bus: SentinelBus, args: dict) -> str:
    task_id = args.get("task_id")
    if not task_id:
        return "Error: task_id is required."

    task = await db.fetchone(
        "SELECT id, title, status, wave, model_tier, model_used, "
        "error, retry_count, max_retries, system_prompt, "
        "started_at, completed_at, created_at, claimed_by, claimed_at "
        "FROM tasks WHERE id = $1",
        (task_id,),
    )
    if not task:
        return f"Error: task {task_id} not found."

    parts = [
        f"Task: {task['title']}",
        f"  ID: {task['id']}",
        f"  Status: {task['status']}",
        f"  Wave: {task['wave']}",
        f"  Model tier: {task['model_tier']}",
        f"  Model used: {task['model_used'] or 'none'}",
        f"  Retries: {task['retry_count']}/{task['max_retries']}",
        f"  Claimed by: {task['claimed_by'] or 'none'}",
    ]

    if task["error"]:
        parts.append(f"  Error: {task['error'][:500]}")

    if task["started_at"] and task["completed_at"]:
        duration = task["completed_at"] - task["started_at"]
        parts.append(f"  Duration: {duration:.1f}s")
    elif task["started_at"]:
        elapsed = time.time() - task["started_at"]
        parts.append(f"  Running for: {elapsed:.1f}s")

    return "\n".join(parts)


async def _get_resource_health(db: Database, bus: SentinelBus, args: dict) -> str:
    import httpx

    checks = []

    # Ollama
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            from backend.config import OLLAMA_URL
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            if resp.status_code == 200:
                data = resp.json()
                model_count = len(data.get("models", []))
                checks.append(f"Ollama: UP ({model_count} models)")
            else:
                checks.append(f"Ollama: ERROR (status {resp.status_code})")
    except Exception as exc:
        checks.append(f"Ollama: DOWN ({type(exc).__name__})")

    # Context store
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get("http://localhost:5102/health")
            if resp.status_code == 200:
                checks.append("Context Store: UP")
            else:
                checks.append(f"Context Store: ERROR (status {resp.status_code})")
    except Exception as exc:
        checks.append(f"Context Store: DOWN ({type(exc).__name__})")

    # Claude CLI
    import asyncio
    import shutil

    claude_path = shutil.which("claude")
    if claude_path:
        try:
            proc = await asyncio.create_subprocess_exec(
                claude_path, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode == 0:
                version = stdout.decode().strip()[:50]
                checks.append(f"Claude CLI: AVAILABLE ({version})")
            else:
                checks.append(f"Claude CLI: ERROR (exit code {proc.returncode})")
        except asyncio.TimeoutError:
            checks.append("Claude CLI: TIMEOUT")
        except Exception as exc:
            checks.append(f"Claude CLI: ERROR ({type(exc).__name__})")
    else:
        checks.append("Claude CLI: NOT FOUND on PATH")

    return "Resource Health:\n" + "\n".join(f"  {c}" for c in checks)


async def _get_provider_status(db: Database, bus: SentinelBus, args: dict) -> str:
    import httpx
    import os

    from backend.config import LLM_GATEWAY_URL
    gateway_url = LLM_GATEWAY_URL
    lines = ["Provider Status (via LLM Gateway):"]

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{gateway_url}/providers")
            if resp.status_code == 200:
                providers = resp.json()
                for name, info in providers.items():
                    available = info.get("available", False) if isinstance(info, dict) else False
                    status = "AVAILABLE" if available else "UNAVAILABLE"
                    lines.append(f"  {name}: {status}")
            else:
                lines.append(f"  LLM Gateway: ERROR (status {resp.status_code})")
    except Exception as exc:
        lines.append(f"  LLM Gateway: UNREACHABLE ({type(exc).__name__}: {exc})")

    return "\n".join(lines)


async def _get_god_health(db: Database, bus: SentinelBus, args: dict) -> str:
    try:
        rows = await db.fetchall(
            "SELECT god_name, payload, created_at FROM god_events "
            "WHERE event_type = 'heartbeat' "
            "ORDER BY created_at DESC LIMIT 50",
            (),
        )
    except Exception as exc:
        return f"Could not query god_events table: {type(exc).__name__}: {exc}"

    if not rows:
        return "No god heartbeats found. No gods are running."

    gods = {}
    for r in rows:
        name = r["god_name"]
        if name not in gods:
            payload = r["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload) if payload else {}
            elif payload is None:
                payload = {}
            deps = payload.get("dependencies", {})
            dep_summary = ", ".join(
                f"{d}={'OK' if v.get('healthy') else 'DOWN'}"
                for d, v in deps.items()
            ) if deps else "none"
            gods[name] = {
                "last_heartbeat": str(r["created_at"]),
                "uptime_s": payload.get("uptime_s", 0),
                "deps": dep_summary,
            }

    lines = ["God Health:"]
    for name, info in gods.items():
        lines.append(
            f"  {name}: uptime={info['uptime_s']:.0f}s "
            f"last_beat={info['last_heartbeat']} deps=[{info['deps']}]"
        )

    return "\n".join(lines)


async def _retry_task(db: Database, bus: SentinelBus, args: dict) -> str:
    task_id = args.get("task_id")
    reason = args.get("reason", "")
    if not task_id:
        return "Error: task_id is required."

    task = await db.fetchone(
        "SELECT id, status, retry_count FROM tasks WHERE id = $1", (task_id,)
    )
    if not task:
        return f"Error: task {task_id} not found."

    now = time.time()
    await db.execute_write(
        "UPDATE tasks SET status = 'pending', error = NULL, "
        "retry_count = retry_count + 1, updated_at = $1 "
        "WHERE id = $2",
        (now, task_id),
    )

    await bus.publish(SentinelMessage(
        topic="state_change",
        source="odin",
        payload={
            "type": "task_retried",
            "task_id": task_id,
            "reason": reason,
        },
    ))

    return f"Task {task_id[:8]} reset to pending (retry_count incremented). Reason: {reason}"


async def _release_task(db: Database, bus: SentinelBus, args: dict) -> str:
    task_id = args.get("task_id")
    reason = args.get("reason", "")
    if not task_id:
        return "Error: task_id is required."

    task = await db.fetchone(
        "SELECT id, status, claimed_by FROM tasks WHERE id = $1", (task_id,)
    )
    if not task:
        return f"Error: task {task_id} not found."
    if not task["claimed_by"]:
        return f"Task {task_id[:8]} is not claimed by anyone."

    now = time.time()
    await db.execute_write(
        "UPDATE tasks SET claimed_by = NULL, claimed_at = NULL, "
        "status = 'pending', updated_at = $1 "
        "WHERE id = $2",
        (now, task_id),
    )

    await bus.publish(SentinelMessage(
        topic="state_change",
        source="odin",
        payload={
            "type": "task_released",
            "task_id": task_id,
            "reason": reason,
        },
    ))

    return f"Task {task_id[:8]} claim released, status set to pending. Reason: {reason}"


async def _skip_task(db: Database, bus: SentinelBus, args: dict) -> str:
    task_id = args.get("task_id")
    reason = args.get("reason", "")
    if not task_id:
        return "Error: task_id is required."

    task = await db.fetchone(
        "SELECT id, status, project_id FROM tasks WHERE id = $1", (task_id,)
    )
    if not task:
        return f"Error: task {task_id} not found."

    now = time.time()
    await db.execute_write(
        "UPDATE tasks SET status = 'cancelled', error = $1, updated_at = $2 "
        "WHERE id = $3",
        (f"Skipped by Odin: {reason}", now, task_id),
    )

    await bus.publish(SentinelMessage(
        topic="state_change",
        source="odin",
        payload={
            "type": "task_skipped",
            "task_id": task_id,
            "project_id": task["project_id"],
            "reason": reason,
        },
    ))

    return f"Task {task_id[:8]} cancelled. Reason: {reason}"


async def _reassign_tier(db: Database, bus: SentinelBus, args: dict) -> str:
    task_id = args.get("task_id")
    new_tier = args.get("new_tier")
    reason = args.get("reason", "")
    if not task_id:
        return "Error: task_id is required."
    if not new_tier or new_tier not in VALID_TIERS:
        return f"Error: new_tier must be one of {sorted(VALID_TIERS)}."

    task = await db.fetchone(
        "SELECT id, model_tier, status FROM tasks WHERE id = $1", (task_id,)
    )
    if not task:
        return f"Error: task {task_id} not found."

    old_tier = task["model_tier"]
    now = time.time()
    await db.execute_write(
        "UPDATE tasks SET model_tier = $1, status = 'pending', "
        "error = NULL, updated_at = $2 "
        "WHERE id = $3",
        (new_tier, now, task_id),
    )

    await bus.publish(SentinelMessage(
        topic="state_change",
        source="odin",
        payload={
            "type": "tier_reassigned",
            "task_id": task_id,
            "old_tier": old_tier,
            "new_tier": new_tier,
            "reason": reason,
        },
    ))

    return f"Task {task_id[:8]} reassigned from {old_tier} to {new_tier}, reset to pending. Reason: {reason}"


async def _modify_prompt(db: Database, bus: SentinelBus, args: dict) -> str:
    task_id = args.get("task_id")
    guidance = args.get("guidance")
    if not task_id:
        return "Error: task_id is required."
    if not guidance:
        return "Error: guidance is required."

    task = await db.fetchone(
        "SELECT id, system_prompt FROM tasks WHERE id = $1", (task_id,)
    )
    if not task:
        return f"Error: task {task_id} not found."

    current_prompt = task["system_prompt"] or ""
    updated_prompt = f"{current_prompt}\n\n# Odin Guidance\n{guidance}".strip()
    now = time.time()

    await db.execute_write(
        "UPDATE tasks SET system_prompt = $1, status = 'pending', "
        "error = NULL, updated_at = $2 "
        "WHERE id = $3",
        (updated_prompt, now, task_id),
    )

    await bus.publish(SentinelMessage(
        topic="state_change",
        source="odin",
        payload={
            "type": "prompt_modified",
            "task_id": task_id,
            "guidance_length": len(guidance),
        },
    ))

    return f"Task {task_id[:8]} prompt updated and reset to pending. Added {len(guidance)} chars of guidance."


async def _log_observation(db: Database, bus: SentinelBus, args: dict) -> str:
    project_id = args.get("project_id")
    summary = args.get("summary")
    severity = args.get("severity", "info")
    category = args.get("category", "general")

    if not project_id or not summary:
        return "Error: project_id and summary are required."
    if severity not in ("info", "warning", "critical"):
        return "Error: severity must be info, warning, or critical."

    obs_id = str(uuid.uuid4())
    now = time.time()

    await db.execute_write(
        "INSERT INTO sentinel_observations "
        "(id, project_id, rule, severity, summary, details_json, timestamp) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        (obs_id, project_id, f"odin:{category}", severity, summary, "{}", now),
    )

    await bus.publish(SentinelMessage(
        topic="stall_notification",
        source="odin",
        payload={
            "type": "observation_logged",
            "observation_id": obs_id,
            "project_id": project_id,
            "severity": severity,
            "category": category,
            "summary": summary,
        },
    ))

    return f"Observation logged: [{severity}] {summary}"


async def _dispatch_task(db: Database, bus: SentinelBus, args: dict) -> str:
    task_id = args.get("task_id")
    reason = args.get("reason", "")
    if not task_id:
        return "Error: task_id is required."

    task = await db.fetchone(
        "SELECT id, title, status, model_tier, project_id FROM tasks WHERE id = $1",
        (task_id,),
    )
    if not task:
        return f"Error: task {task_id} not found."
    if task["status"] not in ("pending", "blocked", "waiting"):
        return f"Error: task is '{task['status']}', must be pending, waiting, or blocked to dispatch."

    now = time.time()
    await db.execute_write(
        "UPDATE tasks SET status = 'pending', updated_at = $1 WHERE id = $2",
        (now, task_id),
    )

    # Publish dispatch command for the executor
    await bus.publish(SentinelMessage(
        topic="dispatch_command",
        source="odin",
        payload={
            "task_id": task_id,
            "project_id": task["project_id"],
            "reason": reason,
        },
    ))

    return f"Task {task_id[:8]} ({task['title']}) set to pending for dispatch. Reason: {reason}"


async def _wave_readiness(db: Database, bus: SentinelBus, args: dict) -> str:
    project_id = args.get("project_id")
    if not project_id:
        return "Error: project_id is required."

    # Find current wave (lowest wave with incomplete tasks)
    wave_row = await db.fetchone(
        "SELECT MIN(wave) as w FROM tasks "
        "WHERE project_id = $1 AND status NOT IN ('completed', 'cancelled', 'skipped', 'needs_review')",
        (project_id,),
    )
    if not wave_row or wave_row["w"] is None:
        return f"All tasks complete for project {project_id}."

    current_wave = wave_row["w"]

    # Try dependency-aware query first (task_deps table)
    ready = None
    try:
        ready = await db.fetchall(
            "SELECT t.id, t.title, t.status, t.model_tier, t.retry_count, t.error "
            "FROM tasks t "
            "LEFT JOIN task_deps d ON d.task_id = t.id "
            "LEFT JOIN tasks dep ON dep.id = d.depends_on "
            "  AND dep.status NOT IN ('completed', 'needs_review') "
            "WHERE t.project_id = $1 AND t.status = 'pending' AND t.wave = $2 "
            "GROUP BY t.id, t.title, t.status, t.model_tier, t.retry_count, t.error "
            "HAVING COUNT(dep.id) = 0 "
            "ORDER BY t.priority ASC",
            (project_id, current_wave),
        )
    except Exception:
        # task_deps table doesn't exist — fall back to simple query
        ready = None

    if ready is None:
        ready = await db.fetchall(
            "SELECT id, title, status, model_tier, retry_count, error "
            "FROM tasks WHERE project_id = $1 AND status = 'pending' AND wave = $2 "
            "ORDER BY priority ASC",
            (project_id, current_wave),
        )

    if not ready:
        # Check if there are running tasks in the current wave
        running = await db.fetchall(
            "SELECT id, title FROM tasks "
            "WHERE project_id = $1 AND status IN ('running', 'queued') AND wave = $2",
            (project_id, current_wave),
        )
        if running:
            lines = [f"Wave {current_wave}: {len(running)} task(s) still running:"]
            for r in running:
                lines.append(f"  - {r['title']} [{r['id'][:8]}]")
            return "\n".join(lines)
        return f"Wave {current_wave}: no ready tasks (may be blocked by dependencies)."

    lines = [f"Wave {current_wave}: {len(ready)} task(s) ready for dispatch:"]
    for t in ready:
        err = f" [last error: {t['error'][:60]}]" if t.get("error") else ""
        lines.append(
            f"  - {t['title']} [{t['id'][:8]}] tier={t['model_tier']} retries={t['retry_count']}{err}"
        )
    lines.append("\nUse dispatch_task to start any of these.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Learning & iteration executors
# ---------------------------------------------------------------------------

async def _review_completed_project(db: Database, bus: SentinelBus, args: dict) -> str:
    project_id = args.get("project_id")
    if not project_id:
        return "Error: project_id is required."

    project = await db.fetchone(
        "SELECT id, name, status, requirements, completed_at, created_at FROM projects WHERE id = $1",
        (project_id,),
    )
    if not project:
        return f"Error: project {project_id} not found."

    # Task outcomes
    tasks = await db.fetchall(
        "SELECT id, title, status, model_tier, error, retry_count, verification_status, verification_notes "
        "FROM tasks WHERE project_id = $1 ORDER BY wave, title",
        (project_id,),
    )

    completed = [t for t in tasks if t["status"] == "completed"]
    failed = [t for t in tasks if t["status"] in ("failed", "cancelled")]
    total = len(tasks)

    lines = [
        f"Project: {project['name']}",
        f"Status: {project['status']}",
        f"Requirements: {project['requirements'][:300]}",
        f"Tasks: {len(completed)}/{total} completed, {len(failed)} failed/cancelled",
    ]

    if project["completed_at"] and project["created_at"]:
        duration = project["completed_at"] - project["created_at"]
        hours = duration / 3600
        lines.append(f"Duration: {hours:.1f} hours")

    # Failed tasks — what went wrong
    if failed:
        lines.append("\nFailed/Cancelled tasks:")
        for t in failed:
            err = (t["error"] or "no error recorded")[:150]
            lines.append(f"  - {t['title']} [{t['status']}] tier={t['model_tier']} retries={t['retry_count']}")
            lines.append(f"    Error: {err}")

    # Verification results
    verified = [t for t in tasks if t["verification_status"]]
    if verified:
        lines.append("\nVerification results:")
        for t in verified:
            lines.append(f"  - {t['title']}: {t['verification_status']}")
            if t["verification_notes"]:
                lines.append(f"    Notes: {t['verification_notes'][:150]}")

    # Knowledge captured
    knowledge = await db.fetchall(
        "SELECT category, content, confidence FROM project_knowledge WHERE project_id = $1",
        (project_id,),
    )
    if knowledge:
        lines.append(f"\nKnowledge captured ({len(knowledge)} items):")
        for k in knowledge[:10]:
            lines.append(f"  [{k['category']}] {k['content'][:100]}")

    lines.append("\nBased on this review, consider: What should be improved? What follow-up project would make the most impact?")
    return "\n".join(lines)


async def _get_project_knowledge(db: Database, bus: SentinelBus, args: dict) -> str:
    project_id = args.get("project_id")
    if not project_id:
        return "Error: project_id is required."

    knowledge = await db.fetchall(
        "SELECT category, content, rationale, confidence, source_task_title "
        "FROM project_knowledge WHERE project_id = $1 ORDER BY created_at DESC",
        (project_id,),
    )
    if not knowledge:
        return f"No knowledge captured for project {project_id}."

    lines = [f"Knowledge for project {project_id} ({len(knowledge)} items):"]
    for k in knowledge:
        lines.append(f"\n[{k['category']}] (confidence: {k['confidence']})")
        lines.append(f"  {k['content'][:200]}")
        if k["rationale"]:
            lines.append(f"  Rationale: {k['rationale'][:150]}")
        if k["source_task_title"]:
            lines.append(f"  From task: {k['source_task_title']}")

    return "\n".join(lines)


async def _get_recent_completions(db: Database, bus: SentinelBus, args: dict) -> str:
    # Projects completed in the last 7 days
    cutoff = time.time() - (7 * 86400)
    projects = await db.fetchall(
        "SELECT id, name, status, completed_at, requirements FROM projects "
        "WHERE status IN ($1, $2) AND completed_at > $3 "
        "ORDER BY completed_at DESC",
        ("completed", "failed", cutoff),
    )
    if not projects:
        return "No recently completed projects in the last 7 days."

    lines = ["Recently completed projects:"]
    for p in projects:
        ago = time.time() - (p["completed_at"] or 0)
        hours = ago / 3600
        task_row = await db.fetchone(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as done "
            "FROM tasks WHERE project_id = $1",
            (p["id"],),
        )
        total = task_row["total"] if task_row else 0
        done = task_row["done"] if task_row else 0
        lines.append(
            f"  - {p['name']} [{p['status']}] {done}/{total} tasks, {hours:.0f}h ago"
        )
        lines.append(f"    Requirements: {p['requirements'][:100]}")

    lines.append("\nUse review_completed_project to deep-dive into any of these.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project lifecycle executors (call orchestration API — need DI context)
# ---------------------------------------------------------------------------

async def _create_project(db: Database, bus: SentinelBus, args: dict) -> str:
    name = args.get("name")
    requirements = args.get("requirements")
    repo_path = args.get("repo_path", "D:/Conversations/Odin")
    if not name or not requirements:
        return "Error: name and requirements are required."

    project_id = uuid.uuid4().hex[:12]
    now = time.time()
    config = json.dumps({"execution_mode": "auto", "rigor": "L2"})

    await db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, config_json, repo_path, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        (project_id, name, requirements, "draft", config, repo_path, now, now),
    )

    logger.info("Odin created project: %s (%s)", name, project_id)
    return f"Project '{name}' created (id={project_id}, status=draft). Call plan_project to generate a task plan."


async def _plan_project(db: Database, bus: SentinelBus, args: dict) -> str:
    project_id = args.get("project_id")
    if not project_id:
        return "Error: project_id is required."

    project = await db.fetchone("SELECT id, name, status FROM projects WHERE id = $1", (project_id,))
    if not project:
        return f"Error: project {project_id} not found."
    if project["status"] != "draft":
        return f"Error: project is '{project['status']}', must be 'draft' to plan."

    # Use planner service directly — CLI providers are subscription-billed ($0)
    try:
        from backend.services.planner import generate_plan
        from backend.container import container
        result = await generate_plan(
            project_id,
            db=db,
            budget=container.budget(),
        )
        plan_id = result.get("plan_id", "unknown")
        task_count = result.get("task_count", 0)
        return f"Plan generated for '{project['name']}' (plan_id={plan_id}, {task_count} tasks). Call start_project to begin execution."
    except Exception as e:
        return f"Planning failed: {type(e).__name__}: {e}"


async def _start_project(db: Database, bus: SentinelBus, args: dict) -> str:
    project_id = args.get("project_id")
    if not project_id:
        return "Error: project_id is required."

    project = await db.fetchone("SELECT id, name, status FROM projects WHERE id = $1", (project_id,))
    if not project:
        return f"Error: project {project_id} not found."
    if project["status"] not in ("planned", "draft"):
        return f"Error: project is '{project['status']}', must be 'planned' to start."

    # Use decomposer directly — avoids auth, runs in-process
    try:
        from backend.container import container
        decomposer = container.decomposer()
        plan = await db.fetchone(
            "SELECT id FROM plans WHERE project_id = $1 ORDER BY version DESC LIMIT 1",
            (project_id,),
        )
        if not plan:
            return f"Error: no plan found for '{project['name']}'. Call plan_project first."

        await decomposer.decompose(project_id, plan["id"])
        await db.execute_write(
            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
            ("executing", time.time(), project_id),
        )
        return f"Project '{project['name']}' decomposed into tasks and set to executing."
    except Exception as e:
        return f"Start failed: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Wave advancement executor
# ---------------------------------------------------------------------------


async def _advance_wave(db: Database, bus: SentinelBus, args: dict) -> str:
    project_id = args.get("project_id")
    if not project_id:
        return "Error: project_id is required."

    # Find current wave (lowest wave with incomplete tasks)
    wave_row = await db.fetchone(
        "SELECT MIN(wave) as w FROM tasks "
        "WHERE project_id = $1 AND status NOT IN ('completed', 'cancelled', 'skipped', 'needs_review')",
        (project_id,),
    )
    if not wave_row or wave_row["w"] is None:
        return f"All tasks complete for project {project_id}."

    current_wave = wave_row["w"]

    # Check if current wave is done
    remaining = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM tasks "
        "WHERE project_id = $1 AND wave = $2 AND status NOT IN ('completed', 'cancelled', 'skipped', 'needs_review')",
        (project_id, current_wave),
    )
    if remaining and remaining["cnt"] > 0:
        return f"Wave {current_wave} still has {remaining['cnt']} incomplete task(s). Cannot advance."

    # Unblock next wave tasks
    next_wave_row = await db.fetchone(
        "SELECT MIN(wave) as w FROM tasks "
        "WHERE project_id = $1 AND wave > $2 AND status IN ('blocked', 'waiting')",
        (project_id, current_wave),
    )
    if not next_wave_row or next_wave_row["w"] is None:
        return f"Wave {current_wave} complete. No more waves -- project may be done."

    next_wave = next_wave_row["w"]
    now = time.time()
    await db.execute_write(
        "UPDATE tasks SET status = 'pending', updated_at = $1 "
        "WHERE project_id = $2 AND wave = $3 AND status IN ('blocked', 'waiting')",
        (now, project_id, next_wave),
    )

    await bus.publish(SentinelMessage(
        topic="state_change",
        source="odin",
        payload={
            "type": "wave_advanced",
            "project_id": project_id,
            "from_wave": current_wave,
            "to_wave": next_wave,
        },
    ))

    return f"Wave {current_wave} complete. Advanced to wave {next_wave}. Blocked tasks unblocked."


# ---------------------------------------------------------------------------
# MCP server management tools
# ---------------------------------------------------------------------------

_mcp_spawner: "McpSpawner | None" = None


def _get_spawner():
    global _mcp_spawner
    if _mcp_spawner is None:
        from backend.services.mcp_spawner import McpSpawner
        _mcp_spawner = McpSpawner()
    return _mcp_spawner


async def _spawn_mcp_server(db: Database, bus: SentinelBus, args: dict) -> str:
    name = args.get("name")
    command_str = args.get("command")
    cwd = args.get("cwd")

    if not name or not command_str:
        return "Error: name and command are required."

    import shlex
    command = shlex.split(command_str)

    spawner = _get_spawner()
    try:
        session = await spawner.spawn(name, command, cwd=cwd)
        tools = [t.get("name", "?") for t in session.tools]
        return (
            f"MCP server '{name}' spawned with {len(session.tools)} tools: "
            f"{', '.join(tools)}"
        )
    except Exception as e:
        return f"Error spawning MCP server '{name}': {type(e).__name__}: {e}"


async def _list_mcp_sessions(db: Database, bus: SentinelBus, args: dict) -> str:
    spawner = _get_spawner()
    sessions = spawner.active_sessions
    if not sessions:
        return "No active MCP sessions."
    return f"Active MCP sessions: {', '.join(sessions)}"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_EXECUTORS: dict[str, callable] = {
    "get_system_status": _get_system_status,
    "get_project_detail": _get_project_detail,
    "get_task_detail": _get_task_detail,
    "get_resource_health": _get_resource_health,
    "get_provider_status": _get_provider_status,
    "get_god_health": _get_god_health,
    "retry_task": _retry_task,
    "release_task": _release_task,
    "skip_task": _skip_task,
    "reassign_tier": _reassign_tier,
    "modify_prompt": _modify_prompt,
    "log_observation": _log_observation,
    "dispatch_task": _dispatch_task,
    "wave_readiness": _wave_readiness,
    "advance_wave": _advance_wave,
    "review_completed_project": _review_completed_project,
    "get_project_knowledge": _get_project_knowledge,
    "get_recent_completions": _get_recent_completions,
    "create_project": _create_project,
    "plan_project": _plan_project,
    "start_project": _start_project,
    "spawn_mcp_server": _spawn_mcp_server,
    "list_mcp_sessions": _list_mcp_sessions,
}


async def execute_tool(name: str, args: dict, db: Database, bus: SentinelBus) -> str:
    """Dispatch a tool call by name. Returns a string result."""
    executor = _EXECUTORS.get(name)
    if not executor:
        return f"Error: unknown tool '{name}'. Available: {sorted(_EXECUTORS.keys())}"

    try:
        result = await executor(db, bus, args)
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        result = f"Error executing {name}: {type(exc).__name__}: {exc}"

    if name in INTERVENTION_TOOLS:
        logger.info("Odin tool [%s] args=%s result=%s", name, args, result[:200])

    return result
