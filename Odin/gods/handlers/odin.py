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

from gods.pipeline import Event, Emit, idem_key
from gods import safe_json
from gods.task_definition import apply_defaults, load_task_definition, get_registry
from gods.task_states import TaskState, TERMINAL_STATES

logger = logging.getLogger("gods.handlers.odin")

# Terminal statuses — needs_review is NOT terminal (task still needs work)
_TERMINAL = tuple(s.value for s in TERMINAL_STATES)


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
    gateway_url: str | None = None,
) -> dict[str, bool]:
    """Query LLM Gateway for provider status."""
    from gods.config import GATEWAY_URL
    if not gateway_url:
        gateway_url = GATEWAY_URL
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
    """Pick a provider for a task based on registry preference + availability.

    Checks the task type registry first for provider_preference.
    Falls back to _TIER_MAP for unregistered types.
    """
    registry = get_registry()
    spec = registry.get(task_type)
    if spec and spec.provider_preference:
        for prov in spec.provider_preference:
            if available.get(prov, False):
                return prov

    # Fallback to static tier map
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
        pid = row["id"]
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
        "SELECT t.id, t.task_type, t.priority, t.context_json, t.retry_count "
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

        task_retries = task_row.get("retry_count", 0) or 0
        emits.append(Emit("dispatch_command", {
            "task_id": tid,
            "project_id": project_id,
            "provider": provider,
        }, source="odin",
           idempotency_key=idem_key("dispatch", tid, str(task_retries)),
        ))

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

        # Version discriminator: total completed+failed count. If project resets
        # and re-runs, this will differ, so the idempotency key won't collide.
        total = await db.fetchone(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE project_id = $1", (project_id,),
        )
        task_count = str(_val(total, "cnt", 0))

        if failed_count > 0:
            await db.execute_write(
                "UPDATE projects SET status = $1, completed_at = $2, updated_at = $3 WHERE id = $4",
                ("failed", time.time(), time.time(), project_id),
            )
            emits.append(Emit("project_failed", {
                "project_id": project_id,
                "reason": f"{failed_count} task(s) failed",
            }, source="odin",
               idempotency_key=idem_key("project_failed", project_id, task_count),
            ))
        else:
            await db.execute_write(
                "UPDATE projects SET status = $1, completed_at = $2, updated_at = $3 WHERE id = $4",
                ("completed", time.time(), time.time(), project_id),
            )
            emits.append(Emit("project_complete", {
                "project_id": project_id,
            }, source="odin",
               idempotency_key=idem_key("project_complete", project_id, task_count),
            ))

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
            }, source="odin",
               idempotency_key=idem_key("project_failed", project_id, "deadlock", str(blocked_count)),
            ))
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

def make_odin_handle_diagnosis(pipeline):
    """Factory: create odin_handle_diagnosis bound to a pipeline for atomic transitions."""

    async def odin_handle_diagnosis(event: Event, db) -> list[Emit] | None:
        """Consume a task_diagnosis event and apply the recommended fix.

        Uses pipeline.transition_and_emit() for atomic state change + event write.

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

        if fix_type == "retry_as_is":
            # Check max retries
            if retry_count >= max_retries:
                logger.info("Task %s exhausted retries (%d/%d), escalating",
                            task_id[:8], retry_count, max_retries)
                emit = Emit("needs_human_review", {
                    "task_id": task_id,
                    "project_id": project_id,
                    "reason": f"Max retries exhausted ({retry_count}/{max_retries})",
                }, source="odin",
                   idempotency_key=idem_key("escalate_exhausted", task_id),
                )
                await pipeline.transition_and_emit(
                    task_id, "needs_review", [emit], source="odin.diagnosis",
                )
                return []  # emits already written atomically
            else:
                # Load stored TaskDefinition from context_json (set during decomposition)
                ctx = await _load_context_json(db, task_id)
                td = load_task_definition(ctx)
                delay = td.compute_retry_delay(retry_count)
                retry_after = time.time() + delay
                ctx["retry_after"] = retry_after

                emit = Emit("task_reset", {
                    "task_id": task_id,
                    "project_id": project_id,
                    "retry_count": retry_count + 1,
                    "retry_delay": delay,
                }, source="odin",
                   idempotency_key=idem_key("task_reset", task_id, str(retry_count + 1)),
                )
                await pipeline.transition_and_emit(
                    task_id, "pending", [emit],
                    extra_fields={
                        "retry_count": retry_count + 1,
                        "error": None,
                        "context_json": json.dumps(ctx),
                    },
                    source="odin.retry",
                )
                logger.info("Odin: task %s retry in %ds (attempt %d, %s backoff)",
                            task_id[:8], delay, retry_count + 1, td.retry_logic.value)
                return []

        elif fix_type == "reassign_tier":
            new_tier = event.payload.get("new_tier", "claude_code")

            # Load stored TaskDefinition from context_json
            ctx = await _load_context_json(db, task_id)
            td = load_task_definition(ctx)
            delay = td.compute_retry_delay(retry_count)
            retry_after = time.time() + delay
            ctx["retry_after"] = retry_after

            emit = Emit("task_reset", {
                "task_id": task_id,
                "project_id": project_id,
                "new_tier": new_tier,
                "retry_count": retry_count + 1,
                "retry_delay": delay,
            }, source="odin",
               idempotency_key=idem_key("task_reset", task_id, str(retry_count + 1)),
            )
            await pipeline.transition_and_emit(
                task_id, "pending", [emit],
                extra_fields={
                    "model_tier": new_tier,
                    "retry_count": retry_count + 1,
                    "error": None,
                    "context_json": json.dumps(ctx),
                },
                source="odin.reassign",
            )
            logger.info("Odin: task %s retry in %ds (attempt %d, %s backoff)",
                        task_id[:8], delay, retry_count + 1, td.retry_logic.value)
            return []

        elif fix_type == "modify_prompt":
            guidance = event.payload.get("prompt_guidance", "")
            ctx = await _load_context_json(db, task_id)
            ctx["prompt_guidance"] = guidance

            emit = Emit("task_reset", {
                "task_id": task_id,
                "project_id": project_id,
                "prompt_guidance": guidance,
                "retry_count": retry_count + 1,
            }, source="odin",
               idempotency_key=idem_key("task_reset", task_id, str(retry_count + 1)),
            )
            await pipeline.transition_and_emit(
                task_id, "pending", [emit],
                extra_fields={
                    "retry_count": retry_count + 1,
                    "error": None,
                    "context_json": json.dumps(ctx),
                },
                source="odin.modify_prompt",
            )
            return []

        elif fix_type == "skip":
            emit = Emit("task_skipped", {
                "task_id": task_id,
                "project_id": project_id,
                "reason": event.payload.get("root_cause", "skipped by diagnosis"),
            }, source="odin",
               idempotency_key=idem_key("task_skipped", task_id),
            )
            await pipeline.transition_and_emit(
                task_id, "cancelled", [emit], source="odin.skip",
            )
            return []

        elif fix_type == "escalate":
            emit = Emit("needs_human_review", {
                "task_id": task_id,
                "project_id": project_id,
                "reason": event.payload.get("root_cause", "escalated by diagnosis"),
            }, source="odin",
               idempotency_key=idem_key("escalate", task_id),
            )
            await pipeline.transition_and_emit(
                task_id, "needs_review", [emit], source="odin.escalate",
            )
            return []

        else:
            logger.warning("Unknown fix_type: %s for task %s", fix_type, task_id[:8])
            return [Emit("odin_error", {
                "error": f"Unknown fix_type: {fix_type}",
                "task_id": task_id,
            }, source="odin")]

    return odin_handle_diagnosis
