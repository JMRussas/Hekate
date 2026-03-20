"""Conductor-style task definitions.

Each task in a plan gets configurable:
  - Retry: count, logic (fixed/exponential/linear), delay, backoff rate
  - Timeout: policy (retry/timeout_wf/alert), seconds, response timeout
  - Rate limiting: per frequency, frequency window
  - Concurrency: max concurrent executions
  - Human: is_human flag, human response timeout

Based on Netflix Conductor task definition spec.
See: https://conductor-oss.github.io/conductor/documentation/configuration/taskdef.html
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RetryLogic(enum.Enum):
    FIXED = "FIXED"
    EXPONENTIAL_BACKOFF = "EXPONENTIAL_BACKOFF"
    LINEAR_BACKOFF = "LINEAR_BACKOFF"


class TimeoutPolicy(enum.Enum):
    RETRY = "RETRY"
    TIME_OUT_WF = "TIME_OUT_WF"
    ALERT_ONLY = "ALERT_ONLY"


# ---------------------------------------------------------------------------
# TaskDefinition
# ---------------------------------------------------------------------------

MAX_RETRY_DELAY = 3600  # 1 hour cap


@dataclass
class TaskDefinition:
    """Conductor-compatible task execution policy."""

    # Retry
    retry_count: int = 3
    retry_logic: RetryLogic = RetryLogic.FIXED
    retry_delay_seconds: int = 60
    backoff_rate: float = 2.0

    # Timeout
    timeout_policy: TimeoutPolicy = TimeoutPolicy.RETRY
    timeout_seconds: int = 600
    response_timeout_seconds: int = 600  # heartbeat — reschedule if no update

    # Rate limiting
    rate_limit_per_frequency: int | None = None
    rate_limit_frequency_seconds: int | None = None

    # Concurrency
    concurrent_exec_limit: int | None = None

    # Human task
    is_human: bool = False
    human_timeout_seconds: int = 86400  # 24 hours default

    def compute_retry_delay(self, attempt: int) -> int:
        """Compute delay before next retry based on logic and attempt number."""
        if self.retry_logic == RetryLogic.FIXED:
            delay = self.retry_delay_seconds

        elif self.retry_logic == RetryLogic.EXPONENTIAL_BACKOFF:
            delay = self.retry_delay_seconds * (2 ** attempt)

        elif self.retry_logic == RetryLogic.LINEAR_BACKOFF:
            delay = int(self.retry_delay_seconds * self.backoff_rate * max(attempt, 1))

        else:
            delay = self.retry_delay_seconds

        return min(delay, MAX_RETRY_DELAY)

    @classmethod
    def from_dict(cls, data: dict | None) -> TaskDefinition:
        """Parse from dict. Unknown fields ignored. None returns defaults."""
        if not data or not isinstance(data, dict):
            return cls()

        kwargs: dict[str, Any] = {}

        if "retry_count" in data:
            kwargs["retry_count"] = int(data["retry_count"])
        if "retry_logic" in data:
            try:
                kwargs["retry_logic"] = RetryLogic(data["retry_logic"])
            except ValueError:
                pass
        if "retry_delay_seconds" in data:
            kwargs["retry_delay_seconds"] = int(data["retry_delay_seconds"])
        if "backoff_rate" in data:
            kwargs["backoff_rate"] = float(data["backoff_rate"])

        if "timeout_policy" in data:
            try:
                kwargs["timeout_policy"] = TimeoutPolicy(data["timeout_policy"])
            except ValueError:
                pass
        if "timeout_seconds" in data:
            kwargs["timeout_seconds"] = int(data["timeout_seconds"])
        if "response_timeout_seconds" in data:
            kwargs["response_timeout_seconds"] = int(data["response_timeout_seconds"])

        if "rate_limit_per_frequency" in data:
            kwargs["rate_limit_per_frequency"] = int(data["rate_limit_per_frequency"])
        if "rate_limit_frequency_seconds" in data:
            kwargs["rate_limit_frequency_seconds"] = int(data["rate_limit_frequency_seconds"])
        if "concurrent_exec_limit" in data:
            kwargs["concurrent_exec_limit"] = int(data["concurrent_exec_limit"])

        if "is_human" in data:
            kwargs["is_human"] = bool(data["is_human"])
        if "human_timeout_seconds" in data:
            kwargs["human_timeout_seconds"] = int(data["human_timeout_seconds"])

        return cls(**kwargs)

    def to_dict(self) -> dict:
        """Serialize to dict with enum values as strings."""
        d = asdict(self)
        d["retry_logic"] = self.retry_logic.value
        d["timeout_policy"] = self.timeout_policy.value
        return d


# ---------------------------------------------------------------------------
# Default policies by task type
# ---------------------------------------------------------------------------

def apply_defaults(
    task_type: str = "code",
    complexity: str = "medium",
) -> TaskDefinition:
    """Generate sensible defaults based on task type and complexity."""

    if task_type == "human":
        return TaskDefinition(
            is_human=True,
            human_timeout_seconds=86400,
            retry_count=0,
            timeout_policy=TimeoutPolicy.ALERT_ONLY,
        )

    if task_type == "research":
        return TaskDefinition(
            retry_count=2,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=30,
            timeout_seconds=300,
            response_timeout_seconds=300,
        )

    if task_type == "test":
        return TaskDefinition(
            retry_count=2,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=30,
            timeout_seconds=300,
            response_timeout_seconds=300,
        )

    # Code tasks
    if complexity == "simple":
        return TaskDefinition(
            retry_count=3,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=30,
            timeout_seconds=300,
            response_timeout_seconds=300,
        )
    elif complexity == "complex":
        return TaskDefinition(
            retry_count=5,
            retry_logic=RetryLogic.EXPONENTIAL_BACKOFF,
            retry_delay_seconds=60,
            timeout_seconds=1200,
            response_timeout_seconds=600,
        )
    else:  # medium
        return TaskDefinition(
            retry_count=3,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=60,
            timeout_seconds=600,
            response_timeout_seconds=600,
        )


# ---------------------------------------------------------------------------
# Merge plan-level config with task-level overrides
# ---------------------------------------------------------------------------

def merge_with_plan_config(
    task_def: TaskDefinition,
    plan_config: dict | None = None,
) -> TaskDefinition:
    """Apply plan-level config as defaults, task-level overrides win.

    Plan config keys: max_retries, timeout_seconds, retry_delay_seconds
    """
    if not plan_config:
        return task_def

    import dataclasses

    overrides: dict[str, Any] = {}

    # Only apply plan config if task has the default value
    defaults = TaskDefinition()

    if "max_retries" in plan_config and task_def.retry_count == defaults.retry_count:
        overrides["retry_count"] = int(plan_config["max_retries"])
    if "timeout_seconds" in plan_config and task_def.timeout_seconds == defaults.timeout_seconds:
        overrides["timeout_seconds"] = int(plan_config["timeout_seconds"])
    if "retry_delay_seconds" in plan_config and task_def.retry_delay_seconds == defaults.retry_delay_seconds:
        overrides["retry_delay_seconds"] = int(plan_config["retry_delay_seconds"])

    if overrides:
        return dataclasses.replace(task_def, **overrides)
    return task_def
