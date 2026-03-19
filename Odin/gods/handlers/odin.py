"""Odin handler — orchestration, dispatch, lifecycle, diagnosis.

Handlers:
  - odin_start: project_planned → set executing → emit project_started
  - odin_dispatch: find ready tasks → select providers → emit dispatch_commands
  - odin_lifecycle: task_verified → check wave/project completion, deadlock
  - odin_tick: periodic → scan all executing projects → emit per-project ticks
  - odin_handle_diagnosis: task_diagnosis → apply fix (retry/reassign/skip/escalate)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from gods.pipeline import Event, Emit

logger = logging.getLogger("gods.handlers.odin")

# Terminal statuses — needs_review is NOT terminal (task still needs work)
_TERMINAL = ("completed", "failed", "cancelled")

# Statuses that satisfy dependency requirements
_DEP_SATISFIED = ("completed",)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_provider_availability(
    gateway_url: str = "http://localhost:5210",
) -> dict[str, bool]:
    """Query LLM Gateway for provider status."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{gateway_url}/providers")
            resp.raise_for_status()
            data = resp.json()
            return {
                "claude_code": data.get("claude", {}).get("available", False),
                "gemini_cli": data.get("gemini", {}).get("available", False),
                "ollama": data.get("ollama", {}).get("available", False),
            }
    except Exception as e:
        logger.warning("Provider availability check failed: %s", e)
        return {"claude_code": True, "gemini_cli": True, "ollama": True}


async def _diagnose_failed_tasks(
    db, project_id: str, available: dict[str, bool],
) -> list[Emit]:
    """Find failed tasks and produce diagnosis events."""
    from gods.odin.dispatch import diagnose_failure

    emits: list[Emit] = []
    failed_tasks = await db.fetchall(
        "SELECT id, error, retry_count, max_retries, model_tier "
        "FROM tasks WHERE project_id = $1 AND status = $2",
        (project_id, "failed"),
    )
    for ft in failed_tasks:
        if isinstance(ft, dict):
            tid, error, retries, max_r, tier = (
                ft["id"], ft["error"], ft["retry_count"],
                ft["max_retries"], ft["model_tier"],
            )
        else:
            tid, error, retries, max_r, tier = ft

        diag = diagnose_failure(
            task_id=tid,
            error=error or "",
            retry_count=retries or 0,
            max_retries=max_r or 3,
            model_tier=tier or "claude_code",
            available_providers=available,
        )
        emits.append(Emit("task_diagnosis", {
            "task_id": tid,
            "project_id": project_id,
            "fix_type": diag.fix_type,
            "confidence": diag.confidence,
            "root_cause": diag.root_cause,
            "why_chain": diag.why_chain,
            "new_tier": diag.new_tier,
        }, source="odin"))

    return emits


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

_TIER_MAP: dict[tuple[str, str], str] = {
    ("code", "simple"): "claude_code",
    ("code", "medium"): "claude_code",
    ("code", "complex"): "claude_code",
    ("research", "simple"): "gemini_cli",
    ("research", "medium"): "gemini_cli",
    ("research", "complex"): "claude_code",
    ("analysis", "simple"): "gemini_cli",
    ("analysis", "medium"): "claude_code",
    ("analysis", "complex"): "claude_code",
    ("integration", "simple"): "gemini_cli",
    ("integration", "medium"): "claude_code",
    ("integration", "complex"): "claude_code",
    ("documentation", "simple"): "gemini_cli",
    ("documentation", "medium"): "claude_code",
    ("asset", "simple"): "ollama",
    ("asset", "medium"): "ollama",
}

_FALLBACK = ["claude_code", "gemini_cli", "ollama"]


def _select_provider(
    task_type: str,
    complexity: str,
    available: dict[str, bool],
) -> str:
    """Pick a provider for a task based on type + complexity + availability."""
    preferred = _TIER_MAP.get((task_type, complexity), "claude_code")
    if available.get(preferred, False):
        return preferred
    for fallback in _FALLBACK:
        if available.get(fallback, False):
            return fallback
    return "claude_code"  # last resort


# ---------------------------------------------------------------------------
# odin_start — project_planned → executing (idempotent)
# ---------------------------------------------------------------------------

async def odin_start(event: Event, db) -> list[Emit] | None:
    """Handle project_planned → set project to executing → emit project_started.

    Idempotent: if project is already executing, returns no-op.
    """
    project_id = event.payload.get("project_id")
    if not project_id:
        return [Emit("odin_error", {"error": "No project_id"}, source="odin")]

    row = await db.fetchone(
        "SELECT id, status FROM projects WHERE id = $1", (project_id,)
    )
    if not row:
        return [Emit("odin_error", {
            "error": f"Project {project_id} not found",
            "project_id": project_id,
        }, source="odin")]

    current_status = row[1] if isinstance(row, (list, tuple)) else row["status"]

    # Idempotency: if already executing or beyond, skip
    if current_status in ("executing", "completed", "failed"):
        logger.info("Odin: project %s already %s, skipping start",
                     project_id[:8], current_status)
        return []

    await db.execute_write(
        "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
        ("executing", time.time(), project_id),
    )

    logger.info("Odin: project %s → executing", project_id[:8])

    return [Emit("project_started", {
        "project_id": project_id,
        "plan_id": event.payload.get("plan_id"),
    }, source="odin")]


# ---------------------------------------------------------------------------
# odin_tick — periodic scan of all executing projects
# ---------------------------------------------------------------------------

async def odin_tick(event: Event, db) -> list[Emit] | None:
    """Scan all executing projects and emit a per-project tick for each."""
    rows = await db.fetchall(
        "SELECT id FROM projects WHERE status = $1",
        ("executing",),
    )
    if not rows:
        return None

    emits: list[Emit] = []
    for row in rows:
        pid = row[0] if isinstance(row, (list, tuple)) else row["id"]
        emits.append(Emit("project_tick", {
            "project_id": pid,
        }, source="odin"))

    return emits or None


# ---------------------------------------------------------------------------
# odin_dispatch — find ready tasks → emit dispatch_commands
# ---------------------------------------------------------------------------

async def odin_dispatch(event: Event, db) -> list[Emit] | None:
    """Find ready tasks in current wave, select providers, emit dispatch_commands.

    Also diagnoses failed tasks and emits task_diagnosis events.
    Reads max_concurrent from event payload (default 4).
    """
    project_id = event.payload.get("project_id")
    if not project_id:
        return None

    max_concurrent = event.payload.get("max_concurrent", 4)
    available = await _get_provider_availability()

    emits: list[Emit] = []

    # Diagnose failed tasks
    diag_emits = await _diagnose_failed_tasks(db, project_id, available)
    emits.extend(diag_emits)

    # Find current wave (lowest wave with non-terminal, non-needs_review tasks)
    wave_row = await db.fetchone(
        "SELECT MIN(wave) FROM tasks "
        "WHERE project_id = $1 AND status NOT IN ($2, $3, $4)",
        (project_id, *_TERMINAL),
    )
    current_wave = wave_row[0] if wave_row and wave_row[0] is not None else None
    if current_wave is None:
        return emits or None

    # Find ready tasks: pending, in current wave, all deps completed
    # Note: real DB may not have 'complexity' column — use COALESCE
    ready = await db.fetchall(
        "SELECT t.id, t.task_type, t.priority "
        "FROM tasks t "
        "LEFT JOIN task_deps d ON d.task_id = t.id "
        "LEFT JOIN tasks dep ON dep.id = d.depends_on "
        "  AND dep.status != $1 "
        "WHERE t.project_id = $2 AND t.status = $3 AND t.wave = $4 "
        "GROUP BY t.id HAVING COUNT(dep.id) = 0 "
        "ORDER BY t.priority ASC",
        ("completed", project_id, "pending", current_wave),
    )

    # Count running for concurrency limits
    running_row = await db.fetchone(
        "SELECT COUNT(*) FROM tasks WHERE project_id = $1 AND status IN ($2, $3)",
        (project_id, "running", "queued"),
    )
    running_count = running_row[0] if running_row else 0

    # Compute parallelism with max_concurrent from event
    from gods.odin.dispatch import compute_wave_parallelism
    to_dispatch = compute_wave_parallelism(
        ready_task_count=len(ready),
        running_task_count=running_count,
        available_providers=available,
        max_concurrent=max_concurrent,
    )

    for task_row in ready[:to_dispatch]:
        if isinstance(task_row, dict):
            tid = task_row["id"]
            ttype = task_row.get("task_type", "code")
        else:
            tid = task_row[0]
            ttype = task_row[1] if len(task_row) > 1 else "code"

        # Default complexity to medium — real DB may not have this column
        complexity = "medium"
        provider = _select_provider(ttype, complexity, available)

        emits.append(Emit("dispatch_command", {
            "task_id": tid,
            "project_id": project_id,
            "provider": provider,
        }, source="odin"))

    return emits or None


# ---------------------------------------------------------------------------
# odin_lifecycle — task_verified → wave/project completion
# ---------------------------------------------------------------------------

async def odin_lifecycle(event: Event, db) -> list[Emit] | None:
    """Check wave/project completion after a task is verified."""
    project_id = event.payload.get("project_id")
    if not project_id:
        return None

    emits: list[Emit] = []

    # Count remaining non-terminal tasks (needs_review counts as non-terminal)
    remaining = await db.fetchone(
        "SELECT COUNT(*) FROM tasks WHERE project_id = $1 AND status NOT IN ($2, $3, $4)",
        (project_id, *_TERMINAL),
    )
    remaining_count = remaining[0] if remaining else 0

    if remaining_count == 0:
        # All tasks are terminal — check for failures
        failed = await db.fetchone(
            "SELECT COUNT(*) FROM tasks WHERE project_id = $1 AND status = $2",
            (project_id, "failed"),
        )
        failed_count = failed[0] if failed else 0

        if failed_count > 0:
            await db.execute_write(
                "UPDATE projects SET status = $1, completed_at = $2, updated_at = $3 WHERE id = $4",
                ("failed", time.time(), time.time(), project_id),
            )
            emits.append(Emit("project_failed", {
                "project_id": project_id,
                "reason": f"{failed_count} task(s) failed",
            }, source="odin"))
        else:
            await db.execute_write(
                "UPDATE projects SET status = $1, completed_at = $2, updated_at = $3 WHERE id = $4",
                ("completed", time.time(), time.time(), project_id),
            )
            emits.append(Emit("project_complete", {
                "project_id": project_id,
            }, source="odin"))

        return emits

    # Check for deadlock: no pending/running/queued, but some blocked or needs_review
    active = await db.fetchone(
        "SELECT COUNT(*) FROM tasks WHERE project_id = $1 AND status IN ($2, $3, $4)",
        (project_id, "pending", "queued", "running"),
    )
    active_count = active[0] if active else 0

    if active_count == 0:
        blocked = await db.fetchone(
            "SELECT COUNT(*) FROM tasks WHERE project_id = $1 AND status = $2",
            (project_id, "blocked"),
        )
        blocked_count = blocked[0] if blocked else 0
        if blocked_count > 0:
            await db.execute_write(
                "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
                ("failed", time.time(), project_id),
            )
            emits.append(Emit("project_failed", {
                "project_id": project_id,
                "reason": f"Deadlock: {blocked_count} task(s) blocked, no forward progress",
            }, source="odin"))
            return emits

    # Check wave completion for the verified task's wave
    task_id = event.payload.get("task_id")
    if task_id:
        task_row = await db.fetchone(
            "SELECT wave FROM tasks WHERE id = $1", (task_id,)
        )
        if task_row:
            verified_wave = task_row[0] if isinstance(task_row, (list, tuple)) else task_row["wave"]

            # Count non-terminal tasks remaining in this wave
            wave_remaining = await db.fetchone(
                "SELECT COUNT(*) FROM tasks WHERE project_id = $1 AND wave = $2 "
                "AND status NOT IN ($3, $4, $5)",
                (project_id, verified_wave, *_TERMINAL),
            )
            if wave_remaining and wave_remaining[0] == 0:
                emits.append(Emit("wave_complete", {
                    "project_id": project_id,
                    "wave": verified_wave,
                }, source="odin"))

                # Also emit a project_tick to trigger dispatch for the next wave
                emits.append(Emit("project_tick", {
                    "project_id": project_id,
                }, source="odin"))

    return emits or None


# ---------------------------------------------------------------------------
# odin_handle_diagnosis — task_diagnosis → apply fix
# ---------------------------------------------------------------------------

async def odin_handle_diagnosis(event: Event, db) -> list[Emit] | None:
    """Consume a task_diagnosis event and apply the recommended fix.

    Fix types:
      - retry_as_is: reset task to pending, increment retry_count
      - reassign_tier: change model_tier, reset to pending
      - modify_prompt: store prompt guidance, reset to pending
      - skip: mark task cancelled
      - escalate: mark task needs_review, emit needs_human_review
    """
    task_id = event.payload.get("task_id")
    project_id = event.payload.get("project_id")
    fix_type = event.payload.get("fix_type")

    if not task_id or not fix_type:
        return [Emit("odin_error", {"error": "Missing task_id or fix_type"}, source="odin")]

    # Get current task state
    row = await db.fetchone(
        "SELECT status, retry_count, max_retries FROM tasks WHERE id = $1",
        (task_id,),
    )
    if not row:
        return [Emit("odin_error", {"error": f"Task {task_id} not found"}, source="odin")]

    if isinstance(row, dict):
        retry_count = row["retry_count"] or 0
        max_retries = row["max_retries"] or 3
    else:
        retry_count = row[1] or 0
        max_retries = row[2] or 3

    emits: list[Emit] = []

    if fix_type == "retry_as_is":
        # Check max retries
        if retry_count >= max_retries:
            logger.info("Task %s exhausted retries (%d/%d), escalating",
                        task_id[:8], retry_count, max_retries)
            await db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                ("needs_review", time.time(), task_id),
            )
            emits.append(Emit("needs_human_review", {
                "task_id": task_id,
                "project_id": project_id,
                "reason": f"Max retries exhausted ({retry_count}/{max_retries})",
            }, source="odin"))
        else:
            await db.execute_write(
                "UPDATE tasks SET status = $1, retry_count = $2, error = NULL, updated_at = $3 WHERE id = $4",
                ("pending", retry_count + 1, time.time(), task_id),
            )
            emits.append(Emit("task_reset", {
                "task_id": task_id,
                "project_id": project_id,
                "retry_count": retry_count + 1,
            }, source="odin"))

    elif fix_type == "reassign_tier":
        new_tier = event.payload.get("new_tier", "claude_code")
        await db.execute_write(
            "UPDATE tasks SET status = $1, model_tier = $2, retry_count = $3, "
            "error = NULL, updated_at = $4 WHERE id = $5",
            ("pending", new_tier, retry_count + 1, time.time(), task_id),
        )
        emits.append(Emit("task_reset", {
            "task_id": task_id,
            "project_id": project_id,
            "new_tier": new_tier,
            "retry_count": retry_count + 1,
        }, source="odin"))

    elif fix_type == "modify_prompt":
        guidance = event.payload.get("prompt_guidance", "")
        # Read existing context_json, merge guidance into it
        ctx_row = await db.fetchone(
            "SELECT context_json FROM tasks WHERE id = $1", (task_id,))
        try:
            existing = ctx_row[0] if ctx_row else "{}"
            ctx = json.loads(existing) if isinstance(existing, str) else (existing or {})
        except (json.JSONDecodeError, TypeError):
            ctx = {}
        ctx["prompt_guidance"] = guidance
        await db.execute_write(
            "UPDATE tasks SET status = $1, retry_count = $2, error = NULL, "
            "context_json = $3, updated_at = $4 WHERE id = $5",
            ("pending", retry_count + 1, json.dumps(ctx), time.time(), task_id),
        )
        emits.append(Emit("task_reset", {
            "task_id": task_id,
            "project_id": project_id,
            "prompt_guidance": guidance,
            "retry_count": retry_count + 1,
        }, source="odin"))

    elif fix_type == "skip":
        await db.execute_write(
            "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
            ("cancelled", time.time(), task_id),
        )
        emits.append(Emit("task_skipped", {
            "task_id": task_id,
            "project_id": project_id,
            "reason": event.payload.get("root_cause", "skipped by diagnosis"),
        }, source="odin"))

    elif fix_type == "escalate":
        await db.execute_write(
            "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
            ("needs_review", time.time(), task_id),
        )
        emits.append(Emit("needs_human_review", {
            "task_id": task_id,
            "project_id": project_id,
            "reason": event.payload.get("root_cause", "escalated by diagnosis"),
        }, source="odin"))

    else:
        logger.warning("Unknown fix_type: %s for task %s", fix_type, task_id[:8])
        return [Emit("odin_error", {
            "error": f"Unknown fix_type: {fix_type}",
            "task_id": task_id,
        }, source="odin")]

    return emits
