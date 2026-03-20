"""Tests for Conductor-style task definitions.

RED PHASE: TaskDefinition doesn't exist yet.

Each task in a plan gets Conductor-compatible configuration:
  - Retry: count, logic (fixed/exponential/linear), delay
  - Timeout: policy (retry/timeout_wf/alert), seconds, response timeout
  - Rate limiting: per frequency, frequency window
  - Concurrency: max concurrent executions
  - Human: is_human, timeout for human response
"""

import pytest
from gods.task_definition import (
    TaskDefinition,
    RetryLogic,
    TimeoutPolicy,
    apply_defaults,
    merge_with_plan_config,
)


# ---------------------------------------------------------------------------
# TaskDefinition basics
# ---------------------------------------------------------------------------

class TestTaskDefinition:
    def test_defaults(self):
        td = TaskDefinition()
        assert td.retry_count == 3
        assert td.retry_logic == RetryLogic.FIXED
        assert td.retry_delay_seconds == 60
        assert td.timeout_policy == TimeoutPolicy.RETRY
        assert td.timeout_seconds == 600
        assert td.response_timeout_seconds == 600
        assert td.concurrent_exec_limit is None
        assert td.rate_limit_per_frequency is None
        assert td.is_human is False

    def test_custom(self):
        td = TaskDefinition(
            retry_count=5,
            retry_logic=RetryLogic.EXPONENTIAL_BACKOFF,
            retry_delay_seconds=30,
            timeout_seconds=1200,
            concurrent_exec_limit=2,
        )
        assert td.retry_count == 5
        assert td.retry_logic == RetryLogic.EXPONENTIAL_BACKOFF
        assert td.retry_delay_seconds == 30
        assert td.timeout_seconds == 1200
        assert td.concurrent_exec_limit == 2

    def test_from_dict(self):
        td = TaskDefinition.from_dict({
            "retry_count": 2,
            "retry_logic": "EXPONENTIAL_BACKOFF",
            "timeout_policy": "ALERT_ONLY",
            "timeout_seconds": 300,
        })
        assert td.retry_count == 2
        assert td.retry_logic == RetryLogic.EXPONENTIAL_BACKOFF
        assert td.timeout_policy == TimeoutPolicy.ALERT_ONLY
        assert td.timeout_seconds == 300

    def test_from_dict_handles_unknown_fields(self):
        td = TaskDefinition.from_dict({
            "retry_count": 1,
            "unknown_field": "ignored",
        })
        assert td.retry_count == 1

    def test_from_dict_empty(self):
        td = TaskDefinition.from_dict({})
        assert td.retry_count == 3  # default

    def test_from_dict_none(self):
        td = TaskDefinition.from_dict(None)
        assert td.retry_count == 3  # default

    def test_to_dict(self):
        td = TaskDefinition(retry_count=5, retry_logic=RetryLogic.LINEAR_BACKOFF)
        d = td.to_dict()
        assert d["retry_count"] == 5
        assert d["retry_logic"] == "LINEAR_BACKOFF"
        assert "timeout_seconds" in d

    def test_human_task(self):
        td = TaskDefinition(is_human=True, human_timeout_seconds=3600)
        assert td.is_human is True
        assert td.human_timeout_seconds == 3600


# ---------------------------------------------------------------------------
# RetryLogic enum
# ---------------------------------------------------------------------------

class TestRetryLogic:
    def test_values(self):
        assert RetryLogic.FIXED.value == "FIXED"
        assert RetryLogic.EXPONENTIAL_BACKOFF.value == "EXPONENTIAL_BACKOFF"
        assert RetryLogic.LINEAR_BACKOFF.value == "LINEAR_BACKOFF"

    def test_from_string(self):
        assert RetryLogic("FIXED") == RetryLogic.FIXED
        assert RetryLogic("EXPONENTIAL_BACKOFF") == RetryLogic.EXPONENTIAL_BACKOFF

    def test_compute_delay_fixed(self):
        td = TaskDefinition(retry_logic=RetryLogic.FIXED, retry_delay_seconds=10)
        assert td.compute_retry_delay(attempt=1) == 10
        assert td.compute_retry_delay(attempt=3) == 10

    def test_compute_delay_exponential(self):
        td = TaskDefinition(retry_logic=RetryLogic.EXPONENTIAL_BACKOFF, retry_delay_seconds=10)
        assert td.compute_retry_delay(attempt=0) == 10   # 10 * 2^0
        assert td.compute_retry_delay(attempt=1) == 20   # 10 * 2^1
        assert td.compute_retry_delay(attempt=2) == 40   # 10 * 2^2
        assert td.compute_retry_delay(attempt=3) == 80   # 10 * 2^3

    def test_compute_delay_linear(self):
        td = TaskDefinition(
            retry_logic=RetryLogic.LINEAR_BACKOFF,
            retry_delay_seconds=10,
            backoff_rate=2,
        )
        assert td.compute_retry_delay(attempt=1) == 20   # 10 * 2 * 1
        assert td.compute_retry_delay(attempt=2) == 40   # 10 * 2 * 2
        assert td.compute_retry_delay(attempt=3) == 60   # 10 * 2 * 3

    def test_compute_delay_capped(self):
        """Delay should never exceed a reasonable maximum."""
        td = TaskDefinition(retry_logic=RetryLogic.EXPONENTIAL_BACKOFF, retry_delay_seconds=60)
        delay = td.compute_retry_delay(attempt=10)  # 60 * 2^10 = 61440
        assert delay <= 3600  # Cap at 1 hour


# ---------------------------------------------------------------------------
# TimeoutPolicy enum
# ---------------------------------------------------------------------------

class TestTimeoutPolicy:
    def test_values(self):
        assert TimeoutPolicy.RETRY.value == "RETRY"
        assert TimeoutPolicy.TIME_OUT_WF.value == "TIME_OUT_WF"
        assert TimeoutPolicy.ALERT_ONLY.value == "ALERT_ONLY"


# ---------------------------------------------------------------------------
# apply_defaults — task type based defaults
# ---------------------------------------------------------------------------

class TestApplyDefaults:
    def test_code_task_defaults(self):
        td = apply_defaults(task_type="code", complexity="medium")
        assert td.retry_count == 3
        assert td.timeout_seconds == 600

    def test_research_task_shorter_timeout(self):
        td = apply_defaults(task_type="research", complexity="simple")
        assert td.timeout_seconds <= 300

    def test_complex_task_more_retries(self):
        td = apply_defaults(task_type="code", complexity="complex")
        assert td.retry_count >= 3
        assert td.timeout_seconds >= 600

    def test_test_task_fast_timeout(self):
        td = apply_defaults(task_type="test", complexity="simple")
        assert td.timeout_seconds <= 300

    def test_human_task(self):
        td = apply_defaults(task_type="human")
        assert td.is_human is True
        assert td.human_timeout_seconds > 0


# ---------------------------------------------------------------------------
# merge_with_plan_config — plan-level overrides
# ---------------------------------------------------------------------------

class TestMergeWithPlanConfig:
    def test_plan_config_overrides_defaults(self):
        plan_config = {"max_retries": 5, "timeout_seconds": 1200}
        td = merge_with_plan_config(
            task_def=TaskDefinition(),
            plan_config=plan_config,
        )
        assert td.retry_count == 5
        assert td.timeout_seconds == 1200

    def test_task_specific_overrides_plan(self):
        """Task-level config takes priority over plan-level."""
        plan_config = {"max_retries": 5}
        task_override = {"retry_count": 1}
        td = merge_with_plan_config(
            task_def=TaskDefinition.from_dict(task_override),
            plan_config=plan_config,
        )
        # Task said 1, plan said 5 — task wins
        assert td.retry_count == 1

    def test_empty_plan_config(self):
        td = merge_with_plan_config(
            task_def=TaskDefinition(),
            plan_config={},
        )
        assert td.retry_count == 3  # default unchanged
