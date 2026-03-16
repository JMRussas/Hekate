#  Sentinel Models
#
#  Shared data structures for sentinel messages and observations.
#
#  Used by: bus.py, context_client.py, system_sentinel.py

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Intervention(Enum):
    NONE = "none"
    PAUSE = "pause"
    THROTTLE = "throttle"
    ESCALATE = "escalate"


@dataclass
class SentinelMessage:
    """Message routed through the SentinelBus."""

    topic: str
    source: str  # e.g. "system_sentinel", "plan_sentinel:<project_id>"
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


class HealthState(Enum):
    """Derived health state from trend analysis."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    INTERMITTENT = "intermittent"
    DOWN = "down"


@dataclass
class HealthSample:
    """Single health check data point."""
    timestamp: datetime
    online: bool
    response_time_ms: float | None = None


@dataclass
class HealthTrend:
    """Sliding-window trend tracker for a single resource."""
    resource_id: str
    window_size: int = 20  # number of samples to retain
    samples: deque[HealthSample] = field(default_factory=deque)
    state: HealthState = HealthState.HEALTHY
    previous_state: HealthState | None = None

    def __post_init__(self):
        # Re-wrap if deserialized from a plain list
        if not isinstance(self.samples, deque):
            self.samples = deque(self.samples, maxlen=self.window_size)
        else:
            self.samples = deque(self.samples, maxlen=self.window_size)

    def push(self, sample: HealthSample) -> None:
        self.samples.append(sample)

    @property
    def failure_rate(self) -> float:
        """Fraction of offline samples in the window (0.0–1.0)."""
        if not self.samples:
            return 0.0
        failures = sum(1 for s in self.samples if not s.online)
        return failures / len(self.samples)

    @property
    def recent_latencies(self) -> list[float]:
        """Non-None response times in chronological order."""
        return [s.response_time_ms for s in self.samples if s.response_time_ms is not None]

    @property
    def avg_latency_ms(self) -> float | None:
        lats = self.recent_latencies
        return sum(lats) / len(lats) if lats else None

    def latency_increasing(self, min_samples: int = 5) -> bool:
        """True if latency is trending upward across the window.

        Compares mean of first half vs second half of available latencies.
        """
        lats = self.recent_latencies
        if len(lats) < min_samples:
            return False
        mid = len(lats) // 2
        first_half = sum(lats[:mid]) / mid
        second_half = sum(lats[mid:]) / (len(lats) - mid)
        # Require ≥30% increase to flag as degrading
        return second_half > first_half * 1.3


class SentinelCommand(Enum):
    """Commands the sentinel can issue to the orchestration layer."""
    DISPATCH_TASK = "dispatch_task"
    CANCEL_TASK = "cancel_task"
    PAUSE_PROJECT = "pause_project"
    RESUME_PROJECT = "resume_project"
    ADVANCE_WAVE = "advance_wave"
    RETRY_TASK = "retry_task"
    REASSIGN_TIER = "reassign_tier"
    SKIP_TASK = "skip_task"


@dataclass
class DecisionRecord:
    """Record of a sentinel decision for audit and replay."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    project_id: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    command: SentinelCommand = SentinelCommand.DISPATCH_TASK
    reasoning: str = ""
    confidence: float = 1.0
    outcome: str = ""  # filled after execution

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "timestamp": self.timestamp.isoformat(),
            "command": self.command.value,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionRecord:
        return cls(
            id=data["id"],
            project_id=data["project_id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            command=SentinelCommand(data["command"]),
            reasoning=data["reasoning"],
            confidence=data["confidence"],
            outcome=data.get("outcome", ""),
        )


@dataclass
class TaskWorldState:
    """Snapshot of a single task's state within the world model."""
    id: str = ""
    status: str = "pending"
    wave: int = 0
    model_tier: str = ""
    retry_count: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    output_summary: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "wave": self.wave,
            "model_tier": self.model_tier,
            "retry_count": self.retry_count,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "output_summary": self.output_summary,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskWorldState:
        started = data.get("started_at")
        completed = data.get("completed_at")
        return cls(
            id=data["id"],
            status=data["status"],
            wave=data["wave"],
            model_tier=data.get("model_tier", ""),
            retry_count=data.get("retry_count", 0),
            started_at=datetime.fromisoformat(started) if started else None,
            completed_at=datetime.fromisoformat(completed) if completed else None,
            output_summary=data.get("output_summary", ""),
            error=data.get("error", ""),
        )


@dataclass
class ExecutionStrategy:
    """Configuration for how the sentinel should execute a project."""
    max_concurrent: int = 4
    model_preferences: dict[str, str] = field(default_factory=dict)
    retry_policy: dict[str, Any] = field(default_factory=lambda: {
        "max_retries": 3,
        "backoff_seconds": 30,
    })
    checkpoint_on_wave: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_concurrent": self.max_concurrent,
            "model_preferences": self.model_preferences,
            "retry_policy": self.retry_policy,
            "checkpoint_on_wave": self.checkpoint_on_wave,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionStrategy:
        return cls(
            max_concurrent=data.get("max_concurrent", 4),
            model_preferences=data.get("model_preferences", {}),
            retry_policy=data.get("retry_policy", {"max_retries": 3, "backoff_seconds": 30}),
            checkpoint_on_wave=data.get("checkpoint_on_wave", True),
        )


@dataclass
class ProjectWorldModel:
    """Complete snapshot of a project's state as seen by the sentinel."""
    project_id: str = ""
    status: str = "pending"
    tasks: dict[str, TaskWorldState] = field(default_factory=dict)
    current_wave: int = 0
    completed_waves: list[int] = field(default_factory=list)
    budget_spent: float = 0.0
    budget_limit: float = 0.0
    resource_health: dict[str, str] = field(default_factory=dict)
    timing: dict[str, Any] = field(default_factory=dict)
    decision_log: list[DecisionRecord] = field(default_factory=list)
    strategy: ExecutionStrategy = field(default_factory=ExecutionStrategy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "status": self.status,
            "tasks": {k: v.to_dict() for k, v in self.tasks.items()},
            "current_wave": self.current_wave,
            "completed_waves": self.completed_waves,
            "budget_spent": self.budget_spent,
            "budget_limit": self.budget_limit,
            "resource_health": self.resource_health,
            "timing": self.timing,
            "decision_log": [d.to_dict() for d in self.decision_log],
            "strategy": self.strategy.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectWorldModel:
        tasks = {
            k: TaskWorldState.from_dict(v)
            for k, v in data.get("tasks", {}).items()
        }
        decisions = [
            DecisionRecord.from_dict(d)
            for d in data.get("decision_log", [])
        ]
        strategy = ExecutionStrategy.from_dict(data.get("strategy", {}))
        return cls(
            project_id=data["project_id"],
            status=data["status"],
            tasks=tasks,
            current_wave=data.get("current_wave", 0),
            completed_waves=data.get("completed_waves", []),
            budget_spent=data.get("budget_spent", 0.0),
            budget_limit=data.get("budget_limit", 0.0),
            resource_health=data.get("resource_health", {}),
            timing=data.get("timing", {}),
            decision_log=decisions,
            strategy=strategy,
        )


@dataclass
class SentinelObservation:
    """Persistent observation saved to the context store."""

    observation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    category: str = ""  # e.g. "health_state_change", "resource_contention", "lifecycle"
    message: str = ""
    severity: Severity = Severity.INFO
    intervention: Intervention | None = None
    project_id: str | None = None
    task_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class WhyStep:
    """A single step in the 5-whys reasoning chain."""
    question: str
    sources_queried: list[str] = field(default_factory=list)
    evidence_found: str = ""
    conclusion: str = ""


@dataclass
class ReasoningResult:
    """The final result of an iterative reasoning process."""
    why_chain: list[WhyStep] = field(default_factory=list)
    root_cause: str = "unknown"
    confidence: float = 0.0
    recommended_action: str = "escalate"
    knowledge_gaps: list[str] = field(default_factory=list)
    escalation_reason: str | None = None
    # Retry diagnosis fields — populated when error text is analyzed
    fix_type: str = "retry_as_is"  # reassign_tier | modify_prompt | skip | retry_as_is
    fix_params: dict[str, Any] = field(default_factory=dict)  # new_tier, prompt_additions
