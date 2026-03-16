#  Sentinel Detection Rules
#
#  Standalone rule functions extracted from PlanSentinel.  Each rule accepts
#  the internal PlanState (for backward-compat with the existing tick loop)
#  plus project metadata, and returns a list of SentinelObservation.
#
#  New rules that operate on ProjectWorldModel are also defined here.
#
#  Used by: plan_sentinel.py (existing tick loop), future orchestrator

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from backend.services.sentinel.models import (
    ProjectWorldModel,
    SentinelObservation,
    Severity,
    TaskWorldState,
)

if TYPE_CHECKING:
    from backend.services.sentinel.plan_sentinel import PlanState, TaskState

# -----------------------------------------------------------------------
# Configurable thresholds (mirrored from PlanSentinel class attributes)
# -----------------------------------------------------------------------
DEFAULT_STUCK_THRESHOLD_SECS: float = 300.0  # 5 minutes
DEFAULT_CASCADE_FAILURE_MIN: int = 3
DEFAULT_BUDGET_PERCENT_THRESHOLD: float = 0.80


# -----------------------------------------------------------------------
# Rules operating on PlanState (existing tick-loop rules, extracted)
# -----------------------------------------------------------------------

def check_task_stuck(
    state: PlanState,
    project_id: str,
    *,
    stuck_threshold_secs: float = DEFAULT_STUCK_THRESHOLD_SECS,
) -> list[SentinelObservation]:
    """Detect tasks running > threshold with no progress."""
    from backend.services.sentinel.plan_sentinel import TaskState as TS

    now = time.time()
    results: list[SentinelObservation] = []
    for task_id, status in state.task_statuses.items():
        if status != TS.RUNNING:
            continue
        timing = state.task_timing.get(task_id)
        if timing is None or timing.last_progress_at is None:
            continue
        elapsed = now - timing.last_progress_at
        if elapsed > stuck_threshold_secs:
            results.append(SentinelObservation(
                category="task_stuck",
                message=(
                    f"Task {task_id[:8]} has had no progress for "
                    f"{elapsed:.0f}s (threshold: {stuck_threshold_secs:.0f}s)"
                ),
                severity=Severity.WARNING,
                project_id=project_id,
                task_id=task_id,
                details={
                    "rule": "task_stuck",
                    "elapsed_secs": round(elapsed, 1),
                    "threshold_secs": stuck_threshold_secs,
                },
            ))
    return results


def check_wave_stalled(
    state: PlanState,
    project_id: str,
    *,
    stuck_threshold_secs: float = DEFAULT_STUCK_THRESHOLD_SECS,
) -> list[SentinelObservation]:
    """Detect when all tasks in the current wave are failed or stuck."""
    from backend.services.sentinel.plan_sentinel import TaskState as TS

    if state.current_wave is None:
        return []

    now = time.time()
    wave_tasks: list[str] = []
    for task_id, status in state.task_statuses.items():
        if status in (
            TS.RUNNING, TS.FAILED,
            TS.PENDING, TS.QUEUED, TS.BLOCKED,
        ):
            wave_tasks.append(task_id)

    if not wave_tasks:
        return []

    all_stalled = True
    for task_id in wave_tasks:
        status = state.task_statuses[task_id]
        if status == TS.FAILED:
            continue
        if status == TS.RUNNING:
            timing = state.task_timing.get(task_id)
            if timing and timing.last_progress_at:
                elapsed = now - timing.last_progress_at
                if elapsed <= stuck_threshold_secs:
                    all_stalled = False
                    break
            else:
                all_stalled = False
                break
        else:
            all_stalled = False
            break

    if not all_stalled:
        return []

    return [SentinelObservation(
        category="wave_stalled",
        message=(
            f"Wave {state.current_wave} is stalled: all "
            f"{len(wave_tasks)} tasks are failed or stuck"
        ),
        severity=Severity.CRITICAL,
        project_id=project_id,
        details={
            "rule": "wave_stalled",
            "wave": state.current_wave,
            "task_count": len(wave_tasks),
            "task_ids": wave_tasks,
        },
    )]


def check_cascade_failure(
    state: PlanState,
    project_id: str,
    *,
    cascade_failure_min: int = DEFAULT_CASCADE_FAILURE_MIN,
) -> list[SentinelObservation]:
    """Detect 3+ consecutive task failures in the same wave."""
    results: list[SentinelObservation] = []
    for wave, outcomes in state.wave_outcomes.items():
        consecutive_failures: list[str] = []
        max_run: list[str] = []

        for task_id, outcome in outcomes:
            if outcome == "failed":
                consecutive_failures.append(task_id)
            else:
                if len(consecutive_failures) > len(max_run):
                    max_run = list(consecutive_failures)
                consecutive_failures = []

        if len(consecutive_failures) > len(max_run):
            max_run = consecutive_failures

        if len(max_run) >= cascade_failure_min:
            obs_key = f"cascade_failure:wave_{wave}"
            if obs_key in state.emitted_observations:
                continue
            state.emitted_observations.add(obs_key)
            results.append(SentinelObservation(
                category="cascade_failure",
                message=(
                    f"Cascade failure in wave {wave}: "
                    f"{len(max_run)} consecutive failures"
                ),
                severity=Severity.CRITICAL,
                project_id=project_id,
                details={
                    "rule": "cascade_failure",
                    "wave": wave,
                    "consecutive_failure_count": len(max_run),
                    "failed_task_ids": max_run,
                },
            ))
    return results


def check_budget_warning(
    state: PlanState,
    project_id: str,
    *,
    budget_percent_threshold: float = DEFAULT_BUDGET_PERCENT_THRESHOLD,
) -> list[SentinelObservation]:
    """Detect budget usage exceeding threshold."""
    if state.budget_limit <= 0:
        return []
    ratio = state.budget_spent / state.budget_limit
    if ratio < budget_percent_threshold:
        return []

    obs_key = "budget_warning"
    if obs_key in state.emitted_observations:
        return []
    state.emitted_observations.add(obs_key)

    pct = ratio * 100
    return [SentinelObservation(
        category="budget_warning",
        message=(
            f"Budget warning: spending at {pct:.1f}% of limit "
            f"(${state.budget_spent:.2f} / ${state.budget_limit:.2f})"
        ),
        severity=Severity.WARNING,
        project_id=project_id,
        details={
            "rule": "budget_warning",
            "budget_spent": state.budget_spent,
            "budget_limit": state.budget_limit,
            "usage_percent": round(pct, 1),
        },
    )]


# -----------------------------------------------------------------------
# Pure state detection functions (Phase 2 — sentinel-as-orchestrator)
#
# These derive answers purely from ProjectWorldModel, no DB queries.
# Used by the sentinel orchestrator for state decisions.
# -----------------------------------------------------------------------

def detect_tasks_ready(world: ProjectWorldModel) -> list[str]:
    """Return task IDs in the current wave that are pending and ready for dispatch.

    A task is ready when:
    - It belongs to the current wave
    - Its status is "pending"
    """
    return [
        tid for tid, t in world.tasks.items()
        if t.status == "pending" and t.wave == world.current_wave
    ]


def detect_wave_complete(world: ProjectWorldModel) -> bool:
    """Return True when all tasks in the current wave are in a terminal state."""
    terminal = {"completed", "failed", "cancelled"}
    wave_tasks = [t for t in world.tasks.values() if t.wave == world.current_wave]
    if not wave_tasks:
        return False
    return all(t.status in terminal for t in wave_tasks)


def detect_project_complete(world: ProjectWorldModel) -> tuple[bool, str]:
    """Return (done, reason) — True when all tasks are terminal."""
    if not world.tasks:
        return False, ""

    terminal = {"completed", "failed", "cancelled"}
    all_terminal = all(t.status in terminal for t in world.tasks.values())
    if not all_terminal:
        return False, ""

    completed = sum(1 for t in world.tasks.values() if t.status == "completed")
    failed = sum(1 for t in world.tasks.values() if t.status == "failed")
    cancelled = sum(1 for t in world.tasks.values() if t.status == "cancelled")
    total = len(world.tasks)

    reason = f"{completed}/{total} succeeded, {failed} failed, {cancelled} cancelled"
    return True, reason


def detect_dead_project(world: ProjectWorldModel) -> bool:
    """Return True when project is blocked with no active or pending tasks.

    A project is dead when it has tasks but none are running or pending —
    everything is failed, cancelled, or there's a mix of completed and
    failed/cancelled with nothing left to dispatch.
    """
    if not world.tasks:
        return False

    active_statuses = {"pending", "running", "queued"}
    has_active = any(t.status in active_statuses for t in world.tasks.values())
    if has_active:
        return False

    # All tasks are terminal — dead if none succeeded, or if there are
    # still undispatched waves with no way to reach them
    has_completed = any(t.status == "completed" for t in world.tasks.values())
    all_terminal = all(
        t.status in ("completed", "failed", "cancelled")
        for t in world.tasks.values()
    )
    if not all_terminal:
        return False

    # If everything completed successfully, that's project_complete, not dead
    if has_completed and not any(
        t.status in ("failed", "cancelled") for t in world.tasks.values()
    ):
        return False

    # Dead if all failed/cancelled, or if remaining waves can't proceed
    # because the current wave has failures blocking progress
    all_failed_or_cancelled = all(
        t.status in ("failed", "cancelled") for t in world.tasks.values()
    )
    if all_failed_or_cancelled:
        return True

    # Mixed terminal: check if future waves exist with pending-like tasks
    max_wave = max(t.wave for t in world.tasks.values())
    if world.current_wave < max_wave:
        # There are future waves but nothing can dispatch — blocked
        current_wave_tasks = [
            t for t in world.tasks.values() if t.wave == world.current_wave
        ]
        current_all_failed = all(
            t.status in ("failed", "cancelled") for t in current_wave_tasks
        )
        if current_all_failed:
            return True

    return False


def detect_hollow_completions(world: ProjectWorldModel) -> list[str]:
    """Return task IDs that completed but produced no meaningful output.

    These are "hollow" completions — the task says it's done but there's
    nothing to show for it, which usually indicates a silent failure.
    """
    return [
        tid for tid, t in world.tasks.items()
        if t.status == "completed" and not t.output_summary.strip()
    ]


# -----------------------------------------------------------------------
# Observation-emitting wrappers (backward compat with existing tick loop)
#
# These delegate to detect_* for logic, then wrap results as observations.
# -----------------------------------------------------------------------

def check_tasks_ready(world: ProjectWorldModel) -> list[SentinelObservation]:
    """Detect tasks that are pending and could be dispatched."""
    ready_ids = detect_tasks_ready(world)
    if not ready_ids:
        return []
    return [SentinelObservation(
        category="tasks_ready",
        message=f"{len(ready_ids)} task(s) ready for dispatch",
        severity=Severity.INFO,
        project_id=world.project_id,
        details={
            "rule": "tasks_ready",
            "task_ids": ready_ids,
            "count": len(ready_ids),
        },
    )]


def check_wave_complete(world: ProjectWorldModel) -> list[SentinelObservation]:
    """Detect when all tasks in the current wave have completed."""
    if not detect_wave_complete(world):
        return []

    wave = world.current_wave
    wave_tasks = [t for t in world.tasks.values() if t.wave == wave]
    completed = sum(1 for t in wave_tasks if t.status == "completed")
    failed = sum(1 for t in wave_tasks if t.status == "failed")

    return [SentinelObservation(
        category="wave_complete",
        message=(
            f"Wave {wave} complete: {completed} succeeded, {failed} failed "
            f"out of {len(wave_tasks)} tasks"
        ),
        severity=Severity.INFO,
        project_id=world.project_id,
        details={
            "rule": "wave_complete",
            "wave": wave,
            "total": len(wave_tasks),
            "completed": completed,
            "failed": failed,
        },
    )]


def check_project_complete(world: ProjectWorldModel) -> list[SentinelObservation]:
    """Detect when all tasks in the project are in a terminal state."""
    done, reason = detect_project_complete(world)
    if not done:
        return []

    completed = sum(1 for t in world.tasks.values() if t.status == "completed")
    failed = sum(1 for t in world.tasks.values() if t.status == "failed")
    total = len(world.tasks)

    return [SentinelObservation(
        category="project_complete",
        message=(
            f"Project {world.project_id} complete: "
            f"{completed}/{total} succeeded, {failed} failed"
        ),
        severity=Severity.INFO,
        project_id=world.project_id,
        details={
            "rule": "project_complete",
            "total": total,
            "completed": completed,
            "failed": failed,
        },
    )]


def check_dead_project(world: ProjectWorldModel) -> list[SentinelObservation]:
    """Detect a project where all tasks have failed — nothing left to run."""
    if not detect_dead_project(world):
        return []

    return [SentinelObservation(
        category="dead_project",
        message=(
            f"Project {world.project_id} is dead: "
            f"all {len(world.tasks)} tasks failed or cancelled"
        ),
        severity=Severity.CRITICAL,
        project_id=world.project_id,
        details={
            "rule": "dead_project",
            "total": len(world.tasks),
            "failed_ids": [t.id for t in world.tasks.values()],
        },
    )]


def check_hollow_completions(world: ProjectWorldModel) -> list[SentinelObservation]:
    """Detect tasks that completed with empty output (silent failures)."""
    hollow_ids = detect_hollow_completions(world)
    if not hollow_ids:
        return []
    return [SentinelObservation(
        category="hollow_completion",
        message=f"{len(hollow_ids)} task(s) completed with empty output",
        severity=Severity.WARNING,
        project_id=world.project_id,
        details={
            "rule": "hollow_completion",
            "task_ids": hollow_ids,
            "count": len(hollow_ids),
        },
    )]
