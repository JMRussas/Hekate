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

            # Collect error text and tiers for diagnosis
            error_samples = {}
            tier_counts: dict[str, int] = {}
            for tid in max_run:
                err = getattr(state, "task_errors", {}).get(tid, "")
                if err:
                    error_samples[tid] = err[:300]
                tier = getattr(state, "task_tiers", {}).get(tid, "")
                if tier:
                    tier_counts[tier] = tier_counts.get(tier, 0) + 1

            # Detect same-tier pattern (e.g., all codex_cli failures)
            dominant_tier = max(tier_counts, key=tier_counts.get) if tier_counts else None
            same_tier = dominant_tier and tier_counts.get(dominant_tier, 0) == len(max_run)

            msg = (
                f"Cascade failure in wave {wave}: "
                f"{len(max_run)} consecutive failures"
            )
            if same_tier:
                msg += f" (all on {dominant_tier})"

            results.append(SentinelObservation(
                category="cascade_failure",
                message=msg,
                severity=Severity.CRITICAL,
                project_id=project_id,
                details={
                    "rule": "cascade_failure",
                    "wave": wave,
                    "consecutive_failure_count": len(max_run),
                    "failed_task_ids": max_run,
                    "error_samples": error_samples,
                    "tier_counts": tier_counts,
                    "dominant_tier": dominant_tier,
                    "same_tier_failure": same_tier,
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
# New rules operating on ProjectWorldModel (for future orchestrator)
# -----------------------------------------------------------------------

def check_tasks_ready(world: ProjectWorldModel) -> list[SentinelObservation]:
    """Detect tasks that are pending and could be dispatched."""
    ready_ids = [
        tid for tid, t in world.tasks.items()
        if t.status == "pending"
    ]
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
    wave = world.current_wave
    wave_tasks = [
        t for t in world.tasks.values()
        if t.wave == wave
    ]
    if not wave_tasks:
        return []

    all_done = all(t.status in ("completed", "failed", "cancelled") for t in wave_tasks)
    if not all_done:
        return []

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
    if not world.tasks:
        return []

    terminal = {"completed", "failed", "cancelled"}
    all_terminal = all(t.status in terminal for t in world.tasks.values())
    if not all_terminal:
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
    if not world.tasks:
        return []

    all_failed = all(
        t.status in ("failed", "cancelled") for t in world.tasks.values()
    )
    if not all_failed:
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
