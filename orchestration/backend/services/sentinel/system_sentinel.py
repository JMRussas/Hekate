#  System Sentinel
#
#  Singleton async service that monitors system health trends, manages Plan
#  Sentinel lifecycles, detects cross-plan resource contention, and persists
#  system-level observations.
#
#  Started alongside the executor in the app lifespan (app.py).
#
#  Depends on: resource_monitor.py, sentinel/bus.py, sentinel/context_client.py
#  Used by:    app.py (lifespan), container.py

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from backend.models.enums import ResourceStatus
from backend.services.sentinel.bus import SentinelBus
from backend.services.sentinel.models import (
    HealthSample,
    HealthState,
    HealthTrend,
    SentinelMessage,
    SentinelObservation,
    Severity,
)
from backend.services.sentinel.plan_sentinel import PlanSentinel

if TYPE_CHECKING:
    from backend.db.connection import Database
    from backend.services.resource_monitor import ResourceMonitor
    from backend.services.sentinel.context_client import SentinelContextClient

logger = logging.getLogger(__name__)

# How often the sentinel polls resource health (seconds)
DEFAULT_POLL_INTERVAL = 30.0

# Sliding window size (number of samples retained per resource)
DEFAULT_WINDOW_SIZE = 20

# Degradation thresholds
FAILURE_RATE_INTERMITTENT = 0.2   # ≥20% failures in window → intermittent
FAILURE_RATE_DOWN = 0.8           # ≥80% failures in window → down

# Contention thresholds
SEMAPHORE_CONTENTION_THRESHOLD = 0.8  # ≥80% slot utilization triggers advisory
TIER_CONTENTION_MIN_PROJECTS = 2      # ≥2 projects using same tier = contention

# Well-known parent node ID for system-level (non-project) observations.
# The context store creates this lazily on first child insert.
SYSTEM_SENTINEL_PARENT = "system_sentinel_root"


class SystemSentinel:
    """Async singleton that monitors system health and orchestrates Plan Sentinels.

    Lifecycle:
        sentinel = SystemSentinel(resource_monitor=rm, bus=bus)
        await sentinel.start()   # spawns background loop
        ...
        await sentinel.stop()    # cancels loop, cleans up

    Phase 1 (this file): foundation — start/stop, background tick loop.
    Subsequent phases add health trend tracking, Plan Sentinel registry,
    cross-plan contention detection, and observation persistence.
    """

    def __init__(
        self,
        resource_monitor: ResourceMonitor,
        bus: SentinelBus | None = None,
        context_client: SentinelContextClient | None = None,
        db: Database | None = None,
        progress_manager=None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        window_size: int = DEFAULT_WINDOW_SIZE,
        max_concurrent_tasks: int | None = None,
    ):
        self._resource_monitor = resource_monitor
        self._bus = bus or SentinelBus()
        self._context_client = context_client
        self._db = db
        self._progress_manager = progress_manager
        self._poll_interval = poll_interval
        self._window_size = window_size

        # Resolve max concurrent tasks from config if not explicitly set
        if max_concurrent_tasks is not None:
            self._max_concurrent_tasks = max_concurrent_tasks
        else:
            from backend.config import MAX_CONCURRENT_TASKS
            self._max_concurrent_tasks = MAX_CONCURRENT_TASKS

        self._running = False
        self._task: asyncio.Task | None = None

        # Health trend history — resource_id → HealthTrend
        self._trends: dict[str, HealthTrend] = {}

        # Plan Sentinel registry — keyed by project_id
        self._plan_sentinels: dict[str, PlanSentinel] = {}

        # Contention tracking — tier → set of project_ids (last known)
        self._last_contention: dict[str, set[str]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the sentinel background loop. Idempotent."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="system_sentinel")
        # Listen for PlanSentinels that self-terminated
        self._bus.on("plan_sentinel_stopped", self._on_plan_sentinel_stopped)
        logger.info("System Sentinel started (poll_interval=%.0fs)", self._poll_interval)

    async def _on_plan_sentinel_stopped(self, msg) -> None:
        """Remove a self-terminated PlanSentinel from the registry."""
        project_id = msg.payload.get("project_id")
        reason = msg.payload.get("reason", "unknown")
        if project_id and project_id in self._plan_sentinels:
            del self._plan_sentinels[project_id]
            logger.info(
                "Plan Sentinel removed for project %s (reason=%s, remaining=%d)",
                project_id, reason, len(self._plan_sentinels),
            )

    async def stop(self) -> None:
        """Stop the sentinel and clean up resources."""
        if not self._running:
            return
        self._running = False

        # Cancel the background loop
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # Stop all Plan Sentinels
        for ps in list(self._plan_sentinels.values()):
            await ps.stop()
        self._plan_sentinels.clear()

        # Shut down the message bus
        await self._bus.shutdown()

        # Close context client if we own it
        if self._context_client:
            await self._context_client.close()
        logger.info("System Sentinel stopped")

    @property
    def running(self) -> bool:
        return self._running

    @property
    def bus(self) -> SentinelBus:
        return self._bus

    @property
    def plan_sentinels(self) -> dict[str, PlanSentinel]:
        """Read-only view of the active Plan Sentinel registry."""
        return dict(self._plan_sentinels)

    def get_trend(self, resource_id: str) -> HealthTrend | None:
        """Return the health trend for a resource, or None if not tracked yet."""
        return self._trends.get(resource_id)

    def get_all_trends(self) -> dict[str, HealthTrend]:
        """Return a snapshot of all health trends."""
        return dict(self._trends)

    # ------------------------------------------------------------------
    # Plan Sentinel lifecycle
    # ------------------------------------------------------------------

    async def spawn_plan_sentinel(self, project_id: str) -> PlanSentinel:
        """Spawn a Plan Sentinel for *project_id* and register it.

        If a sentinel already exists for the project, the existing one is
        returned without creating a duplicate.
        """
        existing = self._plan_sentinels.get(project_id)
        if existing is not None:
            logger.warning(
                "Plan Sentinel already active for project %s — returning existing",
                project_id,
            )
            return existing

        # Create InterventionExecutor with DB access for tier reassignment
        from backend.services.sentinel.intervention_executor import InterventionExecutor
        executor = InterventionExecutor(
            base_url="http://localhost:5200",
            bus=self._bus,
            db=self._db,
        )

        sentinel = PlanSentinel(
            project_id=project_id,
            bus=self._bus,
            progress_manager=self._progress_manager,
            db=self._db,
            intervention_executor=executor,
        )
        await sentinel.start()
        self._plan_sentinels[project_id] = sentinel
        logger.info(
            "Spawned Plan Sentinel for project %s (active: %d)",
            project_id,
            len(self._plan_sentinels),
        )

        await self._persist_observation(SentinelObservation(
            category="lifecycle",
            message=f"Plan Sentinel spawned for project {project_id}",
            severity=Severity.INFO,
            project_id=project_id,
            details={"event": "plan_sentinel_spawned", "active_count": len(self._plan_sentinels)},
        ))

        return sentinel

    async def teardown_plan_sentinel(self, project_id: str) -> bool:
        """Stop and remove the Plan Sentinel for *project_id*.

        Returns True if a sentinel was found and torn down, False if none
        existed for the given project.
        """
        sentinel = self._plan_sentinels.pop(project_id, None)
        if sentinel is None:
            logger.warning(
                "No Plan Sentinel found for project %s — nothing to tear down",
                project_id,
            )
            return False

        await sentinel.stop()
        logger.info(
            "Torn down Plan Sentinel for project %s (active: %d)",
            project_id,
            len(self._plan_sentinels),
        )

        await self._persist_observation(SentinelObservation(
            category="lifecycle",
            message=f"Plan Sentinel torn down for project {project_id}",
            severity=Severity.INFO,
            project_id=project_id,
            details={"event": "plan_sentinel_teardown", "active_count": len(self._plan_sentinels)},
        ))

        return True

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Main tick loop — polls resources and runs sentinel logic."""
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("System Sentinel tick error")
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        """Single sentinel tick: poll resources, update trends, detect contention."""
        states = await self._resource_monitor.check_all()
        now = datetime.now(timezone.utc)

        # Contention detection (requires DB)
        if self._db and self._plan_sentinels:
            await self._detect_contention()

        for state in states:
            sample = HealthSample(
                timestamp=now,
                online=state.status == ResourceStatus.ONLINE,
                response_time_ms=state.response_time_ms,
            )

            # Get or create trend tracker for this resource
            trend = self._trends.get(state.id)
            if trend is None:
                trend = HealthTrend(
                    resource_id=state.id,
                    window_size=self._window_size,
                )
                self._trends[state.id] = trend

            trend.push(sample)

            # Evaluate derived health state from the sliding window
            new_state = self._evaluate_state(trend)

            if new_state != trend.state:
                trend.previous_state = trend.state
                trend.state = new_state
                await self._on_state_change(state.id, trend)

    # ------------------------------------------------------------------
    # Trend analysis
    # ------------------------------------------------------------------

    @staticmethod
    def _evaluate_state(trend: HealthTrend) -> HealthState:
        """Derive a health state from the sliding window of samples."""
        if len(trend.samples) == 0:
            return HealthState.HEALTHY

        fr = trend.failure_rate

        # Mostly failing → down
        if fr >= FAILURE_RATE_DOWN:
            return HealthState.DOWN

        # Some failures → intermittent
        if fr >= FAILURE_RATE_INTERMITTENT:
            return HealthState.INTERMITTENT

        # All online but latency is climbing → degraded
        if trend.latency_increasing():
            return HealthState.DEGRADED

        return HealthState.HEALTHY

    async def _on_state_change(
        self, resource_id: str, trend: HealthTrend,
    ) -> None:
        """Handle a health state transition — log and publish to the bus."""
        prev = trend.previous_state or HealthState.HEALTHY
        cur = trend.state
        avg = trend.avg_latency_ms

        severity = Severity.INFO
        if cur in (HealthState.INTERMITTENT, HealthState.DEGRADED):
            severity = Severity.WARNING
        elif cur == HealthState.DOWN:
            severity = Severity.CRITICAL

        detail = (
            f"Resource {resource_id}: {prev.value} → {cur.value}"
            f" (failure_rate={trend.failure_rate:.0%}"
            f", avg_latency={avg:.0f}ms)" if avg is not None else
            f"Resource {resource_id}: {prev.value} → {cur.value}"
            f" (failure_rate={trend.failure_rate:.0%})"
        )

        if severity == Severity.CRITICAL:
            logger.error(detail)
        elif severity == Severity.WARNING:
            logger.warning(detail)
        else:
            logger.info(detail)

        await self._bus.publish(SentinelMessage(
            topic="resource_alert",
            source="system_sentinel",
            payload={
                "resource_id": resource_id,
                "previous_state": prev.value,
                "current_state": cur.value,
                "failure_rate": round(trend.failure_rate, 3),
                "avg_latency_ms": round(avg, 1) if avg is not None else None,
                "severity": severity.value,
            },
        ))

        # Persist health state change to context store
        await self._persist_observation(SentinelObservation(
            category="health_state_change",
            message=detail,
            severity=severity,
            details={
                "resource_id": resource_id,
                "previous_state": prev.value,
                "current_state": cur.value,
                "failure_rate": round(trend.failure_rate, 3),
                "avg_latency_ms": round(avg, 1) if avg is not None else None,
            },
        ))

    # ------------------------------------------------------------------
    # Observation persistence
    # ------------------------------------------------------------------

    async def _persist_observation(self, observation: SentinelObservation) -> None:
        """Fire-and-forget save to the context store. Never raises."""
        if not self._context_client:
            return
        try:
            parent = observation.project_id or SYSTEM_SENTINEL_PARENT
            await self._context_client.save_observation(observation, parent_id=parent)
        except Exception:
            logger.debug("Failed to persist observation %s", observation.observation_id, exc_info=True)

    # ------------------------------------------------------------------
    # Contention detection
    # ------------------------------------------------------------------

    async def _detect_contention(self) -> None:
        """Query running/queued tasks, detect semaphore and tier contention."""
        assert self._db is not None  # guarded by caller

        active_project_ids = set(self._plan_sentinels.keys())
        if not active_project_ids:
            return

        # Fetch running + queued tasks for active projects
        placeholders = ", ".join("?" for _ in active_project_ids)
        rows = await self._db.fetchall(
            f"SELECT project_id, model_tier FROM tasks "
            f"WHERE project_id IN ({placeholders}) "
            f"AND status IN ('running', 'queued')",
            tuple(active_project_ids),
        )

        await self._check_semaphore_contention(rows)
        await self._check_tier_contention(rows)

    async def _check_semaphore_contention(
        self, rows: list[Any],
    ) -> None:
        """Detect when aggregate task demand saturates the global semaphore."""
        total_active = len(rows)
        utilization = total_active / self._max_concurrent_tasks if self._max_concurrent_tasks > 0 else 0.0

        if utilization < SEMAPHORE_CONTENTION_THRESHOLD:
            # Clear prior semaphore contention state
            self._last_contention.pop("__semaphore__", None)
            return

        # Which projects contribute?
        project_ids: set[str] = {r["project_id"] for r in rows}
        if len(project_ids) < 2:
            # Single project using slots isn't cross-plan contention
            self._last_contention.pop("__semaphore__", None)
            return

        # Only fire if the set of contending projects changed
        prev = self._last_contention.get("__semaphore__", set())
        if project_ids == prev:
            return
        self._last_contention["__semaphore__"] = project_ids

        logger.warning(
            "Semaphore contention: %d/%d slots used by %d projects %s",
            total_active, self._max_concurrent_tasks, len(project_ids),
            sorted(project_ids),
        )

        await self._persist_observation(SentinelObservation(
            category="resource_contention",
            message=f"Semaphore contention: {total_active}/{self._max_concurrent_tasks} slots across {len(project_ids)} projects",
            severity=Severity.WARNING,
            details={
                "kind": "semaphore",
                "utilization": round(utilization, 2),
                "active_tasks": total_active,
                "max_slots": self._max_concurrent_tasks,
                "contending_projects": sorted(project_ids),
            },
        ))

        for pid in project_ids:
            await self._bus.publish(SentinelMessage(
                topic="contention_advisory",
                source="system_sentinel",
                payload={
                    "kind": "semaphore",
                    "project_id": pid,
                    "utilization": round(utilization, 2),
                    "active_tasks": total_active,
                    "max_slots": self._max_concurrent_tasks,
                    "contending_projects": sorted(project_ids),
                    "severity": Severity.WARNING.value,
                },
            ))

    async def _check_tier_contention(
        self, rows: list[Any],
    ) -> None:
        """Detect when multiple projects compete for the same model tier."""
        # Build tier → set of project_ids
        tier_projects: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            tier_projects[r["model_tier"]].add(r["project_id"])

        # Check each tier for cross-plan contention
        for tier, project_ids in tier_projects.items():
            if len(project_ids) < TIER_CONTENTION_MIN_PROJECTS:
                self._last_contention.pop(tier, None)
                continue

            prev = self._last_contention.get(tier, set())
            if project_ids == prev:
                continue
            self._last_contention[tier] = set(project_ids)

            logger.warning(
                "Tier contention on %s: %d projects competing %s",
                tier, len(project_ids), sorted(project_ids),
            )

            await self._persist_observation(SentinelObservation(
                category="resource_contention",
                message=f"Tier contention on {tier}: {len(project_ids)} projects competing",
                severity=Severity.WARNING,
                details={
                    "kind": "model_tier",
                    "tier": tier,
                    "contending_projects": sorted(project_ids),
                },
            ))

            for pid in project_ids:
                await self._bus.publish(SentinelMessage(
                    topic="contention_advisory",
                    source="system_sentinel",
                    payload={
                        "kind": "model_tier",
                        "tier": tier,
                        "project_id": pid,
                        "contending_projects": sorted(project_ids),
                        "severity": Severity.WARNING.value,
                    },
                ))
