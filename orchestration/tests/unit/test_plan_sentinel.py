#  Plan Sentinel — Unit Tests
#
#  Tests the PlanSentinel state tracking, 4 detection rules,
#  auto/supervised interventions, and observation publishing.
#  All external dependencies (SSE stream, bus, context client, HTTP)
#  are mocked.

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure all async tests are picked up by anyio/pytest-asyncio
pytestmark = pytest.mark.anyio

from backend.services.sentinel.models import (
    SentinelMessage,
    SentinelObservation,
    Severity,
)
from backend.services.sentinel.plan_sentinel import (
    InterventionAction,
    InterventionTier,
    PlanSentinel,
    PlanState,
    TaskState,
    TaskTiming,
    MAX_AUTO_RETRIES,
    _CATEGORY_TO_INTERVENTION,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.publish = AsyncMock(return_value=1)
    return bus


@pytest.fixture
def mock_context_client():
    client = AsyncMock()
    client.save_observation = AsyncMock(return_value="obs-node-id")
    return client


@pytest.fixture
def sentinel(mock_bus, mock_context_client):
    """A PlanSentinel wired with mocked bus and context client."""
    return PlanSentinel(
        project_id="proj-001",
        bus=mock_bus,
        context_client=mock_context_client,
        base_url="http://test:5200",
    )


# ---------------------------------------------------------------------------
# State tracking via _handle_event
# ---------------------------------------------------------------------------

class TestStateTracking:
    """Verify internal state is updated correctly from SSE events."""

    async def test_task_start_sets_running(self, sentinel: PlanSentinel):
        await sentinel._handle_event({
            "type": "task_start",
            "task_id": "t1",
            "timestamp": 1000.0,
        })
        assert sentinel.state.task_statuses["t1"] == TaskState.RUNNING
        assert sentinel.state.task_timing["t1"].started_at == 1000.0
        assert sentinel.state.task_timing["t1"].last_progress_at == 1000.0
        assert sentinel.state.events_processed == 1

    async def test_task_complete_sets_completed(self, sentinel: PlanSentinel):
        sentinel._state.current_wave = 1
        await sentinel._handle_event({
            "type": "task_start",
            "task_id": "t1",
            "timestamp": 1000.0,
        })
        await sentinel._handle_event({
            "type": "task_complete",
            "task_id": "t1",
            "timestamp": 1050.0,
        })
        assert sentinel.state.task_statuses["t1"] == TaskState.COMPLETED
        assert sentinel.state.task_timing["t1"].completed_at == 1050.0
        # Failure count should be cleared on success
        assert "t1" not in sentinel.state.failure_counts
        # Wave outcomes tracked
        assert ("t1", "completed") in sentinel.state.wave_outcomes[1]

    async def test_task_failed_increments_failure_count(self, sentinel: PlanSentinel):
        sentinel._state.current_wave = 1
        await sentinel._handle_event({
            "type": "task_failed",
            "task_id": "t1",
            "timestamp": 1000.0,
        })
        assert sentinel.state.task_statuses["t1"] == TaskState.FAILED
        assert sentinel.state.failure_counts["t1"] == 1
        assert ("t1", "failed") in sentinel.state.wave_outcomes[1]

        # Second failure increments
        await sentinel._handle_event({
            "type": "task_failed",
            "task_id": "t1",
            "timestamp": 1010.0,
        })
        assert sentinel.state.failure_counts["t1"] == 2

    async def test_task_output_updates_progress_time(self, sentinel: PlanSentinel):
        await sentinel._handle_event({
            "type": "task_start",
            "task_id": "t1",
            "timestamp": 1000.0,
        })
        await sentinel._handle_event({
            "type": "task_output",
            "task_id": "t1",
            "timestamp": 1100.0,
        })
        assert sentinel.state.task_timing["t1"].last_progress_at == 1100.0

    async def test_wave_checkpoint_updates_current_wave(self, sentinel: PlanSentinel):
        await sentinel._handle_event({
            "type": "wave_checkpoint",
            "wave": 1,
            "next_wave": 2,
            "timestamp": 1000.0,
        })
        assert sentinel.state.current_wave == 2

    async def test_wave_checkpoint_fallback_to_wave(self, sentinel: PlanSentinel):
        await sentinel._handle_event({
            "type": "wave_checkpoint",
            "wave": 3,
            "timestamp": 1000.0,
        })
        assert sentinel.state.current_wave == 3

    async def test_budget_warning_event_updates_state(self, sentinel: PlanSentinel):
        await sentinel._handle_event({
            "type": "budget_warning",
            "spent": 85.0,
            "limit": 100.0,
            "timestamp": 1000.0,
        })
        assert sentinel.state.budget_spent == 85.0
        assert sentinel.state.budget_limit == 100.0

    async def test_budget_warning_alternate_keys(self, sentinel: PlanSentinel):
        await sentinel._handle_event({
            "type": "budget_warning",
            "total_spent": 90.0,
            "budget_limit": 100.0,
            "timestamp": 1000.0,
        })
        assert sentinel.state.budget_spent == 90.0
        assert sentinel.state.budget_limit == 100.0

    async def test_task_needs_review(self, sentinel: PlanSentinel):
        await sentinel._handle_event({
            "type": "task_needs_review",
            "task_id": "t1",
            "timestamp": 1000.0,
        })
        assert sentinel.state.task_statuses["t1"] == TaskState.NEEDS_REVIEW

    async def test_task_zombie_recovered(self, sentinel: PlanSentinel):
        await sentinel._handle_event({
            "type": "task_start",
            "task_id": "t1",
            "timestamp": 1000.0,
        })
        await sentinel._handle_event({
            "type": "task_zombie_recovered",
            "task_id": "t1",
            "timestamp": 1500.0,
        })
        assert sentinel.state.task_statuses["t1"] == TaskState.PENDING
        assert sentinel.state.task_timing["t1"].started_at is None

    async def test_unknown_event_type_is_ignored(self, sentinel: PlanSentinel):
        await sentinel._handle_event({
            "type": "unknown_event",
            "task_id": "t1",
            "timestamp": 1000.0,
        })
        assert sentinel.state.events_processed == 1
        assert "t1" not in sentinel.state.task_statuses

    async def test_events_without_task_id_ignored_gracefully(self, sentinel: PlanSentinel):
        # task_start with no task_id should not crash
        await sentinel._handle_event({
            "type": "task_start",
            "timestamp": 1000.0,
        })
        assert sentinel.state.events_processed == 1
        assert len(sentinel.state.task_statuses) == 0


# ---------------------------------------------------------------------------
# Rule: task_stuck
# ---------------------------------------------------------------------------

class TestRuleTaskStuck:
    """Detect tasks running > threshold with no progress."""

    async def test_stuck_task_detected(self, sentinel: PlanSentinel):
        sentinel.STUCK_THRESHOLD_SECS = 60.0
        sentinel._state.task_statuses["t1"] = TaskState.RUNNING
        sentinel._state.task_timing["t1"] = TaskTiming(
            started_at=100.0,
            last_progress_at=time.time() - 120,  # 120s ago
        )
        obs = sentinel._rule_task_stuck()
        assert len(obs) == 1
        assert obs[0].category == "task_stuck"
        assert obs[0].severity == Severity.WARNING
        assert obs[0].task_id == "t1"
        assert obs[0].project_id == "proj-001"

    async def test_active_task_not_flagged(self, sentinel: PlanSentinel):
        sentinel.STUCK_THRESHOLD_SECS = 60.0
        sentinel._state.task_statuses["t1"] = TaskState.RUNNING
        sentinel._state.task_timing["t1"] = TaskTiming(
            started_at=100.0,
            last_progress_at=time.time() - 10,  # 10s ago, within threshold
        )
        obs = sentinel._rule_task_stuck()
        assert len(obs) == 0

    async def test_completed_task_not_flagged(self, sentinel: PlanSentinel):
        sentinel.STUCK_THRESHOLD_SECS = 60.0
        sentinel._state.task_statuses["t1"] = TaskState.COMPLETED
        sentinel._state.task_timing["t1"] = TaskTiming(
            started_at=100.0,
            last_progress_at=time.time() - 120,
        )
        obs = sentinel._rule_task_stuck()
        assert len(obs) == 0

    async def test_multiple_stuck_tasks(self, sentinel: PlanSentinel):
        sentinel.STUCK_THRESHOLD_SECS = 60.0
        old_time = time.time() - 120
        for tid in ("t1", "t2", "t3"):
            sentinel._state.task_statuses[tid] = TaskState.RUNNING
            sentinel._state.task_timing[tid] = TaskTiming(
                started_at=100.0,
                last_progress_at=old_time,
            )
        obs = sentinel._rule_task_stuck()
        assert len(obs) == 3


# ---------------------------------------------------------------------------
# Rule: wave_stalled
# ---------------------------------------------------------------------------

class TestRuleWaveStalled:
    """Detect when all tasks in current wave are failed or stuck."""

    async def test_all_failed_wave(self, sentinel: PlanSentinel):
        sentinel._state.current_wave = 1
        sentinel._state.task_statuses = {
            "t1": TaskState.FAILED,
            "t2": TaskState.FAILED,
        }
        obs = sentinel._rule_wave_stalled()
        assert len(obs) == 1
        assert obs[0].category == "wave_stalled"
        assert obs[0].severity == Severity.CRITICAL

    async def test_mixed_failed_and_stuck(self, sentinel: PlanSentinel):
        sentinel.STUCK_THRESHOLD_SECS = 60.0
        sentinel._state.current_wave = 1
        old_time = time.time() - 120
        sentinel._state.task_statuses = {
            "t1": TaskState.FAILED,
            "t2": TaskState.RUNNING,
        }
        sentinel._state.task_timing["t2"] = TaskTiming(
            started_at=100.0,
            last_progress_at=old_time,
        )
        obs = sentinel._rule_wave_stalled()
        assert len(obs) == 1
        assert obs[0].category == "wave_stalled"

    async def test_wave_not_stalled_when_task_progressing(self, sentinel: PlanSentinel):
        sentinel.STUCK_THRESHOLD_SECS = 60.0
        sentinel._state.current_wave = 1
        sentinel._state.task_statuses = {
            "t1": TaskState.FAILED,
            "t2": TaskState.RUNNING,
        }
        sentinel._state.task_timing["t2"] = TaskTiming(
            started_at=100.0,
            last_progress_at=time.time() - 5,  # recent progress
        )
        obs = sentinel._rule_wave_stalled()
        assert len(obs) == 0

    async def test_no_wave_returns_empty(self, sentinel: PlanSentinel):
        sentinel._state.current_wave = None
        obs = sentinel._rule_wave_stalled()
        assert len(obs) == 0

    async def test_pending_tasks_not_stalled(self, sentinel: PlanSentinel):
        sentinel._state.current_wave = 1
        sentinel._state.task_statuses = {
            "t1": TaskState.FAILED,
            "t2": TaskState.PENDING,
        }
        obs = sentinel._rule_wave_stalled()
        assert len(obs) == 0


# ---------------------------------------------------------------------------
# Rule: cascade_failure
# ---------------------------------------------------------------------------

class TestRuleCascadeFailure:
    """Detect 3+ consecutive task failures in the same wave."""

    async def test_three_consecutive_failures(self, sentinel: PlanSentinel):
        sentinel._state.wave_outcomes[1] = [
            ("t1", "failed"),
            ("t2", "failed"),
            ("t3", "failed"),
        ]
        obs = sentinel._rule_cascade_failure()
        assert len(obs) == 1
        assert obs[0].category == "cascade_failure"
        assert obs[0].severity == Severity.CRITICAL
        assert obs[0].details["consecutive_failure_count"] == 3

    async def test_success_breaks_run(self, sentinel: PlanSentinel):
        sentinel._state.wave_outcomes[1] = [
            ("t1", "failed"),
            ("t2", "completed"),  # resets
            ("t3", "failed"),
            ("t4", "failed"),
        ]
        obs = sentinel._rule_cascade_failure()
        assert len(obs) == 0

    async def test_two_failures_not_enough(self, sentinel: PlanSentinel):
        sentinel._state.wave_outcomes[1] = [
            ("t1", "failed"),
            ("t2", "failed"),
        ]
        obs = sentinel._rule_cascade_failure()
        assert len(obs) == 0

    async def test_dedup_prevents_repeat_observation(self, sentinel: PlanSentinel):
        sentinel._state.wave_outcomes[1] = [
            ("t1", "failed"),
            ("t2", "failed"),
            ("t3", "failed"),
        ]
        obs1 = sentinel._rule_cascade_failure()
        assert len(obs1) == 1
        # Second call should be suppressed by emitted_observations
        obs2 = sentinel._rule_cascade_failure()
        assert len(obs2) == 0

    async def test_multiple_waves_independent(self, sentinel: PlanSentinel):
        sentinel._state.wave_outcomes[1] = [
            ("t1", "failed"), ("t2", "failed"), ("t3", "failed"),
        ]
        sentinel._state.wave_outcomes[2] = [
            ("t4", "failed"), ("t5", "failed"), ("t6", "failed"),
        ]
        obs = sentinel._rule_cascade_failure()
        assert len(obs) == 2


# ---------------------------------------------------------------------------
# Rule: budget_warning
# ---------------------------------------------------------------------------

class TestRuleBudgetWarning:
    """Detect budget usage exceeding 80%."""

    async def test_over_budget_threshold(self, sentinel: PlanSentinel):
        sentinel._state.budget_spent = 85.0
        sentinel._state.budget_limit = 100.0
        obs = sentinel._rule_budget_warning()
        assert len(obs) == 1
        assert obs[0].category == "budget_warning"
        assert obs[0].severity == Severity.WARNING
        assert obs[0].details["usage_percent"] == 85.0

    async def test_under_budget_threshold(self, sentinel: PlanSentinel):
        sentinel._state.budget_spent = 50.0
        sentinel._state.budget_limit = 100.0
        obs = sentinel._rule_budget_warning()
        assert len(obs) == 0

    async def test_no_budget_limit(self, sentinel: PlanSentinel):
        sentinel._state.budget_spent = 100.0
        sentinel._state.budget_limit = 0.0
        obs = sentinel._rule_budget_warning()
        assert len(obs) == 0

    async def test_exactly_at_threshold(self, sentinel: PlanSentinel):
        sentinel._state.budget_spent = 80.0
        sentinel._state.budget_limit = 100.0
        obs = sentinel._rule_budget_warning()
        assert len(obs) == 1

    async def test_budget_dedup(self, sentinel: PlanSentinel):
        sentinel._state.budget_spent = 90.0
        sentinel._state.budget_limit = 100.0
        obs1 = sentinel._rule_budget_warning()
        assert len(obs1) == 1
        obs2 = sentinel._rule_budget_warning()
        assert len(obs2) == 0


# ---------------------------------------------------------------------------
# Evaluate all rules together
# ---------------------------------------------------------------------------

class TestEvaluateRules:
    """_evaluate_rules runs all 4 rules and aggregates results."""

    async def test_multiple_rules_fire(self, sentinel: PlanSentinel):
        sentinel.STUCK_THRESHOLD_SECS = 60.0
        old_time = time.time() - 120
        # Stuck task
        sentinel._state.task_statuses["t1"] = TaskState.RUNNING
        sentinel._state.task_timing["t1"] = TaskTiming(
            started_at=100.0, last_progress_at=old_time,
        )
        # Budget over threshold
        sentinel._state.budget_spent = 90.0
        sentinel._state.budget_limit = 100.0

        obs = sentinel._evaluate_rules()
        categories = {o.category for o in obs}
        assert "task_stuck" in categories
        assert "budget_warning" in categories


# ---------------------------------------------------------------------------
# Observation publishing
# ---------------------------------------------------------------------------

class TestPublishObservation:
    """Observations are published to bus and persisted via context client."""

    async def test_publishes_stall_notification(self, sentinel, mock_bus, mock_context_client):
        obs = SentinelObservation(
            category="task_stuck",
            message="Task stuck",
            severity=Severity.WARNING,
            project_id="proj-001",
            task_id="t1",
        )
        await sentinel._publish_observation(obs)
        mock_bus.publish.assert_called_once()
        msg = mock_bus.publish.call_args[0][0]
        assert msg.topic == "stall_notification"
        assert msg.payload["category"] == "task_stuck"
        mock_context_client.save_observation.assert_called_once_with(obs)

    async def test_budget_warning_uses_resource_alert_topic(
        self, sentinel, mock_bus, mock_context_client,
    ):
        obs = SentinelObservation(
            category="budget_warning",
            message="Budget high",
            severity=Severity.WARNING,
            project_id="proj-001",
        )
        await sentinel._publish_observation(obs)
        msg = mock_bus.publish.call_args[0][0]
        assert msg.topic == "resource_alert"

    async def test_context_client_none_skips_persist(self, mock_bus):
        s = PlanSentinel("proj-001", mock_bus, context_client=None)
        obs = SentinelObservation(
            category="task_stuck",
            message="Test",
            severity=Severity.WARNING,
            project_id="proj-001",
        )
        # Should not raise
        await s._publish_observation(obs)
        mock_bus.publish.assert_called_once()


# ---------------------------------------------------------------------------
# Intervention handling — auto tier
# ---------------------------------------------------------------------------

class TestAutoInterventions:
    """Auto-tier interventions execute without approval."""

    async def test_task_stuck_triggers_release_claim(self, sentinel, mock_bus):
        obs = SentinelObservation(
            category="task_stuck",
            message="Task stuck",
            severity=Severity.WARNING,
            project_id="proj-001",
            task_id="t1",
        )
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=MagicMock(status_code=200))
        sentinel._http_client = mock_http

        await sentinel._handle_intervention(obs)

        # Should call release endpoint
        mock_http.post.assert_called_once()
        call_url = mock_http.post.call_args[0][0]
        assert "/release" in call_url
        # Should publish intervention_executed to bus
        assert mock_bus.publish.call_count >= 1
        last_msg = mock_bus.publish.call_args[0][0]
        assert last_msg.payload["action"] == "release_claim"
        assert last_msg.payload["tier"] == "auto"
        assert last_msg.payload["success"] is True

    async def test_cascade_failure_auto_retries_when_under_limit(self, sentinel, mock_bus):
        obs = SentinelObservation(
            category="cascade_failure",
            message="Cascade",
            severity=Severity.CRITICAL,
            project_id="proj-001",
            details={
                "failed_task_ids": ["t1", "t2", "t3"],
                "wave": 1,
            },
        )
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=MagicMock(status_code=200))
        sentinel._http_client = mock_http

        # No retries yet — should auto-retry the last failed task
        await sentinel._handle_intervention(obs)

        call_url = mock_http.post.call_args[0][0]
        assert "/retry" in call_url
        assert sentinel.state.retry_counts["t3"] == 1

    async def test_retry_limit_respected(self, sentinel, mock_bus):
        sentinel._state.retry_counts["t3"] = MAX_AUTO_RETRIES  # exhausted

        obs = SentinelObservation(
            category="cascade_failure",
            message="Cascade",
            severity=Severity.CRITICAL,
            project_id="proj-001",
            details={
                "failed_task_ids": ["t1", "t2", "t3"],
                "wave": 1,
            },
        )
        # When retries exhausted, should fall through to supervised (skip_task)
        await sentinel._handle_intervention(obs)

        # Should publish an intervention_proposal (supervised)
        assert mock_bus.publish.call_count >= 1
        last_msg = mock_bus.publish.call_args[0][0]
        assert last_msg.payload["action"] == "skip_task"
        assert last_msg.payload["tier"] == "supervised"

    async def test_release_claim_falls_back_to_retry(self, sentinel, mock_bus):
        obs = SentinelObservation(
            category="task_stuck",
            message="Stuck",
            severity=Severity.WARNING,
            project_id="proj-001",
            task_id="t1",
        )
        # Simulate release endpoint failing, then retry succeeding
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=[
            MagicMock(status_code=404, text="not found"),  # release fails
            MagicMock(status_code=200),  # retry succeeds
        ])
        sentinel._http_client = mock_http

        await sentinel._handle_intervention(obs)

        assert mock_http.post.call_count == 2
        assert sentinel.state.retry_counts["t1"] == 1


# ---------------------------------------------------------------------------
# Intervention handling — supervised tier
# ---------------------------------------------------------------------------

class TestSupervisedInterventions:
    """Supervised-tier interventions publish proposals for user approval."""

    async def test_wave_stalled_proposes_reorder(self, sentinel, mock_bus):
        obs = SentinelObservation(
            category="wave_stalled",
            message="Wave stalled",
            severity=Severity.CRITICAL,
            project_id="proj-001",
            details={
                "wave": 1,
                "task_ids": ["t1", "t2"],
            },
        )
        await sentinel._handle_intervention(obs)

        assert mock_bus.publish.call_count >= 1
        msg = mock_bus.publish.call_args[0][0]
        assert msg.payload["action"] == "reorder_wave"
        assert msg.payload["tier"] == "supervised"
        assert msg.payload["type"] == "intervention_proposal"
        assert "recommendation" in msg.payload

    async def test_budget_warning_has_no_intervention(self, sentinel, mock_bus):
        obs = SentinelObservation(
            category="budget_warning",
            message="Budget high",
            severity=Severity.WARNING,
            project_id="proj-001",
            details={"budget_spent": 85, "budget_limit": 100},
        )
        await sentinel._handle_intervention(obs)
        # No intervention for budget_warning — bus.publish should NOT be called
        mock_bus.publish.assert_not_called()

    async def test_intervention_dedup(self, sentinel, mock_bus):
        obs = SentinelObservation(
            category="wave_stalled",
            message="Wave stalled",
            severity=Severity.CRITICAL,
            project_id="proj-001",
            details={"wave": 1, "task_ids": ["t1"]},
        )
        await sentinel._handle_intervention(obs)
        call_count = mock_bus.publish.call_count
        # Second call with same category/target should be deduped
        await sentinel._handle_intervention(obs)
        assert mock_bus.publish.call_count == call_count


# ---------------------------------------------------------------------------
# Tick (end-to-end rule evaluation + publish + intervention)
# ---------------------------------------------------------------------------

class TestTick:
    """_tick runs detection, publishes observations, and handles interventions."""

    async def test_tick_publishes_and_intervenes(self, sentinel, mock_bus, mock_context_client):
        sentinel.STUCK_THRESHOLD_SECS = 60.0
        sentinel._state.task_statuses["t1"] = TaskState.RUNNING
        sentinel._state.task_timing["t1"] = TaskTiming(
            started_at=100.0,
            last_progress_at=time.time() - 120,
        )
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=MagicMock(status_code=200))
        sentinel._http_client = mock_http

        await sentinel._tick()

        # Should have published observation + intervention result
        assert mock_bus.publish.call_count >= 2
        mock_context_client.save_observation.assert_called_once()

    async def test_tick_no_observations_when_healthy(self, sentinel, mock_bus, mock_context_client):
        # No tasks, no budget issues → no observations
        await sentinel._tick()
        mock_bus.publish.assert_not_called()
        mock_context_client.save_observation.assert_not_called()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    async def test_start_stop(self, sentinel):
        assert not sentinel.running
        # Mock the run loop so it doesn't actually connect
        sentinel._run_loop = AsyncMock()
        await sentinel.start()
        assert sentinel.running

        await sentinel.stop()
        assert not sentinel.running

    async def test_start_is_idempotent(self, sentinel):
        sentinel._run_loop = AsyncMock()
        await sentinel.start()
        task1 = sentinel._task
        await sentinel.start()
        assert sentinel._task is task1  # same task, not replaced

    async def test_properties(self, sentinel):
        assert sentinel.project_id == "proj-001"
        assert isinstance(sentinel.state, PlanState)


# ---------------------------------------------------------------------------
# Category → intervention mapping
# ---------------------------------------------------------------------------

class TestInterventionMapping:
    def test_task_stuck_maps_to_release_claim_auto(self):
        action, tier = _CATEGORY_TO_INTERVENTION["task_stuck"]
        assert action == InterventionAction.RELEASE_CLAIM
        assert tier == InterventionTier.AUTO

    def test_cascade_failure_maps_to_skip_supervised(self):
        action, tier = _CATEGORY_TO_INTERVENTION["cascade_failure"]
        assert action == InterventionAction.SKIP_TASK
        assert tier == InterventionTier.SUPERVISED

    def test_wave_stalled_maps_to_reorder_supervised(self):
        action, tier = _CATEGORY_TO_INTERVENTION["wave_stalled"]
        assert action == InterventionAction.REORDER_WAVE
        assert tier == InterventionTier.SUPERVISED

    def test_budget_warning_has_no_mapping(self):
        assert "budget_warning" not in _CATEGORY_TO_INTERVENTION
