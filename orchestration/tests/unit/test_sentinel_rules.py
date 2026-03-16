#  Sentinel Rules — Unit Tests
#
#  Tests for standalone detection rules extracted into rules.py.
#  Covers both PlanState-based rules (backward compat) and
#  ProjectWorldModel-based rules (new orchestrator).

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.anyio

from backend.services.sentinel.models import (
    ProjectWorldModel,
    Severity,
    TaskWorldState,
)
from backend.services.sentinel.plan_sentinel import (
    PlanState,
    TaskState,
    TaskTiming,
)
from backend.services.sentinel.rules import (
    check_task_stuck,
    check_wave_stalled,
    check_cascade_failure,
    check_budget_warning,
    check_tasks_ready,
    check_wave_complete,
    check_project_complete,
    check_dead_project,
    check_hollow_completions,
    detect_tasks_ready,
    detect_wave_complete,
    detect_project_complete,
    detect_dead_project,
    detect_hollow_completions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**kwargs) -> PlanState:
    return PlanState(**kwargs)


def _make_world(**kwargs) -> ProjectWorldModel:
    defaults = {"project_id": "proj-test", "status": "running"}
    defaults.update(kwargs)
    return ProjectWorldModel(**defaults)


# ===========================================================================
# PlanState-based rules (extracted from PlanSentinel)
# ===========================================================================

class TestCheckTaskStuck:

    def test_stuck_task_detected(self):
        old = time.time() - 600  # 10 minutes ago
        state = _make_state(
            task_statuses={"t1": TaskState.RUNNING},
            task_timing={"t1": TaskTiming(started_at=old, last_progress_at=old)},
        )
        obs = check_task_stuck(state, "proj-1")
        assert len(obs) == 1
        assert obs[0].category == "task_stuck"
        assert obs[0].task_id == "t1"
        assert obs[0].severity == Severity.WARNING

    def test_recent_progress_no_alert(self):
        recent = time.time() - 10
        state = _make_state(
            task_statuses={"t1": TaskState.RUNNING},
            task_timing={"t1": TaskTiming(started_at=recent, last_progress_at=recent)},
        )
        obs = check_task_stuck(state, "proj-1")
        assert len(obs) == 0

    def test_non_running_tasks_ignored(self):
        old = time.time() - 600
        state = _make_state(
            task_statuses={"t1": TaskState.COMPLETED},
            task_timing={"t1": TaskTiming(started_at=old, last_progress_at=old)},
        )
        obs = check_task_stuck(state, "proj-1")
        assert len(obs) == 0

    def test_no_timing_data_ignored(self):
        state = _make_state(
            task_statuses={"t1": TaskState.RUNNING},
            task_timing={},
        )
        obs = check_task_stuck(state, "proj-1")
        assert len(obs) == 0

    def test_no_last_progress_ignored(self):
        state = _make_state(
            task_statuses={"t1": TaskState.RUNNING},
            task_timing={"t1": TaskTiming(started_at=time.time() - 600)},
        )
        obs = check_task_stuck(state, "proj-1")
        assert len(obs) == 0

    def test_multiple_stuck_tasks(self):
        old = time.time() - 600
        state = _make_state(
            task_statuses={"t1": TaskState.RUNNING, "t2": TaskState.RUNNING},
            task_timing={
                "t1": TaskTiming(started_at=old, last_progress_at=old),
                "t2": TaskTiming(started_at=old, last_progress_at=old),
            },
        )
        obs = check_task_stuck(state, "proj-1")
        assert len(obs) == 2

    def test_custom_threshold(self):
        old = time.time() - 30
        state = _make_state(
            task_statuses={"t1": TaskState.RUNNING},
            task_timing={"t1": TaskTiming(started_at=old, last_progress_at=old)},
        )
        # Default threshold (300s) — should not fire
        assert len(check_task_stuck(state, "proj-1")) == 0
        # Custom threshold (10s) — should fire
        assert len(check_task_stuck(state, "proj-1", stuck_threshold_secs=10.0)) == 1


class TestCheckWaveStalled:

    def test_all_failed_is_stalled(self):
        state = _make_state(
            current_wave=1,
            task_statuses={"t1": TaskState.FAILED, "t2": TaskState.FAILED},
        )
        obs = check_wave_stalled(state, "proj-1")
        assert len(obs) == 1
        assert obs[0].category == "wave_stalled"
        assert obs[0].severity == Severity.CRITICAL

    def test_mixed_failed_and_stuck(self):
        old = time.time() - 600
        state = _make_state(
            current_wave=1,
            task_statuses={"t1": TaskState.FAILED, "t2": TaskState.RUNNING},
            task_timing={"t2": TaskTiming(started_at=old, last_progress_at=old)},
        )
        obs = check_wave_stalled(state, "proj-1")
        assert len(obs) == 1

    def test_progressing_task_not_stalled(self):
        recent = time.time() - 5
        state = _make_state(
            current_wave=1,
            task_statuses={"t1": TaskState.FAILED, "t2": TaskState.RUNNING},
            task_timing={"t2": TaskTiming(started_at=recent, last_progress_at=recent)},
        )
        obs = check_wave_stalled(state, "proj-1")
        assert len(obs) == 0

    def test_pending_tasks_not_stalled(self):
        state = _make_state(
            current_wave=1,
            task_statuses={"t1": TaskState.PENDING},
        )
        obs = check_wave_stalled(state, "proj-1")
        assert len(obs) == 0

    def test_no_current_wave(self):
        state = _make_state(current_wave=None)
        obs = check_wave_stalled(state, "proj-1")
        assert len(obs) == 0

    def test_no_wave_tasks(self):
        state = _make_state(
            current_wave=1,
            task_statuses={"t1": TaskState.COMPLETED},
        )
        obs = check_wave_stalled(state, "proj-1")
        assert len(obs) == 0


class TestCheckCascadeFailure:

    def test_three_consecutive_failures(self):
        state = _make_state(
            wave_outcomes={
                1: [("t1", "failed"), ("t2", "failed"), ("t3", "failed")]
            },
        )
        obs = check_cascade_failure(state, "proj-1")
        assert len(obs) == 1
        assert obs[0].category == "cascade_failure"
        assert obs[0].details["consecutive_failure_count"] == 3

    def test_success_resets_run(self):
        state = _make_state(
            wave_outcomes={
                1: [("t1", "failed"), ("t2", "completed"), ("t3", "failed")]
            },
        )
        obs = check_cascade_failure(state, "proj-1")
        assert len(obs) == 0

    def test_trailing_run(self):
        state = _make_state(
            wave_outcomes={
                1: [("t1", "completed"), ("t2", "failed"), ("t3", "failed"), ("t4", "failed")]
            },
        )
        obs = check_cascade_failure(state, "proj-1")
        assert len(obs) == 1
        assert obs[0].details["failed_task_ids"] == ["t2", "t3", "t4"]

    def test_dedup_emitted_observations(self):
        state = _make_state(
            wave_outcomes={
                1: [("t1", "failed"), ("t2", "failed"), ("t3", "failed")]
            },
        )
        obs1 = check_cascade_failure(state, "proj-1")
        obs2 = check_cascade_failure(state, "proj-1")
        assert len(obs1) == 1
        assert len(obs2) == 0  # deduped

    def test_below_threshold(self):
        state = _make_state(
            wave_outcomes={1: [("t1", "failed"), ("t2", "failed")]},
        )
        obs = check_cascade_failure(state, "proj-1")
        assert len(obs) == 0

    def test_custom_threshold(self):
        state = _make_state(
            wave_outcomes={1: [("t1", "failed"), ("t2", "failed")]},
        )
        obs = check_cascade_failure(state, "proj-1", cascade_failure_min=2)
        assert len(obs) == 1

    def test_multiple_waves(self):
        state = _make_state(
            wave_outcomes={
                1: [("t1", "failed"), ("t2", "failed"), ("t3", "failed")],
                2: [("t4", "failed"), ("t5", "failed"), ("t6", "failed")],
            },
        )
        obs = check_cascade_failure(state, "proj-1")
        assert len(obs) == 2


class TestCheckBudgetWarning:

    def test_over_threshold(self):
        state = _make_state(budget_spent=90.0, budget_limit=100.0)
        obs = check_budget_warning(state, "proj-1")
        assert len(obs) == 1
        assert obs[0].category == "budget_warning"
        assert obs[0].severity == Severity.WARNING
        assert obs[0].details["usage_percent"] == 90.0

    def test_under_threshold(self):
        state = _make_state(budget_spent=50.0, budget_limit=100.0)
        obs = check_budget_warning(state, "proj-1")
        assert len(obs) == 0

    def test_no_budget_limit(self):
        state = _make_state(budget_spent=50.0, budget_limit=0.0)
        obs = check_budget_warning(state, "proj-1")
        assert len(obs) == 0

    def test_dedup(self):
        state = _make_state(budget_spent=90.0, budget_limit=100.0)
        obs1 = check_budget_warning(state, "proj-1")
        obs2 = check_budget_warning(state, "proj-1")
        assert len(obs1) == 1
        assert len(obs2) == 0

    def test_custom_threshold(self):
        state = _make_state(budget_spent=60.0, budget_limit=100.0)
        # Default 80% — should not fire
        assert len(check_budget_warning(state, "proj-1")) == 0
        # Custom 50% — should fire
        state2 = _make_state(budget_spent=60.0, budget_limit=100.0)
        assert len(check_budget_warning(state2, "proj-1", budget_percent_threshold=0.50)) == 1


# ===========================================================================
# ProjectWorldModel-based rules (new)
# ===========================================================================

class TestCheckTasksReady:

    def test_pending_tasks_detected(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="pending"),
            "t2": TaskWorldState(id="t2", status="running"),
        })
        obs = check_tasks_ready(world)
        assert len(obs) == 1
        assert obs[0].category == "tasks_ready"
        assert obs[0].details["count"] == 1
        assert "t1" in obs[0].details["task_ids"]

    def test_no_pending_tasks(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="running"),
            "t2": TaskWorldState(id="t2", status="completed"),
        })
        obs = check_tasks_ready(world)
        assert len(obs) == 0

    def test_empty_tasks(self):
        world = _make_world(tasks={})
        obs = check_tasks_ready(world)
        assert len(obs) == 0

    def test_multiple_pending(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="pending"),
            "t2": TaskWorldState(id="t2", status="pending"),
            "t3": TaskWorldState(id="t3", status="completed"),
        })
        obs = check_tasks_ready(world)
        assert len(obs) == 1
        assert obs[0].details["count"] == 2


class TestCheckWaveComplete:

    def test_wave_complete_all_succeeded(self):
        world = _make_world(
            current_wave=1,
            tasks={
                "t1": TaskWorldState(id="t1", status="completed", wave=1),
                "t2": TaskWorldState(id="t2", status="completed", wave=1),
            },
        )
        obs = check_wave_complete(world)
        assert len(obs) == 1
        assert obs[0].category == "wave_complete"
        assert obs[0].details["completed"] == 2
        assert obs[0].details["failed"] == 0

    def test_wave_complete_with_failures(self):
        world = _make_world(
            current_wave=1,
            tasks={
                "t1": TaskWorldState(id="t1", status="completed", wave=1),
                "t2": TaskWorldState(id="t2", status="failed", wave=1),
            },
        )
        obs = check_wave_complete(world)
        assert len(obs) == 1
        assert obs[0].details["completed"] == 1
        assert obs[0].details["failed"] == 1

    def test_wave_not_complete(self):
        world = _make_world(
            current_wave=1,
            tasks={
                "t1": TaskWorldState(id="t1", status="completed", wave=1),
                "t2": TaskWorldState(id="t2", status="running", wave=1),
            },
        )
        obs = check_wave_complete(world)
        assert len(obs) == 0

    def test_ignores_other_waves(self):
        world = _make_world(
            current_wave=1,
            tasks={
                "t1": TaskWorldState(id="t1", status="completed", wave=1),
                "t2": TaskWorldState(id="t2", status="running", wave=2),
            },
        )
        obs = check_wave_complete(world)
        assert len(obs) == 1

    def test_no_tasks_in_wave(self):
        world = _make_world(
            current_wave=1,
            tasks={
                "t1": TaskWorldState(id="t1", status="running", wave=2),
            },
        )
        obs = check_wave_complete(world)
        assert len(obs) == 0

    def test_cancelled_counts_as_terminal(self):
        world = _make_world(
            current_wave=1,
            tasks={
                "t1": TaskWorldState(id="t1", status="cancelled", wave=1),
            },
        )
        obs = check_wave_complete(world)
        assert len(obs) == 1


class TestCheckProjectComplete:

    def test_all_completed(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed"),
            "t2": TaskWorldState(id="t2", status="completed"),
        })
        obs = check_project_complete(world)
        assert len(obs) == 1
        assert obs[0].category == "project_complete"
        assert obs[0].details["completed"] == 2
        assert obs[0].details["failed"] == 0

    def test_mixed_terminal(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed"),
            "t2": TaskWorldState(id="t2", status="failed"),
            "t3": TaskWorldState(id="t3", status="cancelled"),
        })
        obs = check_project_complete(world)
        assert len(obs) == 1
        assert obs[0].details["total"] == 3

    def test_not_all_terminal(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed"),
            "t2": TaskWorldState(id="t2", status="running"),
        })
        obs = check_project_complete(world)
        assert len(obs) == 0

    def test_empty_tasks(self):
        world = _make_world(tasks={})
        obs = check_project_complete(world)
        assert len(obs) == 0


class TestCheckDeadProject:

    def test_all_failed(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="failed"),
            "t2": TaskWorldState(id="t2", status="failed"),
        })
        obs = check_dead_project(world)
        assert len(obs) == 1
        assert obs[0].category == "dead_project"
        assert obs[0].severity == Severity.CRITICAL

    def test_all_cancelled(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="cancelled"),
        })
        obs = check_dead_project(world)
        assert len(obs) == 1

    def test_mixed_failed_cancelled(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="failed"),
            "t2": TaskWorldState(id="t2", status="cancelled"),
        })
        obs = check_dead_project(world)
        assert len(obs) == 1

    def test_has_completed_not_dead(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed"),
            "t2": TaskWorldState(id="t2", status="failed"),
        })
        obs = check_dead_project(world)
        assert len(obs) == 0

    def test_has_running_not_dead(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="running"),
            "t2": TaskWorldState(id="t2", status="failed"),
        })
        obs = check_dead_project(world)
        assert len(obs) == 0

    def test_empty_tasks(self):
        world = _make_world(tasks={})
        obs = check_dead_project(world)
        assert len(obs) == 0


class TestCheckHollowCompletions:

    def test_empty_output_detected(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed", output_summary=""),
            "t2": TaskWorldState(id="t2", status="completed", output_summary="good output"),
        })
        obs = check_hollow_completions(world)
        assert len(obs) == 1
        assert obs[0].category == "hollow_completion"
        assert obs[0].details["count"] == 1
        assert "t1" in obs[0].details["task_ids"]

    def test_whitespace_only_is_hollow(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed", output_summary="   \n  "),
        })
        obs = check_hollow_completions(world)
        assert len(obs) == 1
        assert "t1" in obs[0].details["task_ids"]

    def test_no_hollow_completions(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed", output_summary="done"),
        })
        obs = check_hollow_completions(world)
        assert len(obs) == 0

    def test_non_completed_tasks_ignored(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="failed", output_summary=""),
            "t2": TaskWorldState(id="t2", status="running", output_summary=""),
        })
        obs = check_hollow_completions(world)
        assert len(obs) == 0

    def test_multiple_hollow(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed", output_summary=""),
            "t2": TaskWorldState(id="t2", status="completed", output_summary=""),
            "t3": TaskWorldState(id="t3", status="completed", output_summary="real"),
        })
        obs = check_hollow_completions(world)
        assert len(obs) == 1
        assert obs[0].details["count"] == 2

    def test_empty_tasks(self):
        world = _make_world(tasks={})
        obs = check_hollow_completions(world)
        assert len(obs) == 0


# ===========================================================================
# Pure detect_* functions (Phase 2 — direct return values, no observations)
# ===========================================================================

class TestDetectTasksReady:

    def test_returns_pending_in_current_wave(self):
        world = _make_world(current_wave=1, tasks={
            "t1": TaskWorldState(id="t1", status="pending", wave=1),
            "t2": TaskWorldState(id="t2", status="running", wave=1),
            "t3": TaskWorldState(id="t3", status="pending", wave=2),
        })
        ready = detect_tasks_ready(world)
        assert ready == ["t1"]

    def test_ignores_other_waves(self):
        world = _make_world(current_wave=0, tasks={
            "t1": TaskWorldState(id="t1", status="pending", wave=1),
        })
        assert detect_tasks_ready(world) == []

    def test_empty(self):
        world = _make_world(tasks={})
        assert detect_tasks_ready(world) == []

    def test_multiple_ready(self):
        world = _make_world(current_wave=0, tasks={
            "t1": TaskWorldState(id="t1", status="pending", wave=0),
            "t2": TaskWorldState(id="t2", status="pending", wave=0),
        })
        ready = detect_tasks_ready(world)
        assert set(ready) == {"t1", "t2"}

    def test_all_running_none_ready(self):
        world = _make_world(current_wave=0, tasks={
            "t1": TaskWorldState(id="t1", status="running", wave=0),
        })
        assert detect_tasks_ready(world) == []


class TestDetectWaveComplete:

    def test_all_completed(self):
        world = _make_world(current_wave=1, tasks={
            "t1": TaskWorldState(id="t1", status="completed", wave=1),
            "t2": TaskWorldState(id="t2", status="completed", wave=1),
        })
        assert detect_wave_complete(world) is True

    def test_mixed_terminal(self):
        world = _make_world(current_wave=1, tasks={
            "t1": TaskWorldState(id="t1", status="completed", wave=1),
            "t2": TaskWorldState(id="t2", status="failed", wave=1),
            "t3": TaskWorldState(id="t3", status="cancelled", wave=1),
        })
        assert detect_wave_complete(world) is True

    def test_still_running(self):
        world = _make_world(current_wave=1, tasks={
            "t1": TaskWorldState(id="t1", status="completed", wave=1),
            "t2": TaskWorldState(id="t2", status="running", wave=1),
        })
        assert detect_wave_complete(world) is False

    def test_no_tasks_in_wave(self):
        world = _make_world(current_wave=2, tasks={
            "t1": TaskWorldState(id="t1", status="completed", wave=1),
        })
        assert detect_wave_complete(world) is False

    def test_other_wave_tasks_ignored(self):
        world = _make_world(current_wave=0, tasks={
            "t1": TaskWorldState(id="t1", status="completed", wave=0),
            "t2": TaskWorldState(id="t2", status="running", wave=1),
        })
        assert detect_wave_complete(world) is True


class TestDetectProjectComplete:

    def test_all_completed(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed"),
            "t2": TaskWorldState(id="t2", status="completed"),
        })
        done, reason = detect_project_complete(world)
        assert done is True
        assert "2/2 succeeded" in reason

    def test_mixed_terminal(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed"),
            "t2": TaskWorldState(id="t2", status="failed"),
            "t3": TaskWorldState(id="t3", status="cancelled"),
        })
        done, reason = detect_project_complete(world)
        assert done is True
        assert "1/3 succeeded" in reason
        assert "1 failed" in reason
        assert "1 cancelled" in reason

    def test_not_done_if_running(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed"),
            "t2": TaskWorldState(id="t2", status="running"),
        })
        done, reason = detect_project_complete(world)
        assert done is False
        assert reason == ""

    def test_not_done_if_pending(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed"),
            "t2": TaskWorldState(id="t2", status="pending"),
        })
        done, _ = detect_project_complete(world)
        assert done is False

    def test_empty_tasks(self):
        done, _ = detect_project_complete(_make_world(tasks={}))
        assert done is False

    def test_all_failed_is_complete(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="failed"),
            "t2": TaskWorldState(id="t2", status="failed"),
        })
        done, reason = detect_project_complete(world)
        assert done is True
        assert "0/2 succeeded" in reason


class TestDetectDeadProject:

    def test_all_failed(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="failed"),
            "t2": TaskWorldState(id="t2", status="failed"),
        })
        assert detect_dead_project(world) is True

    def test_all_cancelled(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="cancelled"),
        })
        assert detect_dead_project(world) is True

    def test_mixed_failed_cancelled(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="failed"),
            "t2": TaskWorldState(id="t2", status="cancelled"),
        })
        assert detect_dead_project(world) is True

    def test_has_running_not_dead(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="running"),
            "t2": TaskWorldState(id="t2", status="failed"),
        })
        assert detect_dead_project(world) is False

    def test_has_pending_not_dead(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="pending"),
            "t2": TaskWorldState(id="t2", status="failed"),
        })
        assert detect_dead_project(world) is False

    def test_has_queued_not_dead(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="queued"),
            "t2": TaskWorldState(id="t2", status="failed"),
        })
        assert detect_dead_project(world) is False

    def test_all_succeeded_not_dead(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed"),
            "t2": TaskWorldState(id="t2", status="completed"),
        })
        assert detect_dead_project(world) is False

    def test_completed_plus_failed_not_dead_same_wave(self):
        """Mixed completed+failed in same wave — not dead since some succeeded."""
        world = _make_world(current_wave=0, tasks={
            "t1": TaskWorldState(id="t1", status="completed", wave=0),
            "t2": TaskWorldState(id="t2", status="failed", wave=0),
        })
        assert detect_dead_project(world) is False

    def test_current_wave_failed_with_future_waves_is_dead(self):
        """Current wave all failed, future waves pending — dead (blocked)."""
        world = _make_world(current_wave=0, tasks={
            "t1": TaskWorldState(id="t1", status="failed", wave=0),
            "t2": TaskWorldState(id="t2", status="failed", wave=0),
            "t3": TaskWorldState(id="t3", status="completed", wave=1),
        })
        # All terminal, current wave all failed, future waves exist
        assert detect_dead_project(world) is True

    def test_empty_tasks(self):
        assert detect_dead_project(_make_world(tasks={})) is False


class TestDetectHollowCompletions:

    def test_empty_output(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed", output_summary=""),
        })
        assert detect_hollow_completions(world) == ["t1"]

    def test_whitespace_only(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed", output_summary="  \t\n "),
        })
        assert detect_hollow_completions(world) == ["t1"]

    def test_with_output(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed", output_summary="done"),
        })
        assert detect_hollow_completions(world) == []

    def test_non_completed_ignored(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="failed", output_summary=""),
            "t2": TaskWorldState(id="t2", status="pending", output_summary=""),
        })
        assert detect_hollow_completions(world) == []

    def test_multiple_hollow(self):
        world = _make_world(tasks={
            "t1": TaskWorldState(id="t1", status="completed", output_summary=""),
            "t2": TaskWorldState(id="t2", status="completed", output_summary=""),
        })
        result = detect_hollow_completions(world)
        assert set(result) == {"t1", "t2"}
