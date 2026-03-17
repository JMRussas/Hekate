#  Odin Tools — LLM-callable tool definitions for the system overseer
#
#  10 tools: 4 observation, 6 intervention.
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
]

INTERVENTION_TOOLS: set[str] = {
    "retry_task",
    "release_task",
    "skip_task",
    "reassign_tier",
    "modify_prompt",
    "log_observation",
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
            resp = await client.get("http://localhost:11434/api/tags")
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


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_EXECUTORS: dict[str, callable] = {
    "get_system_status": _get_system_status,
    "get_project_detail": _get_project_detail,
    "get_task_detail": _get_task_detail,
    "get_resource_health": _get_resource_health,
    "retry_task": _retry_task,
    "release_task": _release_task,
    "skip_task": _skip_task,
    "reassign_tier": _reassign_tier,
    "modify_prompt": _modify_prompt,
    "log_observation": _log_observation,
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
