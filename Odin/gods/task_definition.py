"""Conductor-style task definitions + task type registry.

Each task in a plan gets configurable:
  - Retry: count, logic (fixed/exponential/linear), delay, backoff rate
  - Timeout: policy (retry/timeout_wf/alert), seconds, response timeout
  - Rate limiting: per frequency, frequency window
  - Concurrency: max concurrent executions
  - Human: is_human flag, human response timeout

The TaskTypeRegistry holds typed definitions with provider preferences
and input/output schemas. Decomposer validates tasks against the registry.
Handlers read the stored TaskDefinition from context_json rather than
regenerating from scratch.

Based on Netflix Conductor task definition spec.
See: https://conductor-oss.github.io/conductor/documentation/configuration/taskdef.html
"""

from __future__ import annotations

import enum
import logging
import random
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger("gods.task_definition")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RetryLogic(enum.Enum):
    FIXED = "FIXED"
    EXPONENTIAL_BACKOFF = "EXPONENTIAL_BACKOFF"
    LINEAR_BACKOFF = "LINEAR_BACKOFF"


@dataclass
class RetryPolicy:
    """Standalone retry policy with delay computation.

    Encapsulates retry strategy, delays, and jitter into a reusable unit.
    Uses equal jitter: half + random(0, half) — guarantees >= 50% of computed
    delay while providing good de-correlation across retries.
    """

    strategy: RetryLogic = RetryLogic.EXPONENTIAL_BACKOFF
    base_delay_seconds: int = 30
    max_delay_seconds: int = 300
    jitter: bool = True
    backoff_rate: float = 2.0

    def compute_delay(self, attempt: int) -> float:
        """Compute delay for a retry attempt (0-indexed).

        attempt=0 is the first retry, matching the convention where
        2 ** 0 yields 1x on first retry.
        """
        if self.strategy == RetryLogic.FIXED:
            computed = float(self.base_delay_seconds)
        elif self.strategy == RetryLogic.EXPONENTIAL_BACKOFF:
            computed = self.base_delay_seconds * (self.backoff_rate ** attempt)
        elif self.strategy == RetryLogic.LINEAR_BACKOFF:
            computed = self.base_delay_seconds * self.backoff_rate * max(attempt, 1)
        else:
            computed = float(self.base_delay_seconds)

        computed = min(computed, self.max_delay_seconds)

        if self.jitter:
            half = computed / 2
            computed = half + random.uniform(0, half)

        return computed

    def to_dict(self) -> dict:
        """Serialize to dict with strategy as string value."""
        return {
            "strategy": self.strategy.value,
            "base_delay_seconds": self.base_delay_seconds,
            "max_delay_seconds": self.max_delay_seconds,
            "jitter": self.jitter,
            "backoff_rate": self.backoff_rate,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RetryPolicy:
        """Deserialize from dict. Gracefully handles invalid enum values."""
        if not data or not isinstance(data, dict):
            return cls()

        kwargs: dict[str, Any] = {}

        if "strategy" in data:
            try:
                kwargs["strategy"] = RetryLogic(data["strategy"])
            except ValueError:
                pass  # fall back to default
        if "base_delay_seconds" in data:
            kwargs["base_delay_seconds"] = int(data["base_delay_seconds"])
        if "max_delay_seconds" in data:
            kwargs["max_delay_seconds"] = int(data["max_delay_seconds"])
        if "jitter" in data:
            kwargs["jitter"] = bool(data["jitter"])
        if "backoff_rate" in data:
            kwargs["backoff_rate"] = float(data["backoff_rate"])

        return cls(**kwargs)


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
    retry_policy: RetryPolicy | None = None

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
        if self.retry_policy is not None:
            return int(self.retry_policy.compute_delay(attempt))

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

        if "rate_limit_per_frequency" in data and data["rate_limit_per_frequency"] is not None:
            kwargs["rate_limit_per_frequency"] = int(data["rate_limit_per_frequency"])
        if "rate_limit_frequency_seconds" in data and data["rate_limit_frequency_seconds"] is not None:
            kwargs["rate_limit_frequency_seconds"] = int(data["rate_limit_frequency_seconds"])
        if "concurrent_exec_limit" in data and data["concurrent_exec_limit"] is not None:
            kwargs["concurrent_exec_limit"] = int(data["concurrent_exec_limit"])

        if "is_human" in data:
            kwargs["is_human"] = bool(data["is_human"])
        if "human_timeout_seconds" in data:
            kwargs["human_timeout_seconds"] = int(data["human_timeout_seconds"])

        if "retry_policy" in data and isinstance(data["retry_policy"], dict):
            kwargs["retry_policy"] = RetryPolicy.from_dict(data["retry_policy"])

        return cls(**kwargs)

    def to_dict(self) -> dict:
        """Serialize to dict with enum values as strings."""
        d = asdict(self)
        d["retry_logic"] = self.retry_logic.value
        d["timeout_policy"] = self.timeout_policy.value
        if self.retry_policy is not None:
            d["retry_policy"] = self.retry_policy.to_dict()
        else:
            d.pop("retry_policy", None)
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

    _fixed_policy = RetryPolicy(
        strategy=RetryLogic.FIXED,
        base_delay_seconds=30,
        max_delay_seconds=120,
        jitter=True,
    )

    if task_type == "integration":
        return TaskDefinition(
            retry_count=5,
            retry_logic=RetryLogic.EXPONENTIAL_BACKOFF,
            retry_delay_seconds=60,
            timeout_seconds=1800,
            response_timeout_seconds=600,
            retry_policy=RetryPolicy(
                strategy=RetryLogic.EXPONENTIAL_BACKOFF,
                base_delay_seconds=60,
                max_delay_seconds=300,
                jitter=True,
                backoff_rate=2.0,
            ),
        )

    if task_type == "research":
        return TaskDefinition(
            retry_count=2,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=30,
            timeout_seconds=300,
            response_timeout_seconds=300,
            retry_policy=_fixed_policy,
        )

    if task_type == "test":
        return TaskDefinition(
            retry_count=2,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=30,
            timeout_seconds=300,
            response_timeout_seconds=300,
            retry_policy=_fixed_policy,
        )

    if task_type == "documentation":
        return TaskDefinition(
            retry_count=2,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=30,
            timeout_seconds=300,
            response_timeout_seconds=300,
            retry_policy=_fixed_policy,
        )

    # Code tasks
    _expo_policy = RetryPolicy(
        strategy=RetryLogic.EXPONENTIAL_BACKOFF,
        base_delay_seconds=30,
        max_delay_seconds=300,
        jitter=True,
        backoff_rate=2.0,
    )

    if complexity == "simple":
        return TaskDefinition(
            retry_count=3,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=30,
            timeout_seconds=300,
            response_timeout_seconds=300,
            retry_policy=_fixed_policy,
        )
    elif complexity == "complex":
        return TaskDefinition(
            retry_count=5,
            retry_logic=RetryLogic.EXPONENTIAL_BACKOFF,
            retry_delay_seconds=60,
            timeout_seconds=1800,
            response_timeout_seconds=600,
            retry_policy=_expo_policy,
        )
    else:  # medium
        return TaskDefinition(
            retry_count=3,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=60,
            timeout_seconds=600,
            response_timeout_seconds=600,
            retry_policy=_expo_policy,
        )


# ---------------------------------------------------------------------------
# Merge plan-level config with task-level overrides
# ---------------------------------------------------------------------------

def _is_default_retry_policy(
    task_def: TaskDefinition,
    task_type: str = "code",
    complexity: str = "medium",
) -> bool:
    """Check if the task's retry_policy matches the registry/apply_defaults output.

    Used to decide whether plan-level retry_policy should override. If the
    task was explicitly given a retry_policy (via context_json.task_definition),
    it won't match the default and plan-level config won't clobber it.
    """
    registry_def = get_registry().get_definition(task_type, complexity)

    # Both None → default
    if task_def.retry_policy is None and registry_def.retry_policy is None:
        return True
    # One None, other not → not default
    if task_def.retry_policy is None or registry_def.retry_policy is None:
        return False
    # Compare serialized form (avoids float equality issues with dataclass eq)
    return task_def.retry_policy.to_dict() == registry_def.retry_policy.to_dict()


def merge_with_plan_config(
    task_def: TaskDefinition,
    plan_config: dict | None = None,
    task_type: str = "code",
    complexity: str = "medium",
) -> TaskDefinition:
    """Apply plan-level config as overrides when task still has registry defaults.

    Precedence (lowest → highest):
      1. Registry default — from create_default_registry / apply_defaults
      2. Plan-level config — from plan_config dict passed here; applied only
         when the task's value still matches the registry default
      3. Task-level context_json.task_definition — parsed in from_dict before
         this function is called; always wins since it's explicitly set on
         the task and won't match the registry default

    Plan config keys: max_retries, timeout_seconds, retry_delay_seconds,
    retry_policy (dict).
    """
    if not plan_config:
        return task_def

    import dataclasses

    overrides: dict[str, Any] = {}

    # Compare against registry defaults for the task's type, not bare TaskDefinition()
    registry_def = get_registry().get_definition(task_type, complexity)

    if "max_retries" in plan_config and task_def.retry_count == registry_def.retry_count:
        overrides["retry_count"] = int(plan_config["max_retries"])
    if "timeout_seconds" in plan_config and task_def.timeout_seconds == registry_def.timeout_seconds:
        overrides["timeout_seconds"] = int(plan_config["timeout_seconds"])
    if "retry_delay_seconds" in plan_config and task_def.retry_delay_seconds == registry_def.retry_delay_seconds:
        overrides["retry_delay_seconds"] = int(plan_config["retry_delay_seconds"])

    # Plan-level retry_policy: apply only if task still has the registry default
    if "retry_policy" in plan_config and isinstance(plan_config["retry_policy"], dict):
        if _is_default_retry_policy(task_def, task_type, complexity):
            overrides["retry_policy"] = RetryPolicy.from_dict(plan_config["retry_policy"])

    if overrides:
        return dataclasses.replace(task_def, **overrides)
    return task_def


# ---------------------------------------------------------------------------
# Task Type Registry
# ---------------------------------------------------------------------------

@dataclass
class TaskTypeSpec:
    """Registered task type with execution policy and provider preferences.

    This is the "what" — what kind of task is this, how should it be executed.
    TaskDefinition is the "how" — retry counts, timeouts, etc.
    """
    name: str                               # e.g. "code", "research", "test"
    description: str = ""
    default_definition: TaskDefinition = field(default_factory=TaskDefinition)
    provider_preference: list[str] = field(default_factory=lambda: ["claude_code"])
    input_schema: dict[str, str] = field(default_factory=dict)   # field_name → type hint
    output_schema: dict[str, str] = field(default_factory=dict)  # field_name → type hint
    required_inputs: list[str] = field(default_factory=list)     # must be present in task

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "default_definition": self.default_definition.to_dict(),
            "provider_preference": self.provider_preference,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "required_inputs": self.required_inputs,
        }


class TaskTypeRegistry:
    """Registry of known task types.

    Provides validation, default policies, and provider preferences.
    """

    def __init__(self):
        self._types: dict[str, TaskTypeSpec] = {}

    def register(self, spec: TaskTypeSpec):
        """Register a task type."""
        self._types[spec.name] = spec

    def get(self, name: str) -> TaskTypeSpec | None:
        return self._types.get(name)

    def get_definition(self, task_type: str, complexity: str = "medium") -> TaskDefinition:
        """Get TaskDefinition for a task type. Falls back to apply_defaults."""
        spec = self._types.get(task_type)
        if spec:
            return spec.default_definition
        return apply_defaults(task_type, complexity)

    def get_providers(self, task_type: str) -> list[str]:
        """Get preferred providers for a task type."""
        spec = self._types.get(task_type)
        if spec:
            return spec.provider_preference
        return ["claude_code"]

    def validate_task(self, task: dict) -> list[str]:
        """Validate a task dict against its registered type.

        Returns list of validation errors (empty = valid).
        """
        errors: list[str] = []
        task_type = task.get("task_type", "code")
        spec = self._types.get(task_type)

        if not spec:
            # Unknown type — warn but don't block
            return []

        # Check required inputs
        for req in spec.required_inputs:
            if req not in task and req not in task.get("context_json", {}):
                errors.append(f"Missing required input '{req}' for task type '{task_type}'")

        # Check title/description
        if not task.get("title"):
            errors.append("Task missing title")
        if not task.get("description"):
            errors.append("Task missing description")

        return errors

    def to_snapshot(self) -> dict:
        """Serialize registry state for workflow versioning.

        Captured at project creation so in-flight projects use their
        original task type definitions even after deploys.
        """
        return {
            name: spec.to_dict()
            for name, spec in self._types.items()
        }

    @property
    def types(self) -> list[str]:
        return list(self._types.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._types

    def __len__(self) -> int:
        return len(self._types)


# ---------------------------------------------------------------------------
# Default registry — register all known task types
# ---------------------------------------------------------------------------

def create_default_registry() -> TaskTypeRegistry:
    """Create a registry with all built-in task types."""
    reg = TaskTypeRegistry()

    reg.register(TaskTypeSpec(
        name="code",
        description="Implementation task — write, modify, or refactor code",
        default_definition=apply_defaults("code", "medium"),
        provider_preference=["claude_code"],
        input_schema={"description": "str", "task_type": "str"},
        output_schema={"output_text": "str", "affected_files": "list[str]"},
        required_inputs=["description"],
    ))

    reg.register(TaskTypeSpec(
        name="research",
        description="Investigation task — analyze code, gather information, no writes",
        default_definition=apply_defaults("research"),
        provider_preference=["claude_code"],
        input_schema={"description": "str"},
        output_schema={"output_text": "str", "findings": "str"},
        required_inputs=["description"],
    ))

    reg.register(TaskTypeSpec(
        name="test",
        description="Testing task — write or run tests",
        default_definition=apply_defaults("test"),
        provider_preference=["claude_code"],
        input_schema={"description": "str"},
        output_schema={"output_text": "str", "test_results": "str"},
        required_inputs=["description"],
    ))

    reg.register(TaskTypeSpec(
        name="integration",
        description="Integration task — wire components together, update configs",
        default_definition=TaskDefinition(
            retry_count=3,
            retry_logic=RetryLogic.EXPONENTIAL_BACKOFF,
            retry_delay_seconds=60,
            timeout_seconds=900,
            response_timeout_seconds=600,
            retry_policy=RetryPolicy(
                strategy=RetryLogic.EXPONENTIAL_BACKOFF,
                base_delay_seconds=30,
                max_delay_seconds=300,
                jitter=True,
            ),
        ),
        provider_preference=["claude_code"],
        input_schema={"description": "str"},
        output_schema={"output_text": "str", "affected_files": "list[str]"},
        required_inputs=["description"],
    ))

    reg.register(TaskTypeSpec(
        name="documentation",
        description="Documentation task — write docs, comments, READMEs",
        default_definition=TaskDefinition(
            retry_count=2,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=30,
            timeout_seconds=300,
            response_timeout_seconds=300,
        ),
        provider_preference=["claude_code"],
        input_schema={"description": "str"},
        output_schema={"output_text": "str"},
        required_inputs=["description"],
    ))

    reg.register(TaskTypeSpec(
        name="analysis",
        description="Analysis task — security review, architecture assessment, code review",
        default_definition=TaskDefinition(
            retry_count=2,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=30,
            timeout_seconds=600,
            response_timeout_seconds=600,
            retry_policy=RetryPolicy(
                strategy=RetryLogic.EXPONENTIAL_BACKOFF,
                base_delay_seconds=30,
                max_delay_seconds=300,
                jitter=True,
            ),
        ),
        provider_preference=["claude_code"],
        input_schema={"description": "str"},
        output_schema={"output_text": "str", "findings": "str"},
        required_inputs=["description"],
    ))

    reg.register(TaskTypeSpec(
        name="asset",
        description="Asset generation — images, configs, data files",
        default_definition=TaskDefinition(
            retry_count=2,
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=30,
            timeout_seconds=300,
            response_timeout_seconds=300,
            retry_policy=RetryPolicy(
                strategy=RetryLogic.FIXED,
                base_delay_seconds=30,
                max_delay_seconds=120,
                jitter=True,
            ),
        ),
        provider_preference=["ollama", "claude_code"],
        input_schema={"description": "str"},
        output_schema={"output_text": "str"},
        required_inputs=["description"],
    ))

    reg.register(TaskTypeSpec(
        name="human",
        description="Human review task — requires manual intervention",
        default_definition=apply_defaults("human"),
        provider_preference=[],
        input_schema={"description": "str", "question": "str"},
        output_schema={"response": "str"},
        required_inputs=["description"],
    ))

    return reg


# Singleton — shared across the process
_default_registry: TaskTypeRegistry | None = None


def get_registry() -> TaskTypeRegistry:
    """Get the default task type registry (lazy singleton)."""
    global _default_registry
    if _default_registry is None:
        _default_registry = create_default_registry()
    return _default_registry


def load_task_definition(context_json: dict | str | None) -> TaskDefinition:
    """Load TaskDefinition from a task's context_json.

    Falls back to defaults if not present or unparseable.
    """
    if not context_json:
        return TaskDefinition()

    if isinstance(context_json, str):
        import json
        try:
            context_json = json.loads(context_json)
        except (json.JSONDecodeError, TypeError):
            return TaskDefinition()

    td_data = context_json.get("task_definition") if isinstance(context_json, dict) else None
    if td_data:
        return TaskDefinition.from_dict(td_data)
    return TaskDefinition()
