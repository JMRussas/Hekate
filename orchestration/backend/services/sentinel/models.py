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
