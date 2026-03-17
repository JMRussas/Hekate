#  Plan Sentinel
#
#  Per-plan async monitor that tracks execution health for a single project.
#  Subscribes to the ProgressManager SSE stream, maintains internal state
#  (current wave, task statuses, failure counts, timing data), and feeds
#  the rule-based detection engine.
#
#  Spawned and torn down by SystemSentinel.
#
#  Depends on: sentinel/bus.py, sentinel/models.py, sentinel/context_client.py,
#              sentinel/reasoner.py, sentinel/intervention_executor.py
#  Used by:    system_sentinel.py

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

import httpx

from backend.services.sentinel.models import (
    ProjectWorldModel,
    SentinelMessage,
    SentinelObservation,
    Severity,
    TaskWorldState,
)

from backend.services.sentinel.interrogator import (
    DecisionContext,
    InterrogationInput,
    SelfInterrogator,
)
from backend.services.sentinel.intervention_executor import InterventionExecutor
from backend.services.sentinel.reasoner import SentinelReasoner
from backend.services.sentinel.rules import (
    check_task_stuck as _rule_task_stuck_fn,
    check_wave_stalled as _rule_wave_stalled_fn,
    check_cascade_failure as _rule_cascade_failure_fn,
    check_budget_warning as _rule_budget_warning_fn,
    check_resource_unavailable as _rule_resource_unavailable_fn,
    # Phase 2: world-model-based state detection rules
    check_tasks_ready as _rule_tasks_ready_fn,
    check_wave_complete as _rule_wave_complete_fn,
    check_project_complete as _rule_project_complete_fn,
    check_dead_project as _rule_dead_project_fn,
    check_hollow_completions as _rule_hollow_completions_fn,
)

if TYPE_CHECKING:
    from backend.services.sentinel.bus import SentinelBus
    from backend.services.sentinel.context_client import SentinelContextClient
    from backend.services.sentinel.decision_logger import DecisionLogger
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
    REASSIGN_TIER = "reassign_tier"
    MODIFY_PROMPT = "modify_prompt"
    RESTART_SERVER = "restart_server"


# Map observation categories to the intervention they trigger
_CATEGORY_TO_INTERVENTION: dict[str, tuple[InterventionAction, InterventionTier]] = {
    "task_stuck": (InterventionAction.RELEASE_CLAIM, InterventionTier.AUTO),
    "cascade_failure": (InterventionAction.SKIP_TASK, InterventionTier.SUPERVISED),
    "wave_stalled": (InterventionAction.REORDER_WAVE, InterventionTier.SUPERVISED),
    "resource_unavailable": (InterventionAction.REASSIGN_TIER, InterventionTier.AUTO),
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

    # task_id → error text from last failure (for reasoner diagnosis)
    task_errors: dict[str, str] = field(default_factory=dict)

    # task_id → model tier used (for reassignment decisions)
    task_tiers: dict[str, str] = field(default_factory=dict)


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
        db=None,
        base_url: str = DEFAULT_BASE_URL,
        auth_token: str | None = None,
        reasoner: SentinelReasoner | None = None,
        interrogator: SelfInterrogator | None = None,
        intervention_executor: InterventionExecutor | None = None,
        decision_logger: DecisionLogger | None = None,
    ) -> None:
        self._project_id = project_id
        self._bus = bus
        self._context_client = context_client
        self._progress_manager = progress_manager
        self._db = db
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._running = False
        self._task: asyncio.Task | None = None
        self._state = PlanState()
        self._world_model = ProjectWorldModel(project_id=project_id)
        self._http_client: httpx.AsyncClient | None = None
        self._reasoner = reasoner
        self._interrogator = interrogator
        self._executor = intervention_executor
        self._decision_logger = decision_logger
        # Recent observations for reasoner context
        self._observation_history: list[SentinelObservation] = []

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

    @property
    def world_model(self) -> ProjectWorldModel:
        """Read-only access to the ProjectWorldModel (Phase 2 state detection)."""
        return self._world_model

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
        if self._executor:
            await self._executor.close()
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

    # --- World model helpers ---

    def _ensure_task_world_state(
        self, task_id: str, event: dict,
    ) -> TaskWorldState:
        """Get or create a TaskWorldState entry in the world model.

        Extracts wave and model_tier from the event if available (first
        time a task appears), otherwise preserves existing values.
        """
        if task_id not in self._world_model.tasks:
            wave = event.get("wave", self._world_model.current_wave)
            self._world_model.tasks[task_id] = TaskWorldState(
                id=task_id,
                wave=wave if wave is not None else 0,
                model_tier=event.get("model_tier", ""),
            )
        return self._world_model.tasks[task_id]

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
        # Update world model
        tws = self._ensure_task_world_state(task_id, event)
        tws.status = "running"
        tws.started_at = datetime.fromtimestamp(ts, tz=timezone.utc)
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
        # Update world model
        tws = self._ensure_task_world_state(task_id, event)
        tws.status = "completed"
        tws.completed_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        tws.output_summary = event.get("output_summary", "") or event.get("output", "")
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

        # Capture error text and model tier for reasoner diagnosis
        error_text = event.get("error") or event.get("message") or ""
        model_tier = event.get("model_tier") or ""
        self._state.task_errors[task_id] = error_text[:1000]
        if model_tier:
            self._state.task_tiers[task_id] = model_tier

        # Track wave-level outcomes for consecutive-failure detection
        if self._state.current_wave is not None:
            wave = self._state.current_wave
            outcomes = self._state.wave_outcomes.setdefault(wave, [])
            outcomes.append((task_id, "failed"))

        # Update world model
        tws = self._ensure_task_world_state(task_id, event)
        tws.status = "failed"
        tws.error = event.get("error", "") or event.get("message", "")
        tws.retry_count = self._state.failure_counts[task_id]

        logger.debug(
            "Plan Sentinel: task_failed %s (failures: %d, tier: %s)",
            task_id[:8], self._state.failure_counts[task_id], model_tier or "?",
        )

        # Trigger immediate detection check — cascade failures and wave
        # stalls happen fast and the periodic tick may not fire in time.
        total_failures = sum(
            1 for s in self._state.task_statuses.values()
            if s == TaskState.FAILED
        )
        if total_failures >= self.CASCADE_FAILURE_MIN:
            asyncio.ensure_future(self._tick())

    def _on_task_output(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        """Progress event — update last_progress_at to prevent stuck detection."""
        if not task_id:
            return
        timing = self._state.task_timing.setdefault(task_id, TaskTiming())
        timing.last_progress_at = ts
        # Update world model timing
        self._world_model.timing[f"task:{task_id}:last_progress"] = ts

    def _on_task_needs_review(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        if not task_id:
            return
        self._state.task_statuses[task_id] = TaskState.NEEDS_REVIEW
        timing = self._state.task_timing.setdefault(task_id, TaskTiming())
        timing.last_progress_at = ts
        # Update world model
        tws = self._ensure_task_world_state(task_id, event)
        tws.status = "needs_review"

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
        # Update world model
        tws = self._ensure_task_world_state(task_id, event)
        tws.status = "pending"
        tws.started_at = None

    def _on_wave_checkpoint(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        completed_wave = event.get("wave")
        next_wave = event.get("next_wave")
        if next_wave is not None:
            self._state.current_wave = next_wave
        elif completed_wave is not None:
            self._state.current_wave = completed_wave
        # Update world model
        if completed_wave is not None and completed_wave not in self._world_model.completed_waves:
            self._world_model.completed_waves.append(completed_wave)
        if next_wave is not None:
            self._world_model.current_wave = next_wave
        elif completed_wave is not None:
            self._world_model.current_wave = completed_wave
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
            self._world_model.budget_spent = float(spent)
        if limit is not None:
            self._state.budget_limit = float(limit)
            self._world_model.budget_limit = float(limit)
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
        self._world_model.status = "completed"
        asyncio.ensure_future(self._final_sweep_and_stop("completed"))

    def _on_project_failed(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        logger.info(
            "Plan Sentinel: project_failed for %s", self._project_id,
        )
        self._world_model.status = "failed"
        asyncio.ensure_future(self._final_sweep_and_stop("failed"))

    def _on_project_blocked(
        self, event: dict, task_id: str | None, ts: float,
    ) -> None:
        logger.info(
            "Plan Sentinel: project_blocked for %s", self._project_id,
        )
        self._world_model.status = "blocked"
        asyncio.ensure_future(self._final_sweep_and_stop("blocked"))

    async def _final_sweep_and_stop(self, reason: str) -> None:
        """Run final detection rules, then self-terminate.

        The sentinel owns its own lifecycle — the executor does not tear
        it down. This ensures terminal events are fully processed before
        the sentinel stops.
        """
        try:
            await self._tick()
            logger.info(
                "Plan Sentinel final sweep done for %s (reason=%s, observations=%d)",
                self._project_id, reason, len(self._observation_history),
            )
        except Exception:
            logger.exception(
                "Plan Sentinel final sweep failed for %s", self._project_id,
            )
        finally:
            self._running = False
            # Notify SystemSentinel to clean us from the registry
            try:
                await self._bus.emit(SentinelMessage(
                    topic="plan_sentinel_stopped",
                    source=f"plan_sentinel:{self._project_id}",
                    payload={"project_id": self._project_id, "reason": reason},
                ))
            except Exception:
                pass

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

        # Phase 2: evaluate state detection rules against the world model
        from backend.config import SENTINEL_ORCHESTRATOR_ENABLED
        if SENTINEL_ORCHESTRATOR_ENABLED:
            await self._evaluate_state_detection_rules()

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
        observations.extend(self._rule_resource_unavailable())
        return observations

    def _rule_task_stuck(self) -> list[SentinelObservation]:
        """Detect tasks running > STUCK_THRESHOLD_SECS with no progress."""
        return _rule_task_stuck_fn(
            self._state, self._project_id,
            stuck_threshold_secs=self.STUCK_THRESHOLD_SECS,
        )

    def _rule_wave_stalled(self) -> list[SentinelObservation]:
        """Detect when all tasks in the current wave are failed or stuck."""
        return _rule_wave_stalled_fn(
            self._state, self._project_id,
            stuck_threshold_secs=self.STUCK_THRESHOLD_SECS,
        )

    def _rule_cascade_failure(self) -> list[SentinelObservation]:
        """Detect 3+ consecutive task failures in the same wave."""
        return _rule_cascade_failure_fn(
            self._state, self._project_id,
            cascade_failure_min=self.CASCADE_FAILURE_MIN,
        )

    def _rule_budget_warning(self) -> list[SentinelObservation]:
        """Detect budget usage exceeding threshold."""
        return _rule_budget_warning_fn(
            self._state, self._project_id,
            budget_percent_threshold=self.BUDGET_PERCENT_THRESHOLD,
        )

    def _rule_resource_unavailable(self) -> list[SentinelObservation]:
        """Detect tasks on tiers with offline resources."""
        # Get offline resources from SystemSentinel health trends via the bus
        # For now, query resource states from the system sentinel if available
        offline = set()
        try:
            from backend.services.resource_monitor import ResourceStatus
            if hasattr(self, '_bus') and hasattr(self._bus, '_system_sentinel_ref'):
                for rid, trend in self._bus._system_sentinel_ref.get_all_trends().items():
                    if trend.state.value in ("down", "degraded"):
                        offline.add(rid)
        except Exception:
            pass

        # Also check via DB if we have access — query resource_monitor directly
        if not offline and self._db:
            try:
                # Use the health trend data from SystemSentinel via progress events
                # For now, hardcode known issue: codex_cli without OPENAI_API_KEY
                import os
                if not os.environ.get("OPENAI_API_KEY"):
                    offline.add("codex_cli")
            except Exception:
                pass

        return _rule_resource_unavailable_fn(
            self._state, self._project_id,
            offline_resources=offline,
        )

    # ------------------------------------------------------------------
    # Phase 2: State detection (world-model rules)
    # ------------------------------------------------------------------

    async def _trigger_wave_reassessment(self, obs: SentinelObservation):
        """Collects context, evaluates via LLM, and routes the Athena Loop outcome.

        Flow:
            1. Collect wave context (task outcomes, knowledge, sentinel observations)
            2. Call planner.evaluate_wave_reassessment() → ReassessmentResult
            3. Route on outcome:
               - continue_as_planned → log and resume normal dispatch
               - replan_remaining   → publish replan event for executor
               - escalate_to_human  → publish supervised intervention proposal
        """
        try:
            from backend.models.enums import ReassessmentOutcome
            from backend.services.task_lifecycle import collect_wave_reassessment_context
            from backend.services.planner import PlannerService
            from backend.services.budget import BudgetManager

            if not self._db:
                logger.warning("No DB connection available, cannot trigger wave reassessment.")
                return

            details = obs.details or {}
            wave_number = details.get("wave")
            project_id = obs.project_id

            if wave_number is None:
                logger.warning("wave_complete observation missing 'wave' number in details.")
                return

            logger.info(
                "Wave %d for project %s is complete. Collecting context for reassessment.",
                wave_number, project_id,
            )

            # --- Step 1: Collect wave context ---
            context = await collect_wave_reassessment_context(
                db=self._db,
                project_id=project_id,
                wave_number=wave_number,
            )

            if not context:
                logger.warning(
                    "Failed to collect wave reassessment context for project %s, wave %d.",
                    project_id, wave_number,
                )
                return

            logger.info(
                "Collected wave reassessment context for project %s, wave %d.",
                project_id, wave_number,
            )

            # --- Step 2: Evaluate via LLM ---
            # Obtain budget from DI container (if available), else create a no-op instance
            try:
                from backend.container import Container
                budget = Container.budget()
            except Exception:
                budget = BudgetManager(db=self._db)

            planner = PlannerService(db=self._db, budget=budget)
            result = await planner.evaluate_wave_reassessment(context)

            logger.info(
                "Wave reassessment for project %s, wave %d → %s: %s",
                project_id, wave_number, result.outcome.value, result.rationale,
            )

            # --- Step 3: Persist reassessment decision ---
            reassessment_details = {
                "wave_number": wave_number,
                "outcome": result.outcome.value,
                "rationale": result.rationale,
                "suggested_changes": result.suggested_changes,
                "task_outcome_count": len(context.task_outcomes),
                "knowledge_finding_count": len(context.knowledge_findings),
            }

            if self._db:
                await self._db.execute_write(
                    "INSERT INTO sentinel_observations "
                    "(id, project_id, task_id, category, severity, message, details_json, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING",
                    (
                        str(uuid.uuid4()),
                        project_id,
                        None,
                        "wave_reassessment",
                        obs.severity.value,
                        f"Wave {wave_number} reassessment: {result.outcome.value}",
                        json.dumps(reassessment_details),
                        time.time(),
                    ),
                )

            if self._decision_logger:
                try:
                    await self._decision_logger.log_decision(
                        project_id=project_id,
                        command=f"wave_reassessment:{result.outcome.value}",
                        reasoning=result.rationale,
                        confidence=0.8 if result.outcome == ReassessmentOutcome.CONTINUE_AS_PLANNED else 0.6,
                        outcome=result.outcome.value,
                        details=reassessment_details,
                    )
                except Exception:
                    logger.debug(
                        "Failed to log reassessment decision for project %s",
                        project_id, exc_info=True,
                    )

            # --- Step 3b: Self-interrogation on non-trivial outcomes ---
            if (
                self._interrogator
                and result.outcome != ReassessmentOutcome.CONTINUE_AS_PLANNED
            ):
                try:
                    # Build rich context for the wave reassessment interrogation
                    wave_project_summary = f"Project {project_id}, wave {wave_number} complete"
                    wave_task_outcomes = ""
                    if self._db:
                        try:
                            proj = await self._db.fetchone(
                                "SELECT title, requirements FROM projects WHERE id = $1",
                                (project_id,),
                            )
                            if proj:
                                wave_project_summary = (
                                    f"Project: {proj['title']}\n"
                                    f"Requirements: {(proj['requirements'] or '')[:1000]}"
                                )
                            wave_tasks = await self._db.fetchall(
                                "SELECT title, status, error, output_text FROM tasks "
                                "WHERE project_id = $1 AND wave = $2",
                                (project_id, wave_number),
                            )
                            if wave_tasks:
                                lines = []
                                for wt in wave_tasks:
                                    line = f"- {wt['title']} [{wt['status']}]"
                                    if wt["error"]:
                                        line += f" error: {wt['error'][:150]}"
                                    elif wt["output_text"]:
                                        line += f" output: {wt['output_text'][:150]}"
                                    lines.append(line)
                                wave_task_outcomes = (
                                    f"Wave {wave_number} task outcomes:\n" + "\n".join(lines)
                                )
                        except Exception:
                            pass

                    inp = InterrogationInput(
                        decision_context=DecisionContext.WAVE_REASSESSMENT,
                        proposed_action=f"Wave {wave_number} reassessment: {result.outcome.value}",
                        reasoning=result.rationale,
                        is_coding_task=False,
                        project_summary=wave_project_summary,
                        world_state="\n".join(filter(None, [
                            self._format_state_for_interrogation(),
                            wave_task_outcomes,
                        ])),
                        decision_history=await self._format_decision_history_for_interrogation(),
                    )
                    interrogation = await self._interrogator.interrogate(inp)
                    if interrogation is not None and not interrogation.proceed:
                        logger.info(
                            "Self-interrogation overrode reassessment %s for project %s "
                            "(trigger=%s), escalating to human",
                            result.outcome.value, project_id,
                            interrogation.escalation_trigger,
                        )
                        original_outcome = result.outcome.value
                        result.outcome = ReassessmentOutcome.ESCALATE_TO_HUMAN
                        result.rationale = (
                            f"Original outcome was {original_outcome} but "
                            f"self-interrogation flagged knowledge gap: "
                            f"{interrogation.escalation_trigger}. "
                            f"Original rationale: {result.rationale}"
                        )
                except Exception:
                    logger.debug(
                        "Self-interrogation failed for wave reassessment, proceeding",
                    )

            # --- Step 4: Route based on outcome ---
            if result.outcome == ReassessmentOutcome.CONTINUE_AS_PLANNED:
                logger.info(
                    "Wave %d reassessment: continuing as planned for project %s.",
                    wave_number, project_id,
                )
                # Normal dispatch continues — no action needed.

            elif result.outcome == ReassessmentOutcome.REPLAN_REMAINING:
                logger.info(
                    "Wave %d reassessment: replanning remaining waves for project %s.",
                    wave_number, project_id,
                )
                # Execute the replan directly: cancel future tasks, regenerate plan, decompose
                from backend.services.task_lifecycle import execute_replan
                from backend.services.plan_sync import PlanSyncService
                from backend.services.context_store_client import ContextStoreClient

                plan_sync = None
                try:
                    ctx_client = ContextStoreClient()
                    plan_sync = PlanSyncService(ctx_client, self._db)
                except Exception:
                    logger.debug("Could not create PlanSyncService for revision tracking")

                replan_result = await execute_replan(
                    db=self._db,
                    budget=budget,
                    plan_sync=plan_sync,
                    project_id=project_id,
                    completed_wave=wave_number,
                    rationale=result.rationale,
                    suggested_changes=result.suggested_changes,
                    wave_context_json=context.model_dump_json(),
                )

                # Notify the bus so the executor and other listeners know about the replan
                await self._bus.publish(SentinelMessage(
                    topic="replan_required",
                    source=f"plan_sentinel:{project_id}",
                    payload={
                        "type": "replan_completed",
                        "project_id": project_id,
                        "completed_wave": wave_number,
                        "new_plan_id": replan_result.get("new_plan_id"),
                        "new_tasks_created": replan_result.get("new_tasks_created"),
                        "cancelled_count": replan_result.get("cancelled_count"),
                        "revision_node_id": replan_result.get("revision_node_id"),
                    },
                ))

            elif result.outcome == ReassessmentOutcome.ESCALATE_TO_HUMAN:
                logger.warning(
                    "Wave %d reassessment: escalating to human for project %s. Reason: %s",
                    wave_number, project_id, result.rationale,
                )

                # Build the typed intervention proposal
                from backend.models.schemas import HumanInterventionProposal
                proposal = HumanInterventionProposal(
                    project_id=project_id,
                    wave_number=wave_number,
                    rationale=result.rationale,
                    reassessment_context=context,
                    suggested_actions=result.suggested_changes,
                )
                proposal_dict = proposal.model_dump(mode="json")

                # Build a synthetic observation for the intervention proposal
                escalation_obs = SentinelObservation(
                    project_id=project_id,
                    task_id=None,
                    category="wave_reassessment_escalation",
                    severity=Severity.HIGH,
                    message=(
                        f"Wave {wave_number} reassessment requires human review: "
                        f"{result.rationale}"
                    ),
                    details={
                        "wave": wave_number,
                        "outcome": result.outcome.value,
                        "rationale": result.rationale,
                        "suggested_changes": result.suggested_changes,
                        "intervention_proposal": proposal_dict,
                    },
                )
                await self._publish_intervention_proposal(
                    InterventionAction.REORDER_WAVE,
                    escalation_obs,
                )

                # Emit SSE event so the frontend can surface the proposal
                if self._progress_manager:
                    await self._progress_manager.push_event(
                        project_id=project_id,
                        event_type="escalation_proposal",
                        message=(
                            f"Wave {wave_number} requires human intervention: "
                            f"{result.rationale}"
                        ),
                        **proposal_dict,
                    )

        except Exception:
            logger.exception(
                "Error during wave reassessment for project %s.", self._project_id,
            )


    # Map state detection categories to bus topics
    _STATE_CATEGORY_TO_TOPIC: dict[str, str] = {
        "wave_complete": "state_change",
        "project_complete": "state_change",
        "dead_project": "state_change",
        "tasks_ready": "dispatch_advisory",
        "hollow_completion": "stall_notification",
    }

    async def _evaluate_state_detection_rules(self) -> None:
        """Run Phase 2 state detection rules against the world model.

        When rules fire, publishes state observations to the bus and logs
        each detection as a decision in the sentinel_decisions table.
        """
        state_observations: list[SentinelObservation] = []
        state_observations.extend(_rule_tasks_ready_fn(self._world_model))
        state_observations.extend(_rule_wave_complete_fn(self._world_model))
        state_observations.extend(_rule_project_complete_fn(self._world_model))
        state_observations.extend(_rule_dead_project_fn(self._world_model))
        state_observations.extend(_rule_hollow_completions_fn(self._world_model))

        for obs in state_observations:
            # Dedup: skip if already emitted this observation category+key
            dedup_key = self._state_obs_dedup_key(obs)
            if dedup_key in self._state.emitted_observations:
                continue
            self._state.emitted_observations.add(dedup_key)

            # --- Athena Loop: Trigger Wave Reassessment ---
            if obs.category == "wave_complete":
                asyncio.ensure_future(self._trigger_wave_reassessment(obs))
            # ----------------------------------------------

            # Publish to bus with the appropriate topic
            await self._publish_state_observation(obs)

            # Log decision with reasoning chain
            await self._log_state_decision(obs)

    def _state_obs_dedup_key(self, obs: SentinelObservation) -> str:
        """Generate a deduplication key for a state detection observation."""
        details = obs.details or {}
        if obs.category == "wave_complete":
            return f"state:wave_complete:{details.get('wave', '')}"
        if obs.category == "project_complete":
            return "state:project_complete"
        if obs.category == "dead_project":
            return "state:dead_project"
        if obs.category == "tasks_ready":
            task_ids = sorted(details.get("task_ids", []))
            return f"state:tasks_ready:{','.join(task_ids)}"
        if obs.category == "hollow_completion":
            task_ids = sorted(details.get("task_ids", []))
            return f"state:hollow:{','.join(task_ids)}"
        return f"state:{obs.category}:{obs.observation_id}"

    async def _publish_state_observation(self, obs: SentinelObservation) -> None:
        """Publish a state detection observation to the bus."""
        topic = self._STATE_CATEGORY_TO_TOPIC.get(obs.category, "state_change")

        # Track for reasoner context
        self._observation_history.append(obs)
        if len(self._observation_history) > 50:
            self._observation_history = self._observation_history[-50:]

        msg = SentinelMessage(
            topic=topic,
            source=f"plan_sentinel:{self._project_id}",
            payload={
                "observation_id": obs.observation_id,
                "category": obs.category,
                "severity": obs.severity.value,
                "message": obs.message,
                "project_id": obs.project_id,
                "details": obs.details,
            },
        )
        try:
            await self._bus.publish(msg)
            logger.info(
                "Plan Sentinel state detection [%s]: %s (project %s)",
                obs.category, obs.message, self._project_id,
            )
        except Exception:
            logger.exception(
                "Failed to publish state observation %s to bus",
                obs.observation_id,
            )

    async def _log_state_decision(self, obs: SentinelObservation) -> None:
        """Log a state detection as a decision in the sentinel_decisions table."""
        if not self._decision_logger:
            return

        details = obs.details or {}
        rule = details.get("rule", obs.category)

        # Build a reasoning chain describing what the rule detected and why
        reasoning_parts = [f"Rule '{rule}' triggered: {obs.message}"]
        if obs.category == "wave_complete":
            reasoning_parts.append(
                f"Wave {details.get('wave')}: {details.get('completed', 0)} completed, "
                f"{details.get('failed', 0)} failed out of {details.get('total', 0)} tasks"
            )
        elif obs.category == "project_complete":
            reasoning_parts.append(
                f"All {details.get('total', 0)} tasks terminal: "
                f"{details.get('completed', 0)} completed, {details.get('failed', 0)} failed"
            )
        elif obs.category == "dead_project":
            reasoning_parts.append(
                f"All {details.get('total', 0)} tasks failed or cancelled — "
                "no active or pending work remains"
            )
        elif obs.category == "tasks_ready":
            reasoning_parts.append(
                f"{details.get('count', 0)} pending task(s) in current wave ready for dispatch: "
                f"{details.get('task_ids', [])}"
            )
        elif obs.category == "hollow_completion":
            reasoning_parts.append(
                f"{details.get('count', 0)} task(s) completed with empty output — "
                f"possible silent failures: {details.get('task_ids', [])}"
            )

        # Map category to a SentinelCommand-compatible string
        command_map = {
            "wave_complete": "advance_wave",
            "project_complete": "pause_project",
            "dead_project": "pause_project",
            "tasks_ready": "dispatch_task",
            "hollow_completion": "retry_task",
        }
        command = command_map.get(obs.category, obs.category)

        try:
            await self._decision_logger.log_decision(
                project_id=self._project_id,
                command=command,
                reasoning=" | ".join(reasoning_parts),
                confidence=1.0,  # rule-based detections are deterministic
                outcome=obs.category,
                details=details,
            )
        except Exception:
            logger.debug(
                "Failed to log state decision for %s", obs.observation_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Observation publishing
    # ------------------------------------------------------------------

    async def _publish_observation(self, obs: SentinelObservation) -> None:
        """Publish an observation to the bus and persist via context client.

        Maps observation categories to valid bus topics:
        - budget_warning → resource_alert
        - task_stuck, wave_stalled, cascade_failure → stall_notification
        """
        # Track for reasoner context (cap at 50 most recent)
        self._observation_history.append(obs)
        if len(self._observation_history) > 50:
            self._observation_history = self._observation_history[-50:]

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

        # Persist to SQLite (durable — survives sentinel teardown)
        if self._db:
            try:
                import json as _json
                await self._db.execute_write(
                    "INSERT INTO sentinel_observations "
                    "(id, project_id, task_id, category, severity, message, details_json, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING",
                    (
                        obs.observation_id,
                        obs.project_id,
                        obs.task_id,
                        obs.category,
                        obs.severity.value,
                        obs.message,
                        _json.dumps(obs.details) if obs.details else None,
                        obs.timestamp.timestamp() if obs.timestamp else time.time(),
                    ),
                )
            except Exception:
                logger.exception(
                    "Failed to persist observation %s to DB", obs.observation_id,
                )

        # Persist to context store (knowledge graph — for semantic search)
        if self._context_client:
            try:
                await self._context_client.save_observation(obs)
            except Exception:
                logger.exception(
                    "Failed to persist observation %s to context store", obs.observation_id,
                )

    # ------------------------------------------------------------------
    # Intervention handling
    # ------------------------------------------------------------------

    # Map reasoner recommended_action strings to InterventionAction enum
    _REASONER_ACTION_MAP: dict[str, InterventionAction] = {
        "retry_task": InterventionAction.RETRY_TASK,
        "retry_as_is": InterventionAction.RETRY_TASK,
        "release_claim": InterventionAction.RELEASE_CLAIM,
        "skip_task": InterventionAction.SKIP_TASK,
        "skip": InterventionAction.SKIP_TASK,
        "reorder_wave": InterventionAction.REORDER_WAVE,
        "reassign_tier": InterventionAction.REASSIGN_TIER,
        "modify_prompt": InterventionAction.MODIFY_PROMPT,
    }

    async def _handle_intervention(self, obs: SentinelObservation) -> None:
        """Determine and execute the appropriate intervention for an observation.

        Runs the iterative 5-Whys reasoner (if available) to investigate root
        cause before deciding on an action.  Routes based on the reasoning
        result:
        - Confident result (>= threshold) → use reasoner's recommended action
        - Low confidence or knowledge gaps → escalate with full why-chain
        - Reasoner unavailable → fall back to rule-based category mapping

        The full reasoning chain is logged to the sentinel_decisions table.
        """
        entry = _CATEGORY_TO_INTERVENTION.get(obs.category)
        if entry is None:
            return

        action, tier = entry

        # For cascade failures: if all failures are on the same tier,
        # auto-reassign to a fallback tier instead of retrying or escalating.
        # This handles the "codex_cli is broken, switch to gemini_cli" case.
        if obs.category == "cascade_failure" and obs.details:
            same_tier = obs.details.get("same_tier_failure", False)
            if same_tier:
                action = InterventionAction.REASSIGN_TIER
                tier = InterventionTier.AUTO
            else:
                # Mixed tiers — try auto-retry on the last failed task
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

        # --- Inject task error text for diagnosis ---
        # For cascade_failure and task_stuck, enrich observation details
        # with error text from PlanState so the reasoner can diagnose.
        if obs.category in ("cascade_failure", "task_stuck"):
            error_context: dict[str, str] = {}
            if obs.category == "task_stuck" and obs.task_id:
                # Single task — grab its error if available
                err = self._state.task_errors.get(obs.task_id, "")
                if err:
                    error_context[obs.task_id] = err
            elif obs.category == "cascade_failure":
                # Multiple failed tasks — collect all their errors
                failed_ids = (obs.details or {}).get("failed_task_ids", [])
                for tid in failed_ids:
                    err = self._state.task_errors.get(tid, "")
                    if err:
                        error_context[tid] = err
            if error_context:
                obs.details = {**(obs.details or {}), "task_errors": error_context}

        # --- Run iterative 5-Whys reasoner ---
        reasoning_result = None
        if self._reasoner:
            try:
                reasoning_result = await self._reasoner.reason(
                    obs, self._state, self._observation_history,
                )
            except Exception:
                logger.debug(
                    "Reasoner failed for %s, falling back to rule-only",
                    obs.observation_id,
                )

        # --- Route based on reasoning result ---
        if reasoning_result is not None:
            # Override action if the reasoner recommends a specific intervention
            reasoner_action = self._REASONER_ACTION_MAP.get(
                reasoning_result.recommended_action,
            )

            if reasoning_result.escalation_reason or reasoning_result.confidence < 0.5:
                # Knowledge gap or low confidence → always escalate
                logger.info(
                    "Reasoner escalating for %s (confidence=%.2f, gaps=%d, reason=%s)",
                    obs.observation_id[:8],
                    reasoning_result.confidence,
                    len(reasoning_result.knowledge_gaps),
                    reasoning_result.escalation_reason or "low confidence",
                )
                tier = InterventionTier.SUPERVISED
            elif reasoner_action is not None:
                # Confident with a concrete action → use it
                action = reasoner_action
                tier = InterventionTier.AUTO
                logger.info(
                    "Reasoner recommends %s for %s (confidence=%.2f, root_cause=%s)",
                    action.value, obs.observation_id[:8],
                    reasoning_result.confidence,
                    reasoning_result.root_cause[:80],
                )

                # Merge fix_params from the reasoner into obs.details so the
                # executor can access them (e.g. prompt_additions, new_tier).
                fix_params = getattr(reasoning_result, "fix_params", None) or {}
                if fix_params:
                    obs.details = {**(obs.details or {}), **fix_params}

            # Log the full reasoning chain to sentinel_decisions
            await self._log_reasoning_decision(obs, reasoning_result, action)

        # --- Self-interrogation gate: 6 questions before every action ---
        interrogation_result = None
        if self._interrogator and tier == InterventionTier.AUTO:
            try:
                # Determine if this is a coding-related intervention
                is_coding = obs.category in (
                    "task_stuck", "cascade_failure", "resource_unavailable",
                )

                # Gather task context from DB if available
                task_desc = ""
                project_summary = ""
                if self._db and obs.task_id:
                    try:
                        task_row = await self._db.fetchone(
                            "SELECT title, description, task_type FROM tasks WHERE id = $1",
                            (obs.task_id,),
                        )
                        if task_row:
                            task_desc = (
                                f"Task: {task_row['title']} ({task_row['task_type']})\n"
                                f"{task_row['description'][:1500]}"
                            )
                    except Exception:
                        pass
                if self._db:
                    try:
                        proj = await self._db.fetchone(
                            "SELECT title, requirements FROM projects WHERE id = $1",
                            (obs.project_id,),
                        )
                        if proj:
                            project_summary = (
                                f"Project: {proj['title']}\n"
                                f"Requirements: {(proj['requirements'] or '')[:1000]}"
                            )
                    except Exception:
                        pass

                # Prepare error context for interrogator
                interrogation_error_text = ""
                if obs.details:
                    task_errors = obs.details.get("task_errors")
                    if isinstance(task_errors, dict):
                        if obs.category == "cascade_failure":
                            # For cascades, serialize all errors
                            interrogation_error_text = json.dumps(task_errors, indent=2)
                        elif obs.task_id:
                            # For single-task issues, get the specific error
                            interrogation_error_text = task_errors.get(obs.task_id, "")

                inp = InterrogationInput(
                    decision_context=DecisionContext.SENTINEL_INTERVENTION,
                    proposed_action=f"{action.value} on task {obs.task_id or 'unknown'}",
                    reasoning=(
                        reasoning_result.root_cause if reasoning_result
                        else f"Rule-based: {obs.category}"
                    ),
                    is_coding_task=is_coding,
                    task_description=task_desc,
                    project_summary=project_summary,
                    error_text=interrogation_error_text[:1000],
                    world_state=self._format_state_for_interrogation(),
                    decision_history=await self._format_decision_history_for_interrogation(),
                )
                interrogation_result = await self._interrogator.interrogate(inp)
            except Exception:
                logger.debug(
                    "Self-interrogation failed for %s, proceeding with original tier",
                    obs.observation_id,
                )

            if interrogation_result is not None and not interrogation_result.proceed:
                # Knowledge gap detected — escalate instead of auto-acting
                logger.info(
                    "Self-interrogation BLOCKED auto intervention %s for %s "
                    "(trigger=%s, confidence=%.2f)",
                    action.value,
                    obs.observation_id[:8],
                    interrogation_result.escalation_trigger,
                    interrogation_result.overall_confidence,
                )
                tier = InterventionTier.SUPERVISED

        if tier == InterventionTier.AUTO:
            await self._execute_auto_intervention(
                action, obs,
                reasoning_result=reasoning_result,
                interrogation_result=interrogation_result,
            )
        else:
            await self._publish_intervention_proposal(
                action, obs,
                reasoning_result=reasoning_result,
                interrogation_result=interrogation_result,
            )

    async def _log_reasoning_decision(
        self,
        obs: SentinelObservation,
        result: Any,
        action: InterventionAction,
    ) -> None:
        """Persist the full reasoning chain to the sentinel_decisions table."""
        if not self._db:
            return

        # Serialize why_chain for storage
        chain_data = []
        for step in result.why_chain:
            chain_data.append({
                "question": step.question,
                "sources_queried": step.sources_queried,
                "evidence_found": step.evidence_found,
                "conclusion": step.conclusion,
            })

        details = {
            "observation_id": obs.observation_id,
            "category": obs.category,
            "why_chain": chain_data,
            "root_cause": result.root_cause,
            "knowledge_gaps": result.knowledge_gaps,
            "escalation_reason": result.escalation_reason,
            "recommended_action": result.recommended_action,
        }

        reasoning_summary = (
            f"5-Whys ({len(result.why_chain)} steps): "
            f"root_cause={result.root_cause}, "
            f"action={result.recommended_action}"
        )

        try:
            await self._db.execute_write(
                "INSERT INTO sentinel_decisions "
                "(id, project_id, timestamp, command, reasoning, confidence, "
                "outcome, details_json) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING",
                (
                    uuid.uuid4().hex,
                    self._project_id,
                    time.time(),
                    action.value,
                    reasoning_summary,
                    result.confidence,
                    None,  # outcome filled after execution
                    json.dumps(details, default=str),
                ),
            )
        except Exception:
            logger.exception(
                "Failed to log reasoning decision for %s", obs.observation_id,
            )

    # --- Self-interrogation helpers ---

    def _format_state_for_interrogation(self) -> str:
        """Summarize current plan state for the interrogator."""
        lines = [f"Wave: {self._state.current_wave}"]
        status_counts: dict[str, int] = {}
        for s in self._state.task_statuses.values():
            status_counts[s.value] = status_counts.get(s.value, 0) + 1
        if status_counts:
            lines.append(f"Tasks: {json.dumps(status_counts)}")
        failing = {t: c for t, c in self._state.failure_counts.items() if c > 0}
        if failing:
            lines.append(f"Failures: {len(failing)} tasks with failures")
        if self._state.budget_limit > 0:
            pct = self._state.budget_spent / self._state.budget_limit * 100
            lines.append(f"Budget: {pct:.0f}% spent")
        return "\n".join(lines)

    async def _format_decision_history_for_interrogation(self) -> str:
        """Gather real decision history from DB + in-memory state."""
        parts = []

        # DB-backed history: sentinel observations for this project
        if self._db:
            try:
                rows = await self._db.fetchall(
                    "SELECT message, category, details_json FROM sentinel_observations "
                    "WHERE project_id = $1 AND category IN "
                    "('intervention_result', 'intervention_proposal', "
                    "'interrogation_concern', 'wave_reassessment') "
                    "ORDER BY created_at DESC LIMIT 8",
                    (self._project_id,),
                )
                if rows:
                    lines = [r["message"][:150] for r in rows]
                    parts.append(
                        "Recent decisions (from DB):\n"
                        + "\n".join(f"- {l}" for l in lines)
                    )
            except Exception:
                pass

        # In-memory session history (supplement, not primary)
        if self._state.handled_interventions:
            items = list(self._state.handled_interventions)[-5:]
            parts.append(
                "This session's interventions: "
                + ", ".join(f"{a} on {t}" for a, t in items)
            )

        return "\n".join(parts) if parts else "No prior decisions found."

    # --- Auto-tier interventions ---

    async def _execute_auto_intervention(
        self, action: InterventionAction, obs: SentinelObservation,
        *, reasoning_result=None, interrogation_result=None,
    ) -> None:
        """Execute an auto-tier intervention via the InterventionExecutor
        (or legacy inline calls) and publish the outcome."""
        success = False
        result_detail = ""

        try:
            if self._executor:
                ir = await self._dispatch_via_executor(action, obs)
                success = ir.success
                result_detail = ir.detail
                # Update internal state on successful retry/release
                task_id = ir.task_id or obs.task_id
                if success and task_id:
                    if action == InterventionAction.RETRY_TASK:
                        self._state.retry_counts[task_id] = (
                            self._state.retry_counts.get(task_id, 0) + 1
                        )
                        self._state.task_statuses[task_id] = TaskState.PENDING
                    elif action == InterventionAction.RELEASE_CLAIM:
                        self._state.task_statuses[task_id] = TaskState.PENDING
                if success and action == InterventionAction.REASSIGN_TIER:
                    # Mark all reassigned tasks as pending in internal state
                    for tid in (obs.details or {}).get("failed_task_ids", []):
                        self._state.task_statuses[tid] = TaskState.PENDING
                        self._state.failure_counts[tid] = 0
                if success and action == InterventionAction.MODIFY_PROMPT:
                    task_id_mp = ir.task_id or obs.task_id
                    if task_id_mp:
                        self._state.task_statuses[task_id_mp] = TaskState.PENDING
            else:
                # Legacy fallback — inline HTTP calls
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

        # Persist to DB (durable — survives sentinel teardown)
        if self._db:
            try:
                import uuid as _uuid
                details = {
                    "action": action.value,
                    "tier": InterventionTier.AUTO.value,
                    "reasoning": result_detail,
                    "success": success,
                    "observation_id": obs.observation_id,
                    "task_id": obs.task_id,
                }
                await self._db.execute_write(
                    "INSERT INTO sentinel_observations "
                    "(id, project_id, task_id, category, severity, message, details_json, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING",
                    (
                        str(_uuid.uuid4()),
                        self._project_id,
                        obs.task_id,
                        "intervention_result",
                        obs.severity.value,
                        f"Auto {action.value}: {'succeeded' if success else 'failed'} — {result_detail}",
                        json.dumps(details),
                        time.time(),
                    ),
                )
            except Exception:
                logger.exception(
                    "Failed to persist intervention result for %s", self._project_id,
                )

        # Log decision to audit trail
        if self._decision_logger:
            reasoning_parts = [
                f"Auto intervention '{action.value}' triggered by {obs.category}: {obs.message}",
            ]
            if reasoning_result is not None:
                reasoning_parts.append(f"5-whys diagnosis: {reasoning_result.diagnosis}")
                if reasoning_result.supporting_evidence:
                    reasoning_parts.append(
                        f"Evidence: {'; '.join(reasoning_result.supporting_evidence)}"
                    )
            reasoning_parts.append(
                f"Outcome: {'succeeded' if success else 'failed'} — {result_detail}"
            )
            try:
                await self._decision_logger.log_decision(
                    project_id=self._project_id,
                    command=action.value,
                    reasoning=" | ".join(reasoning_parts),
                    confidence=reasoning_result.confidence if reasoning_result else 1.0,
                    outcome="succeeded" if success else "failed",
                    details={
                        "tier": InterventionTier.AUTO.value,
                        "observation_id": obs.observation_id,
                        "task_id": obs.task_id,
                        "category": obs.category,
                        "result_detail": result_detail,
                    },
                )
            except Exception:
                logger.debug(
                    "Failed to log auto intervention decision for %s",
                    obs.observation_id, exc_info=True,
                )

    async def _dispatch_via_executor(
        self, action: InterventionAction, obs: SentinelObservation,
    ):
        """Route an action to the correct InterventionExecutor method."""
        from backend.services.sentinel.intervention_executor import InterventionResult

        assert self._executor is not None
        if action == InterventionAction.RETRY_TASK:
            return await self._executor.retry_task(obs)
        elif action == InterventionAction.RELEASE_CLAIM:
            return await self._executor.release_claim(obs)
        elif action == InterventionAction.SKIP_TASK:
            return await self._executor.skip_task(obs)
        elif action == InterventionAction.REORDER_WAVE:
            return await self._executor.reorder_wave(obs)
        elif action == InterventionAction.REASSIGN_TIER:
            return await self._executor.reassign_tier(obs)
        elif action == InterventionAction.MODIFY_PROMPT:
            return await self._executor.modify_prompt(obs)
        elif action == InterventionAction.RESTART_SERVER:
            reason = (obs.details or {}).get("reason", obs.description or "stale code detected")
            return await self._executor.restart_server(reason)
        return InterventionResult(
            action=action.value, success=False, detail="unknown action",
        )

    # --- Legacy inline HTTP interventions (used when no executor is wired) ---

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
        self,
        action: InterventionAction,
        obs: SentinelObservation,
        *,
        reasoning_result=None,
        interrogation_result=None,
    ) -> None:
        """Publish a supervised intervention proposal for user approval.

        When a ``reasoning_result`` is provided (from SentinelReasoner), its
        full why-chain, root cause, confidence, and knowledge gaps are
        included in the proposal payload so the human reviewer has full
        investigation context.
        """
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

        # Include self-interrogation results when they caused the escalation
        if interrogation_result is not None:
            payload["self_interrogation"] = {
                "proceed": interrogation_result.proceed,
                "escalation_trigger": interrogation_result.escalation_trigger,
                "overall_confidence": interrogation_result.overall_confidence,
                "answers": [
                    {
                        "question": a.question,
                        "answer": a.answer,
                        "confident": a.confident,
                    }
                    for a in interrogation_result.answers
                ],
            }

        # Include full reasoning chain when available
        if reasoning_result is not None:
            chain_summary = []
            for step in reasoning_result.why_chain:
                chain_summary.append({
                    "question": step.question,
                    "sources_queried": step.sources_queried,
                    "conclusion": step.conclusion,
                })
            payload["llm_diagnosis"] = {
                "root_cause": reasoning_result.root_cause,
                "recommended_action": reasoning_result.recommended_action,
                "confidence": reasoning_result.confidence,
                "why_chain": chain_summary,
                "knowledge_gaps": reasoning_result.knowledge_gaps,
                "escalation_reason": reasoning_result.escalation_reason,
            }

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

        # Persist to DB (durable — survives sentinel teardown)
        if self._db:
            try:
                import uuid as _uuid
                details = {
                    "action": action.value,
                    "tier": InterventionTier.SUPERVISED.value,
                    "reasoning": payload.get("recommendation", ""),
                    "observation_id": obs.observation_id,
                    "task_id": obs.task_id,
                }
                if reasoning_result is not None:
                    details["llm_diagnosis"] = payload.get("llm_diagnosis")
                await self._db.execute_write(
                    "INSERT INTO sentinel_observations "
                    "(id, project_id, task_id, category, severity, message, details_json, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING",
                    (
                        str(_uuid.uuid4()),
                        self._project_id,
                        obs.task_id,
                        "intervention_proposal",
                        obs.severity.value,
                        f"Proposed {action.value}: {payload.get('recommendation', obs.message)}",
                        json.dumps(details),
                        time.time(),
                    ),
                )
            except Exception:
                logger.exception(
                    "Failed to persist intervention proposal for %s", self._project_id,
                )

        # Log decision to audit trail
        if self._decision_logger:
            reasoning_parts = [
                f"Supervised proposal '{action.value}' for {obs.category}: {obs.message}",
            ]
            if reasoning_result is not None:
                reasoning_parts.append(f"5-whys diagnosis: {reasoning_result.diagnosis}")
                reasoning_parts.append(
                    f"Recommended action: {reasoning_result.recommended_action}"
                )
                reasoning_parts.append(f"Confidence: {reasoning_result.confidence:.2f}")
                if reasoning_result.supporting_evidence:
                    reasoning_parts.append(
                        f"Evidence: {'; '.join(reasoning_result.supporting_evidence)}"
                    )
            reasoning_parts.append(
                f"Recommendation: {payload.get('recommendation', obs.message)}"
            )
            try:
                await self._decision_logger.log_decision(
                    project_id=self._project_id,
                    command=action.value,
                    reasoning=" | ".join(reasoning_parts),
                    confidence=reasoning_result.confidence if reasoning_result else 0.5,
                    outcome="proposed",
                    details={
                        "tier": InterventionTier.SUPERVISED.value,
                        "observation_id": obs.observation_id,
                        "task_id": obs.task_id,
                        "category": obs.category,
                        "recommendation": payload.get("recommendation", ""),
                        "llm_diagnosis": payload.get("llm_diagnosis"),
                    },
                )
            except Exception:
                logger.debug(
                    "Failed to log supervised proposal decision for %s",
                    obs.observation_id, exc_info=True,
                )
