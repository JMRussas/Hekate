#  Plan Sentinel
#
#  Per-plan async monitor that tracks execution health for a single project.
#  Subscribes to the ProgressManager SSE stream, maintains internal state
#  (current wave, task statuses, failure counts, timing data), and feeds
#  the rule-based detection engine.
#
#  Spawned and torn down by SystemSentinel.
#
#  Depends on: sentinel/bus.py, sentinel/models.py, sentinel/context_client.py
#  Used by:    system_sentinel.py

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

import httpx

from backend.services.sentinel.models import (
    SentinelMessage,
    SentinelObservation,
    Severity,
)

if TYPE_CHECKING:
    from backend.services.sentinel.bus import SentinelBus
    from backend.services.sentinel.context_client import SentinelContextClient
    from backend.services.progress import ProgressManager

logger = logging.getLogger(__name__)

# Default SSE endpoint base URL (same-process orchestration API)
DEFAULT_BASE_URL = "http://localhost:5200"

# How often (seconds) the tick loop evaluates rules when no SSE events arrive
TICK_INTERVAL = 10.0

# Reconnect delay after SSE stream drops
SSE_RECONNECT_DELAY = 5.0

# Maximum reconnect attempts before giving up
SSE_MAX_RECONNECT_ATTEMPTS = 20


class TaskState(str, Enum):
    """Tracked task states (mirrors key TaskStatus values)."""
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class InterventionTier(str, Enum):
    """Whether an intervention can be auto-executed or needs approval."""
    AUTO = "auto"
    SUPERVISED = "supervised"


class InterventionAction(str, Enum):
    """Specific intervention actions the PlanSentinel can propose or execute."""
    RETRY_TASK = "retry_task"
    RELEASE_CLAIM = "release_claim"
    SKIP_TASK = "skip_task"
    REORDER_WAVE = "reorder_wave"


# Map observation categories to the intervention they trigger
_CATEGORY_TO_INTERVENTION: dict[str, tuple[InterventionAction, InterventionTier]] = {
    "task_stuck": (InterventionAction.RELEASE_CLAIM, InterventionTier.AUTO),
    "cascade_failure": (InterventionAction.SKIP_TASK, InterventionTier.SUPERVISED),
    "wave_stalled": (InterventionAction.REORDER_WAVE, InterventionTier.SUPERVISED),
    # budget_warning has no automatic intervention — observation only
}

# Maximum auto-retries per task before escalating to supervised
MAX_AUTO_RETRIES = 2


@dataclass
class TaskTiming:
    """Timing data for a single task."""
    started_at: float | None = None
    completed_at: float | None = None
    last_progress_at: float | None = None


@dataclass
class PlanState:
    """Internal state maintained by PlanSentinel from SSE events."""

    current_wave: int | None = None

    # task_id → current state
    task_statuses: dict[str, TaskState] = field(default_factory=dict)

    # task_id → consecutive failure count
    failure_counts: dict[str, int] = field(default_factory=dict)

    # task_id → timing data
    task_timing: dict[str, TaskTiming] = field(default_factory=dict)

    # task_id → retry count (how many times we've auto-retried)
    retry_counts: dict[str, int] = field(default_factory=dict)

    # Wave-level outcome tracking: wave → ordered list of (task_id, "failed"/"completed")
    wave_outcomes: dict[int, list[tuple[str, str]]] = field(default_factory=dict)

    # Total events processed
    events_processed: int = 0

    # Budget tracking: current spend and limit (extracted from SSE event data)
    budget_spent: float = 0.0
    budget_limit: float = 0.0

    # Interventions already handled: set of (action, task_id_or_wave) keys
    handled_interventions: set[tuple[str, str]] = field(default_factory=set)

    # Set of observation keys already emitted (to avoid duplicate alerts)
    emitted_observations: set[str] = field(default_factory=set)


class PlanSentinel:
    """Monitors execution health for a single project/plan.

    Subscribes to the orchestration SSE stream (via httpx or in-process
    ProgressManager) and maintains ``PlanState`` from live events.

    Lifecycle managed by SystemSentinel — do not instantiate directly.
    """

    def __init__(
        self,
        project_id: str,
        bus: SentinelBus,
        *,
        context_client: SentinelContextClient | None = None,
        progress_manager: ProgressManager | None = None,
        base_url: str = DEFAULT_BASE_URL,
        auth_token: str | None = None,
    ) -> None:
        self._project_id = project_id
        self._bus = bus
        self._context_client = context_client
        self._progress_manager = progress_manager
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._running = False
        self._task: asyncio.Task | None = None
        self._state = PlanState()
        self._http_client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def running(self) -> bool:
        return self._running

    @property
    def state(self) -> PlanState:
        """Read-only access to the current plan state."""
        return self._state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the plan sentinel. Idempotent."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(), name=f"plan_sentinel:{self._project_id}",
        )
        logger.info("Plan Sentinel started for project %s", self._project_id)

    async def stop(self) -> None:
        """Stop the plan sentinel and clean up."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("Plan Sentinel stopped for project %s", self._project_id)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Subscribe to SSE events and process them.

        Uses in-process ProgressManager if available, otherwise connects
        via httpx to the SSE endpoint.
        """
        if self._progress_manager is not None:
            await self._run_progress_manager_loop()
        else:
            await self._run_httpx_sse_loop()

    # ------------------------------------------------------------------
    # In-process subscription (ProgressManager)
    # ------------------------------------------------------------------

    async def _run_progress_manager_loop(self) -> None:
        """Subscribe to the ProgressManager's internal queue directly."""
        assert self._progress_manager is not None
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._progress_manager._subscribers.setdefault(
            self._project_id, [],
        ).append(queue)
        try:
            while self._running:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=TICK_INTERVAL)
                    await self._handle_event(event)
                except asyncio.TimeoutError:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Plan Sentinel event error (project %s)", self._project_id,
                    )
        finally:
            subs = self._progress_manager._subscribers.get(self._project_id, [])
            if queue in subs:
                subs.remove(queue)

    # ------------------------------------------------------------------
    # httpx SSE subscription
    # ------------------------------------------------------------------

    async def _run_httpx_sse_loop(self) -> None:
        """Connect to the SSE endpoint via httpx and process events."""
        attempts = 0
        while self._running and attempts < SSE_MAX_RECONNECT_ATTEMPTS:
            try:
                await self._stream_sse()
                # Clean exit (project_complete/project_failed received)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                attempts += 1
                logger.warning(
                    "Plan Sentinel SSE connection lost for project %s (attempt %d/%d)",
                    self._project_id, attempts, SSE_MAX_RECONNECT_ATTEMPTS,
                    exc_info=True,
                )
                if self._running and attempts < SSE_MAX_RECONNECT_ATTEMPTS:
                    await asyncio.sleep(SSE_RECONNECT_DELAY)

        if self._running:
            logger.error(
                "Plan Sentinel exhausted SSE reconnect attempts for project %s",
                self._project_id,
            )

    async def _stream_sse(self) -> None:
        """Open an httpx streaming connection and parse SSE events."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(None))

        url = f"{self._base_url}/api/events/{self._project_id}"
        params = {}
        if self._auth_token:
            params["token"] = self._auth_token

        async with self._http_client.stream(
            "GET", url, params=params,
        ) as response:
            response.raise_for_status()

            event_type: str | None = None
            data_lines: list[str] = []

            async for line in response.aiter_lines():
                if not self._running:
                    return

                line = line.rstrip("\r\n")

                # SSE parsing per spec
                if line.startswith("event: "):
                    event_type = line[7:]
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
                elif line == "":
                    # End of event block
                    if data_lines:
                        raw = "\n".join(data_lines)
                        try:
                            event = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.debug(
                                "Plan Sentinel: non-JSON SSE data: %s", raw[:200],
                            )
                            data_lines.clear()
                            event_type = None
                            continue
                        await self._handle_event(event)

                        # Terminal events
                        if event.get("type") in (
                            "project_complete", "project_failed",
                        ):
                            return
                    data_lines.clear()
                    event_type = None
                elif line.startswith(":"):
                    # Comment / keepalive — ignore
                    pass

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """Route an SSE event to the appropriate state-update handler."""
        self._state.events_processed += 1
        event_type = event.get("type", "")
        task_id = event.get("task_id")
        timestamp = event.get("timestamp", time.time())

        handler = self._EVENT_HANDLERS.get(event_type)
        if handler is not None:
            try:
                handler(self, event, task_id, timestamp)
            except Exception:
                logger.exception(
                    "Plan Sentinel handler error for %s (project %s)",
                    event_type, self._project_id,
                )
        else:
            logger.debug(
                "Plan Sentinel: unhandled event type %s (project %s)",
                event_type, self._project_id,
            )

    # --- Individual event handlers ---

    def _on_task_start(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        if not task_id:
            return
        self._state.task_statuses[task_id] = TaskState.RUNNING
        timing = self._state.task_timing.setdefault(task_id, TaskTiming())
        timing.started_at = ts
        timing.last_progress_at = ts
        logger.debug("Plan Sentinel: task_start %s", task_id[:8])

    def _on_task_complete(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        if not task_id:
            return
        self._state.task_statuses[task_id] = TaskState.COMPLETED
        timing = self._state.task_timing.setdefault(task_id, TaskTiming())
        timing.completed_at = ts
        timing.last_progress_at = ts
        # Reset failure count on success
        self._state.failure_counts.pop(task_id, None)
        # Track wave-level outcomes for consecutive-failure detection
        if self._state.current_wave is not None:
            wave = self._state.current_wave
            outcomes = self._state.wave_outcomes.setdefault(wave, [])
            outcomes.append((task_id, "completed"))
        logger.debug("Plan Sentinel: task_complete %s", task_id[:8])

    def _on_task_failed(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        if not task_id:
            return
        self._state.task_statuses[task_id] = TaskState.FAILED
        self._state.failure_counts[task_id] = (
            self._state.failure_counts.get(task_id, 0) + 1
        )
        timing = self._state.task_timing.setdefault(task_id, TaskTiming())
        timing.last_progress_at = ts

        # Track wave-level outcomes for consecutive-failure detection
        if self._state.current_wave is not None:
            wave = self._state.current_wave
            outcomes = self._state.wave_outcomes.setdefault(wave, [])
            outcomes.append((task_id, "failed"))

        logger.debug(
            "Plan Sentinel: task_failed %s (failures: %d)",
            task_id[:8], self._state.failure_counts[task_id],
        )

    def _on_task_output(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        """Progress event — update last_progress_at to prevent stuck detection."""
        if not task_id:
            return
        timing = self._state.task_timing.setdefault(task_id, TaskTiming())
        timing.last_progress_at = ts

    def _on_task_needs_review(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        if not task_id:
            return
        self._state.task_statuses[task_id] = TaskState.NEEDS_REVIEW
        timing = self._state.task_timing.setdefault(task_id, TaskTiming())
        timing.last_progress_at = ts

    def _on_task_zombie_recovered(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        if not task_id:
            return
        # Task was stuck and has been re-queued
        self._state.task_statuses[task_id] = TaskState.PENDING
        timing = self._state.task_timing.setdefault(task_id, TaskTiming())
        timing.started_at = None
        timing.last_progress_at = ts

    def _on_wave_checkpoint(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        completed_wave = event.get("wave")
        next_wave = event.get("next_wave")
        if next_wave is not None:
            self._state.current_wave = next_wave
        elif completed_wave is not None:
            self._state.current_wave = completed_wave
        logger.debug(
            "Plan Sentinel: wave_checkpoint wave=%s next=%s",
            completed_wave, next_wave,
        )

    def _on_budget_warning(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        # Extract actual spend/limit from the SSE event payload
        spent = event.get("spent") or event.get("total_spent")
        limit = event.get("limit") or event.get("budget_limit")
        if spent is not None:
            self._state.budget_spent = float(spent)
        if limit is not None:
            self._state.budget_limit = float(limit)
        logger.debug(
            "Plan Sentinel: budget_warning spent=%.2f limit=%.2f",
            self._state.budget_spent, self._state.budget_limit,
        )

    def _on_checkpoint(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        if not task_id:
            return
        self._state.task_statuses[task_id] = TaskState.NEEDS_REVIEW

    def _on_project_complete(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        logger.info(
            "Plan Sentinel: project_complete for %s", self._project_id,
        )

    def _on_project_failed(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        logger.info(
            "Plan Sentinel: project_failed for %s", self._project_id,
        )

    def _on_project_blocked(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        logger.info(
            "Plan Sentinel: project_blocked for %s", self._project_id,
        )

    # Event type → handler mapping
    _EVENT_HANDLERS: dict[str, Any] = {
        "task_start": _on_task_start,
        "task_complete": _on_task_complete,
        "task_failed": _on_task_failed,
        "task_output": _on_task_output,
        "task_needs_review": _on_task_needs_review,
        "task_zombie_recovered": _on_task_zombie_recovered,
        "wave_checkpoint": _on_wave_checkpoint,
        "budget_warning": _on_budget_warning,
        "checkpoint": _on_checkpoint,
        "project_complete": _on_project_complete,
        "project_failed": _on_project_failed,
        "project_blocked": _on_project_blocked,
    }

    # ------------------------------------------------------------------
    # Tick (periodic evaluation when no events are flowing)
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """Periodic evaluation — run all detection rules, publish observations,
        and handle interventions."""
        observations = self._evaluate_rules()
        for obs in observations:
            await self._publish_observation(obs)
            await self._handle_intervention(obs)

    # ------------------------------------------------------------------
    # Rule-based detection engine
    # ------------------------------------------------------------------

    # Configurable thresholds
    STUCK_THRESHOLD_SECS: float = 300.0  # 5 minutes
    CASCADE_FAILURE_MIN: int = 3  # consecutive failures to trigger
    BUDGET_PERCENT_THRESHOLD: float = 0.80  # 80% of budget limit

    def _evaluate_rules(self) -> list[SentinelObservation]:
        """Run all detection rules against current state. Returns observations."""
        observations: list[SentinelObservation] = []
        observations.extend(self._rule_task_stuck())
        observations.extend(self._rule_wave_stalled())
        observations.extend(self._rule_cascade_failure())
        observations.extend(self._rule_budget_warning())
        return observations

    def _rule_task_stuck(self) -> list[SentinelObservation]:
        """Detect tasks running > STUCK_THRESHOLD_SECS with no progress."""
        now = time.time()
        results: list[SentinelObservation] = []
        for task_id, status in self._state.task_statuses.items():
            if status != TaskState.RUNNING:
                continue
            timing = self._state.task_timing.get(task_id)
            if timing is None or timing.last_progress_at is None:
                continue
            elapsed = now - timing.last_progress_at
            if elapsed > self.STUCK_THRESHOLD_SECS:
                results.append(SentinelObservation(
                    category="task_stuck",
                    message=(
                        f"Task {task_id[:8]} has had no progress for "
                        f"{elapsed:.0f}s (threshold: {self.STUCK_THRESHOLD_SECS:.0f}s)"
                    ),
                    severity=Severity.WARNING,
                    project_id=self._project_id,
                    task_id=task_id,
                    details={
                        "rule": "task_stuck",
                        "elapsed_secs": round(elapsed, 1),
                        "threshold_secs": self.STUCK_THRESHOLD_SECS,
                    },
                ))
        return results

    def _rule_wave_stalled(self) -> list[SentinelObservation]:
        """Detect when all tasks in the current wave are failed or stuck."""
        if self._state.current_wave is None:
            return []

        now = time.time()
        wave_tasks: list[str] = []
        for task_id, status in self._state.task_statuses.items():
            # Heuristic: tasks in current wave are those currently running,
            # failed, or stuck. We consider all non-completed active tasks.
            if status in (
                TaskState.RUNNING, TaskState.FAILED,
                TaskState.PENDING, TaskState.QUEUED, TaskState.BLOCKED,
            ):
                wave_tasks.append(task_id)

        if not wave_tasks:
            return []

        all_stalled = True
        for task_id in wave_tasks:
            status = self._state.task_statuses[task_id]
            if status == TaskState.FAILED:
                continue  # counts as stalled
            if status == TaskState.RUNNING:
                timing = self._state.task_timing.get(task_id)
                if timing and timing.last_progress_at:
                    elapsed = now - timing.last_progress_at
                    if elapsed <= self.STUCK_THRESHOLD_SECS:
                        all_stalled = False
                        break
                else:
                    all_stalled = False
                    break
            else:
                # PENDING/QUEUED/BLOCKED — not failed or stuck, wave not stalled
                all_stalled = False
                break

        if not all_stalled:
            return []

        return [SentinelObservation(
            category="wave_stalled",
            message=(
                f"Wave {self._state.current_wave} is stalled: all "
                f"{len(wave_tasks)} tasks are failed or stuck"
            ),
            severity=Severity.CRITICAL,
            project_id=self._project_id,
            details={
                "rule": "wave_stalled",
                "wave": self._state.current_wave,
                "task_count": len(wave_tasks),
                "task_ids": wave_tasks,
            },
        )]

    def _rule_cascade_failure(self) -> list[SentinelObservation]:
        """Detect 3+ consecutive task failures in the same wave.

        Walks the wave_outcomes list (ordered by time) and finds the longest
        current run of consecutive failures.  A successful completion resets
        the run counter.
        """
        results: list[SentinelObservation] = []
        for wave, outcomes in self._state.wave_outcomes.items():
            consecutive_failures: list[str] = []
            max_run: list[str] = []

            for task_id, outcome in outcomes:
                if outcome == "failed":
                    consecutive_failures.append(task_id)
                else:
                    # Success breaks the consecutive run
                    if len(consecutive_failures) > len(max_run):
                        max_run = list(consecutive_failures)
                    consecutive_failures = []

            # Check the trailing run
            if len(consecutive_failures) > len(max_run):
                max_run = consecutive_failures

            if len(max_run) >= self.CASCADE_FAILURE_MIN:
                obs_key = f"cascade_failure:wave_{wave}"
                if obs_key in self._state.emitted_observations:
                    continue
                self._state.emitted_observations.add(obs_key)
                results.append(SentinelObservation(
                    category="cascade_failure",
                    message=(
                        f"Cascade failure in wave {wave}: "
                        f"{len(max_run)} consecutive failures"
                    ),
                    severity=Severity.CRITICAL,
                    project_id=self._project_id,
                    details={
                        "rule": "cascade_failure",
                        "wave": wave,
                        "consecutive_failure_count": len(max_run),
                        "failed_task_ids": max_run,
                    },
                ))
        return results

    def _rule_budget_warning(self) -> list[SentinelObservation]:
        """Detect budget usage exceeding 80% by evaluating spend vs limit."""
        if self._state.budget_limit <= 0:
            return []
        ratio = self._state.budget_spent / self._state.budget_limit
        if ratio < self.BUDGET_PERCENT_THRESHOLD:
            return []

        obs_key = "budget_warning"
        if obs_key in self._state.emitted_observations:
            return []
        self._state.emitted_observations.add(obs_key)

        pct = ratio * 100
        return [SentinelObservation(
            category="budget_warning",
            message=(
                f"Budget warning: spending at {pct:.1f}% of limit "
                f"(${self._state.budget_spent:.2f} / ${self._state.budget_limit:.2f})"
            ),
            severity=Severity.WARNING,
            project_id=self._project_id,
            details={
                "rule": "budget_warning",
                "budget_spent": self._state.budget_spent,
                "budget_limit": self._state.budget_limit,
                "usage_percent": round(pct, 1),
            },
        )]

    # ------------------------------------------------------------------
    # Observation publishing
    # ------------------------------------------------------------------

    async def _publish_observation(self, obs: SentinelObservation) -> None:
        """Publish an observation to the bus and persist via context client.

        Maps observation categories to valid bus topics:
        - budget_warning → resource_alert
        - task_stuck, wave_stalled, cascade_failure → stall_notification
        """
        topic = "stall_notification"
        if obs.category == "budget_warning":
            topic = "resource_alert"

        msg = SentinelMessage(
            topic=topic,
            source=f"plan_sentinel:{self._project_id}",
            payload={
                "observation_id": obs.observation_id,
                "category": obs.category,
                "severity": obs.severity.value,
                "message": obs.message,
                "project_id": obs.project_id,
                "task_id": obs.task_id,
                "details": obs.details,
            },
        )
        try:
            await self._bus.publish(msg)
        except Exception:
            logger.exception(
                "Failed to publish observation %s to bus", obs.observation_id,
            )

        # Persist to context store
        if self._context_client:
            try:
                await self._context_client.save_observation(obs)
            except Exception:
                logger.exception(
                    "Failed to persist observation %s", obs.observation_id,
                )

    # ------------------------------------------------------------------
    # Intervention handling
    # ------------------------------------------------------------------

    async def _handle_intervention(self, obs: SentinelObservation) -> None:
        """Determine and execute the appropriate intervention for an observation.

        Auto-tier interventions (retry_task, release_claim) are executed
        directly via the orchestration API. Supervised-tier interventions
        (skip_task, reorder_wave) are published as proposals for user approval.
        """
        entry = _CATEGORY_TO_INTERVENTION.get(obs.category)
        if entry is None:
            return

        action, tier = entry

        # For cascade failures, try auto-retry on the last failed task first
        if obs.category == "cascade_failure" and obs.details:
            failed_ids = obs.details.get("failed_task_ids", [])
            if failed_ids:
                last_failed = failed_ids[-1]
                retries = self._state.retry_counts.get(last_failed, 0)
                if retries < MAX_AUTO_RETRIES:
                    action = InterventionAction.RETRY_TASK
                    tier = InterventionTier.AUTO

        # Deduplication: avoid repeating the same intervention
        dedup_target = obs.task_id or str(obs.details.get("wave", ""))
        dedup_key = (action.value, dedup_target)
        if dedup_key in self._state.handled_interventions:
            return
        self._state.handled_interventions.add(dedup_key)

        if tier == InterventionTier.AUTO:
            await self._execute_auto_intervention(action, obs)
        else:
            await self._publish_intervention_proposal(action, obs)

    # --- Auto-tier interventions ---

    async def _execute_auto_intervention(
        self, action: InterventionAction, obs: SentinelObservation,
    ) -> None:
        """Execute an auto-tier intervention and publish the outcome."""
        success = False
        result_detail = ""

        try:
            if action == InterventionAction.RETRY_TASK:
                success, result_detail = await self._auto_retry_task(obs)
            elif action == InterventionAction.RELEASE_CLAIM:
                success, result_detail = await self._auto_release_claim(obs)
            else:
                logger.warning("Unknown auto intervention: %s", action)
                return
        except Exception:
            logger.exception(
                "Auto intervention %s failed for project %s",
                action.value, self._project_id,
            )
            result_detail = "exception during execution"

        # Publish intervention outcome to bus
        msg = SentinelMessage(
            topic="intervention_proposal",
            source=f"plan_sentinel:{self._project_id}",
            payload={
                "type": "intervention_executed",
                "action": action.value,
                "tier": InterventionTier.AUTO.value,
                "success": success,
                "detail": result_detail,
                "observation_id": obs.observation_id,
                "project_id": self._project_id,
                "task_id": obs.task_id,
            },
        )
        try:
            await self._bus.publish(msg)
        except Exception:
            logger.exception(
                "Failed to publish intervention result for %s", obs.observation_id,
            )

    async def _auto_retry_task(
        self, obs: SentinelObservation,
    ) -> tuple[bool, str]:
        """Re-queue a failed task via the orchestration API (max 2 retries)."""
        task_id = obs.task_id
        if not task_id:
            # For cascade failures, retry the last failed task
            failed_ids = obs.details.get("failed_task_ids", [])
            task_id = failed_ids[-1] if failed_ids else None
        if not task_id:
            return False, "no task_id to retry"

        retries = self._state.retry_counts.get(task_id, 0)
        if retries >= MAX_AUTO_RETRIES:
            return False, f"retry limit reached ({retries}/{MAX_AUTO_RETRIES})"

        url = f"{self._base_url}/api/tasks/{task_id}/retry"
        headers = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

        resp = await self._http_client.post(url, headers=headers)
        if resp.status_code == 200:
            self._state.retry_counts[task_id] = retries + 1
            self._state.task_statuses[task_id] = TaskState.PENDING
            logger.info(
                "Plan Sentinel auto-retried task %s (attempt %d/%d)",
                task_id[:8], retries + 1, MAX_AUTO_RETRIES,
            )
            return True, f"retried (attempt {retries + 1}/{MAX_AUTO_RETRIES})"

        detail = resp.text[:200] if resp.text else str(resp.status_code)
        logger.warning(
            "Plan Sentinel retry failed for task %s: %s", task_id[:8], detail,
        )
        return False, f"API returned {resp.status_code}: {detail}"

    async def _auto_release_claim(
        self, obs: SentinelObservation,
    ) -> tuple[bool, str]:
        """Release a stuck task's claim so another worker can pick it up."""
        task_id = obs.task_id
        if not task_id:
            return False, "no task_id on observation"

        url = f"{self._base_url}/api/external/tasks/{task_id}/release"
        headers = {}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

        resp = await self._http_client.post(url, headers=headers)
        if resp.status_code == 200:
            self._state.task_statuses[task_id] = TaskState.PENDING
            logger.info(
                "Plan Sentinel released claim on stuck task %s", task_id[:8],
            )
            return True, "claim released"

        # Release may fail if task isn't externally claimed — fall back to retry
        retries = self._state.retry_counts.get(task_id, 0)
        if retries < MAX_AUTO_RETRIES:
            return await self._auto_retry_task(obs)

        detail = resp.text[:200] if resp.text else str(resp.status_code)
        logger.warning(
            "Plan Sentinel release failed for task %s: %s", task_id[:8], detail,
        )
        return False, f"API returned {resp.status_code}: {detail}"

    # --- Supervised-tier interventions ---

    async def _publish_intervention_proposal(
        self, action: InterventionAction, obs: SentinelObservation,
    ) -> None:
        """Publish a supervised intervention proposal for user approval."""
        payload: dict[str, Any] = {
            "type": "intervention_proposal",
            "action": action.value,
            "tier": InterventionTier.SUPERVISED.value,
            "observation_id": obs.observation_id,
            "project_id": self._project_id,
            "category": obs.category,
            "severity": obs.severity.value,
            "message": obs.message,
        }

        if action == InterventionAction.SKIP_TASK:
            failed_ids = obs.details.get("failed_task_ids", [])
            payload["task_ids"] = failed_ids
            payload["recommendation"] = (
                f"Skip {len(failed_ids)} failed task(s) in wave "
                f"{obs.details.get('wave', '?')} to unblock dependents"
            )
        elif action == InterventionAction.REORDER_WAVE:
            wave = obs.details.get("wave")
            task_ids = obs.details.get("task_ids", [])
            payload["wave"] = wave
            payload["stalled_task_ids"] = task_ids
            payload["recommendation"] = (
                f"Wave {wave} is fully stalled ({len(task_ids)} tasks). "
                "Consider reordering remaining work or manual intervention."
            )

        msg = SentinelMessage(
            topic="intervention_proposal",
            source=f"plan_sentinel:{self._project_id}",
            payload=payload,
        )
        try:
            await self._bus.publish(msg)
            logger.info(
                "Plan Sentinel proposed %s for project %s: %s",
                action.value, self._project_id,
                payload.get("recommendation", ""),
            )
        except Exception:
            logger.exception(
                "Failed to publish intervention proposal %s for %s",
                action.value, self._project_id,
            )
