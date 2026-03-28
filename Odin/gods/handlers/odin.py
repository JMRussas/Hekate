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
from gods import safe_json
from gods.task_definition import apply_defaults

logger = logging.getLogger("gods.handlers.odin")

# Terminal statuses — needs_review is NOT terminal (task still needs work)
_TERMINAL = ("completed", "failed", "cancelled")


def _val(row, key, default=None):
    """Extract a value from a row (dict or tuple). Handles aggregates."""
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[0]
    except (IndexError, KeyError):
        return default

async def _get_task_type_complexity(db, task_id: str) -> tuple[str, str]:
    """Load task_type and complexity from DB, defaulting to code/medium."""
    row = await db.fetchone(
        "SELECT task_type, complexity FROM tasks WHERE id = $1", (task_id,)
    )
    if row:
        return (row.get("task_type") or "code"), (row.get("complexity") or "medium")
    return "code", "medium"


async def _load_context_json(db, task_id: str) -> dict:
    """Load and parse context_json from a task, returning a dict."""
    ctx_row = await db.fetchone(
        "SELECT context_json FROM tasks WHERE id = $1", (task_id,)
    )
    try:
        existing = _val(ctx_row, "context_json", "{}")
        return safe_json.loads_dict(existing) if isinstance(existing, str) else (existing or {})
    except (json.JSONDecodeError, TypeError):
        return {}


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
        logger.warning("Provider availability check failed (%s) - assuming all providers down (fail-conservative)", e)
        return {"claude_code": False, "gemini_cli": False, "ollama": False}


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
    ("research", "simple"): "claude_code",
    ("research", "medium"): "claude_code",
    ("research", "complex"): "claude_code",
    ("analysis", "simple"): "claude_code",
    ("analysis", "medium"): "claude_code",
    ("analysis", "complex"): "claude_code",
    ("integration", "simple"): "claude_code",
    ("integration", "medium"): "claude_code",
    ("integration", "complex"): "claude_code",
    ("documentation", "simple"): "claude_code",
    ("documentation", "medium"): "claude_code",
    ("asset", "simple"): "ollama",
    ("asset", "medium"): "ollama",
}

_FALLBACK = ["claude_code", "ollama"]


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

    current_status = row["status"]

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

    return [
        Emit("project_started", {
            "project_id": project_id,
            "plan_id": event.payload.get("plan_id"),
        }, source="odin"),
        # Immediately trigger dispatch — no tick scheduler needed
        Emit("project_tick", {
            "project_id": project_id,
        }, source="odin"),
    ]


# ---------------------------------------------------------------------------
# odin_tick — periodic scan of all executing projects
# ---------------------------------------------------------------------------

# Tasks stuck in 'running' longer than this are considered dead (crashed executor)
_STUCK_TASK_THRESHOLD_S = 600

# Projects stuck in 'planning' longer than this are considered hung
_STUCK_PLANNING_THRESHOLD_S = 300


async def odin_tick(event: Event, db) -> list[Emit] | None:
    """Scan executing projects and emit project_ticks. Also reset stuck tasks/projects."""
    now = time.time()
    emits: list[Emit] = []

    # --- Project ticks for executing projects ---
    rows = await db.fetchall(
        "SELECT id FROM projects WHERE status = $1",
        ("executing",),
    )
    for row in rows:
        emits.append(Emit("project_tick", {
            "project_id": row["id"],
        }, source="odin"))

    # --- Stuck running tasks: executor crashed, never wrote a completion event ---
    stuck_tasks = await db.fetchall(
        "SELECT id, project_id, status, updated_at FROM tasks "
        "WHERE status = $1 AND updated_at < $2",
        ("running", now - _STUCK_TASK_THRESHOLD_S),
    )
    for task_row in stuck_tasks:
        task_id = task_row["id"]
        project_id = task_row["project_id"]
        logger.warning(
            "Odin: task %s stuck in 'running' for %.0fs — resetting to 'pending'",
            task_id[:8], now - task_row["updated_at"],
        )
        await db.execute_write(
            "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
            ("pending", now, task_id),
        )
        emits.append(Emit("task_stuck_reset", {
            "task_id": task_id,
            "project_id": project_id,
            "stuck_seconds": int(now - task_row["updated_at"]),
        }, source="odin"))

    # --- Stuck planning projects: athena crashed mid-plan ---
    stuck_planning = await db.fetchall(
        "SELECT id, status, updated_at FROM projects "
        "WHERE status = $1 AND updated_at < $2",
        ("planning", now - _STUCK_PLANNING_THRESHOLD_S),
    )
    for proj_row in stuck_planning:
        proj_id = proj_row["id"]
        logger.warning(
            "Odin: project %s stuck in 'planning' for %.0fs — resetting to 'draft'",
            proj_id[:8], now - proj_row["updated_at"],
        )
        await db.execute_write(
            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
            ("draft", now, proj_id),
        )
        emits.append(Emit("project_stuck_reset", {
            "project_id": proj_id,
            "stuck_seconds": int(now - proj_row["updated_at"]),
            "previous_status": "planning",
        }, source="odin"))

    return emits or None


# ---------------------------------------------------------------------------
# run_startup_recovery — re-trigger projects that were lost across a restart
# ---------------------------------------------------------------------------

# How old a draft project must be (with no recent relay activity) before
# we consider it stuck and re-emit project_created
_DRAFT_RECOVERY_THRESHOLD_S = 60

# How old a planning project must be before we reset it to draft on startup
_PLANNING_RECOVERY_THRESHOLD_S = 300


async def run_startup_recovery(db) -> None:
    """Scan for projects that survived a service restart in an intermediate state.

    Called once on startup after cursor restore. Writes recovery events directly
    to god_relay_events so the pipeline picks them up on its first tick.

    1. Draft projects with no recent relay activity → write project_created to relay
       (their planning event was skipped by cursor restoration's -50 window)
    2. Planning projects stuck > threshold → reset to draft, write project_stuck_reset

    Safe to call multiple times — idempotent.
    """
    now = time.time()

    # --- Re-trigger draft projects that have gone silent ---
    draft_rows = await db.fetchall(
        "SELECT id, status, updated_at FROM projects WHERE status = $1",
        ("draft",),
    )
    for row in draft_rows:
        proj_id = row["id"]
        updated_at = row.get("updated_at") or 0

        # Skip very recently created/updated projects — they may still be starting
        if now - updated_at < _DRAFT_RECOVERY_THRESHOLD_S:
            continue

        # Check if there's been any relay activity for this project using
        # json_extract for correctness and index efficiency
        recent = await db.fetchone(
            "SELECT id FROM god_relay_events "
            "WHERE json_extract(payload, '$.project_id') = $1 "
            "ORDER BY id DESC LIMIT 1",
            (proj_id,),
        )
        if recent:
            continue

        logger.info(
            "Odin: startup recovery — writing project_created to relay for stuck draft %s",
            proj_id[:8],
        )
        await db.execute_write(
            "INSERT INTO god_relay_events "
            "(event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            ("project_created", "odin_recovery",
             json.dumps({"project_id": proj_id}),
             "info", now),
        )

    # --- Reset planning projects stuck across a restart ---
    planning_rows = await db.fetchall(
        "SELECT id, status, updated_at FROM projects WHERE status = $1",
        ("planning",),
    )
    for row in planning_rows:
        proj_id = row["id"]
        updated_at = row.get("updated_at") or 0

        if now - updated_at < _PLANNING_RECOVERY_THRESHOLD_S:
            continue

        logger.warning(
            "Odin: startup recovery — resetting stuck 'planning' project %s to 'draft'",
            proj_id[:8],
        )
        await db.execute_write(
            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
            ("draft", now, proj_id),
        )
        await db.execute_write(
            "INSERT INTO god_relay_events "
            "(event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            ("project_stuck_reset", "odin_recovery",
             json.dumps({
                 "project_id": proj_id,
                 "stuck_seconds": int(now - updated_at),
                 "previous_status": "planning",
             }),
             "warning", now),
        )


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
        "SELECT MIN(wave) AS min_wave FROM tasks "
        "WHERE project_id = $1 AND status NOT IN ($2, $3, $4)",
        (project_id, *_TERMINAL),
    )
    current_wave = _val(wave_row, "min_wave")
    if current_wave is None:
        return emits or None

    # Find ready tasks: pending, in current wave, all deps completed
    # Note: real DB may not have 'complexity' column — use COALESCE
    ready_raw = await db.fetchall(
        "SELECT t.id, t.task_type, t.priority, t.context_json "
        "FROM tasks t "
        "LEFT JOIN task_deps d ON d.task_id = t.id "
        "LEFT JOIN tasks dep ON dep.id = d.depends_on "
        "  AND dep.status != $1 "
        "WHERE t.project_id = $2 AND t.status = $3 AND t.wave = $4 "
        "GROUP BY t.id HAVING COUNT(dep.id) = 0 "
        "ORDER BY t.priority ASC",
        ("completed", project_id, "pending", current_wave),
    )

    # Filter out tasks with retry_after in the future
    now = time.time()
    ready = []
    for task_row in ready_raw:
        ctx_str = task_row.get("context_json") or "{}"
        ctx = safe_json.loads_dict(ctx_str) if isinstance(ctx_str, str) else (ctx_str or {})
        retry_after = ctx.get("retry_after")
        if retry_after and retry_after > now:
            logger.debug("Odin: skipping task %s — retry_after in %ds",
                         task_row["id"][:8], int(retry_after - now))
            continue
        ready.append(task_row)

    # Count running for concurrency limits
    running_row = await db.fetchone(
        "SELECT COUNT(*) AS cnt FROM tasks WHERE project_id = $1 AND status IN ($2, $3)",
        (project_id, "running", "queued"),
    )
    running_count = _val(running_row, "cnt", 0)

    # Compute parallelism with max_concurrent from event
    from gods.odin.dispatch import compute_wave_parallelism
    to_dispatch = compute_wave_parallelism(
        ready_task_count=len(ready),
        running_task_count=running_count,
        available_providers=available,
        max_concurrent=max_concurrent,
    )

    for task_row in ready[:to_dispatch]:
        tid = task_row["id"]
        ttype = task_row.get("task_type", "code")

        if not tid:
            continue

        # Default complexity to medium — real DB may not have this column
        complexity = "medium"
        provider = _select_provider(ttype, complexity, available)

        # Record provider selection but keep status pending —
        # hermes sets to running when it actually starts the CLI.
        # This avoids tasks getting stuck in "queued" if hermes can't start them.
        await db.execute_write(
            "UPDATE tasks SET model_tier = $1, updated_at = $2 WHERE id = $3",
            (provider, time.time(), tid),
        )

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
        "SELECT COUNT(*) AS cnt FROM tasks WHERE project_id = $1 AND status NOT IN ($2, $3, $4)",
        (project_id, *_TERMINAL),
    )
    remaining_count = _val(remaining, "cnt", 0)

    if remaining_count == 0:
        # All tasks are terminal — check for failures
        failed = await db.fetchone(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE project_id = $1 AND status = $2",
            (project_id, "failed"),
        )
        failed_count = _val(failed, "cnt", 0)

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

    # Unblock tasks whose dependencies are now satisfied — MUST run before deadlock check
    # Single atomic UPDATE avoids read-then-write race condition
    now = time.time()
    unblock_result = await db.execute_write(
        "UPDATE tasks SET status = $1, updated_at = $2 "
        "WHERE project_id = $3 AND status = $4 "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM task_deps d "
        "  LEFT JOIN tasks dep ON dep.id = d.depends_on "
        "  WHERE d.task_id = tasks.id AND dep.status != $5"
        ")",
        ("pending", now, project_id, "blocked", "completed"),
    )
    unblocked_count = getattr(unblock_result, "rowcount", 0) if unblock_result else 0
    if unblocked_count:
        logger.info("Odin: unblocked %d task(s) for project %s", unblocked_count, project_id[:8])

    # Check for deadlock: no pending/running/queued, but some blocked or needs_review
    active = await db.fetchone(
        "SELECT COUNT(*) AS cnt FROM tasks WHERE project_id = $1 AND status IN ($2, $3, $4)",
        (project_id, "pending", "queued", "running"),
    )
    active_count = _val(active, "cnt", 0)

    if active_count == 0:
        blocked = await db.fetchone(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE project_id = $1 AND status = $2",
            (project_id, "blocked"),
        )
        blocked_count = _val(blocked, "cnt", 0)
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
            verified_wave = _val(task_row, "wave")

            # Count non-terminal tasks remaining in this wave
            wave_remaining = await db.fetchone(
                "SELECT COUNT(*) AS cnt FROM tasks WHERE project_id = $1 AND wave = $2 "
                "AND status NOT IN ($3, $4, $5)",
                (project_id, verified_wave, *_TERMINAL),
            )
            if _val(wave_remaining, "cnt", 1) == 0:
                emits.append(Emit("wave_complete", {
                    "project_id": project_id,
                    "wave": verified_wave,
                }, source="odin"))

                # Also emit a project_tick to trigger dispatch for the next wave
                emits.append(Emit("project_tick", {
                    "project_id": project_id,
                }, source="odin"))

    # Always emit a project_tick after any task_verified — ensures dispatch
    # re-runs to pick up newly unblocked tasks
    if not any(e.event_type == "project_tick" for e in emits):
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

    retry_count = (row.get("retry_count") or 0)
    max_retries = (row.get("max_retries") or 3)

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
            # Compute backoff delay via TaskDefinition
            task_type, complexity = await _get_task_type_complexity(db, task_id)
            td = apply_defaults(task_type, complexity)
            delay = td.compute_retry_delay(retry_count)
            retry_after = time.time() + delay

            # Store retry_after in context_json
            ctx = await _load_context_json(db, task_id)
            ctx["retry_after"] = retry_after

            await db.execute_write(
                "UPDATE tasks SET status = $1, retry_count = $2, error = NULL, "
                "context_json = $3, updated_at = $4 WHERE id = $5",
                ("pending", retry_count + 1, json.dumps(ctx), time.time(), task_id),
            )
            logger.info("Odin: task %s retry in %ds (attempt %d, %s backoff)",
                        task_id[:8], delay, retry_count + 1, td.retry_logic.value)
            emits.append(Emit("task_reset", {
                "task_id": task_id,
                "project_id": project_id,
                "retry_count": retry_count + 1,
                "retry_delay": delay,
            }, source="odin"))

    elif fix_type == "reassign_tier":
        new_tier = event.payload.get("new_tier", "claude_code")

        # Compute backoff delay via TaskDefinition
        task_type, complexity = await _get_task_type_complexity(db, task_id)
        td = apply_defaults(task_type, complexity)
        delay = td.compute_retry_delay(retry_count)
        retry_after = time.time() + delay

        # Store retry_after in context_json
        ctx = await _load_context_json(db, task_id)
        ctx["retry_after"] = retry_after

        await db.execute_write(
            "UPDATE tasks SET status = $1, model_tier = $2, retry_count = $3, "
            "error = NULL, context_json = $4, updated_at = $5 WHERE id = $6",
            ("pending", new_tier, retry_count + 1, json.dumps(ctx), time.time(), task_id),
        )
        logger.info("Odin: task %s retry in %ds (attempt %d, %s backoff)",
                    task_id[:8], delay, retry_count + 1, td.retry_logic.value)
        emits.append(Emit("task_reset", {
            "task_id": task_id,
            "project_id": project_id,
            "new_tier": new_tier,
            "retry_count": retry_count + 1,
            "retry_delay": delay,
        }, source="odin"))

    elif fix_type == "modify_prompt":
        guidance = event.payload.get("prompt_guidance", "")
        # Read existing context_json, merge guidance into it
        ctx_row = await db.fetchone(
            "SELECT context_json FROM tasks WHERE id = $1", (task_id,))
        try:
            existing = _val(ctx_row, "context_json", "{}")
            ctx = safe_json.loads_dict(existing) if isinstance(existing, str) else (existing or {})
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
