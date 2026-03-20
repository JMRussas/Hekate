#  Orchestration Engine - Task Executor
#
#  Async worker pool that executes tasks via Claude API or Ollama,
#  with tool support, dependency resolution, budget enforcement,
#  wave-based dispatch, and context forwarding.
#
#  Depends on: backend/config.py, backend/db/connection.py,
#              services/budget.py, services/model_router.py,
#              services/resource_monitor.py, services/progress.py,
#              services/task_lifecycle.py, services/git_service.py,
#              tools/registry.py
#  Used by:    container.py, app.py (background task)

import asyncio
import json
import logging
import time
from pathlib import Path

import anthropic

from backend.config import (
    ANTHROPIC_API_KEY,
    GIT_BRANCH_PREFIX,
    MAX_CONCURRENT_TASKS,
    RESOURCE_SKIP_SECONDS,
    SHUTDOWN_GRACE_SECONDS,
    STALENESS_TIMEOUT,
    TICK_INTERVAL,
    WAVE_CHECKPOINTS,
    cfg,
)

# Feature flag: when True, the executor skips self-dispatch and waits for
# Odin to publish dispatch_command events via the sentinel bus.
ODIN_DISPATCH = cfg("odin.dispatch_enabled", False)
from backend.models.enums import ExecutionMode, ModelTier, ProjectStatus, TaskStatus
from backend.db.connection import parse_rowcount
from backend.services.model_router import calculate_cost, get_model_id
from backend.services.git_service import GitService
from backend.services.task_lifecycle import execute_task
from backend.utils.slug_utils import slugify

logger = logging.getLogger("orchestration.executor")

# Token estimate for budget reservation before task execution
_EST_TASK_INPUT_TOKENS = 1500  # system prompt + context + tool definitions

from backend.services.model_router import TIER_TO_PROVIDER as _TIER_TO_PROVIDER


class Executor:
    """Async task executor with concurrency control and tool support."""

    def __init__(self, db, budget, progress, resource_monitor, tool_registry,
                 http_client=None, rag_cache=None, diagnostic_ingester=None,
                 quota_manager=None, bus=None):
        self._db = db
        self._budget = budget
        self._progress = progress
        self._resource_monitor = resource_monitor
        self._tool_registry = tool_registry
        self._http = http_client  # Shared httpx client for Ollama calls
        self._rag_cache = rag_cache
        self._diagnostic_ingester = diagnostic_ingester
        self._quota_manager = quota_manager  # Optional; skips quota check when None
        self._bus = bus  # SentinelBus for dispatch_command subscription (Odin mode)
        self._max_concurrent = MAX_CONCURRENT_TASKS
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
        self._task: asyncio.Task | None = None
        self._running = False
        self._dispatched: set[str] = set()  # Task IDs currently dispatched (prevents duplicate dispatch)
        self._in_flight: set[asyncio.Task] = set()  # Tracked task handles for clean shutdown
        self._client: anthropic.AsyncAnthropic | None = None  # Shared Anthropic client
        self._retry_after: dict[str, float] = {}  # task_id → earliest retry timestamp
        self._resource_skip_until: dict[str, float] = {}  # resource → skip until timestamp
        self._git = GitService(db=db)
        self._branch_confirmed: set[str] = set()  # project IDs with branch already verified
        self._worktrees: dict[str, str] = {}  # project_id → worktree path
        self._bus_unsub: callable | None = None  # Unsubscribe handle for dispatch_command

    async def start(self):
        """Start the executor loop. Recovers stale tasks from prior crashes.

        When ODIN_DISPATCH is enabled and a bus is available, subscribes to
        ``dispatch_command`` events so Odin controls which tasks run.
        """
        if self._running:
            return
        self._running = True
        self._client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
        await self._recover_stale_tasks()

        # Subscribe to Odin dispatch commands when the feature flag is on
        if ODIN_DISPATCH and self._bus is not None:
            self._bus_unsub = self._bus.on("dispatch_command", self._handle_dispatch_command)
            logger.info("Executor subscribed to dispatch_command (Odin mode)")
        elif ODIN_DISPATCH and self._bus is None:
            logger.warning(
                "ODIN_DISPATCH enabled but no bus available — "
                "falling back to self-dispatch"
            )

        self._task = asyncio.create_task(self._run_loop())
        logger.info("Executor started")

    async def dispatch_task(self, task_id: str) -> bool:
        """Dispatch a specific task by ID.

        Called via bus ``dispatch_command`` in Odin mode, or directly by Odin
        tools.  Publishes ``worker_event`` messages back to the bus so Odin
        can track task lifecycle.
        """
        row = await self._db.fetchone("SELECT * FROM tasks WHERE id = $1", (task_id,))
        if not row:
            logger.warning("dispatch_task: task %s not found", task_id[:8])
            return False
        if row["status"] not in (TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.WAITING):
            logger.warning("dispatch_task: task %s in %s state, skipping", task_id[:8], row["status"])
            return False

        # Transition to queued and execute
        claim_status = await self._db.execute_write(
            "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3 AND status = $4",
            (TaskStatus.QUEUED, time.time(), task_id, row["status"]),
        )
        if parse_rowcount(claim_status) == 0:
            return False

        # Notify Odin the task is starting
        await self._publish_worker_event(task_id, row["project_id"], "started", row["title"])

        self._dispatched.add(task_id)
        handle = asyncio.create_task(
            self._execute_and_report(
                task_row=row,
                task_id=task_id,
            )
        )
        self._in_flight.add(handle)
        handle.add_done_callback(self._in_flight.discard)
        logger.info("dispatch_task: dispatched %s (%s)", task_id[:8], row["title"])
        return True

    async def _execute_and_report(self, task_row, task_id: str) -> None:
        """Run execute_task and publish a worker_event on completion or failure."""
        project_id = task_row["project_id"]
        title = task_row["title"]
        try:
            await execute_task(
                task_row=task_row,
                est_cost=0.0,  # CLI tiers are subscription-billed
                db=self._db,
                budget=self._budget,
                progress=self._progress,
                tool_registry=self._tool_registry,
                http_client=self._http,
                client=self._client,
                semaphore=self._semaphore,
                dispatched=self._dispatched,
                retry_after=self._retry_after,
                rag_cache=self._rag_cache,
                diagnostic_ingester=self._diagnostic_ingester,
            )
            # Check final status from DB to report accurately
            final = await self._db.fetchone(
                "SELECT status, error FROM tasks WHERE id = $1", (task_id,)
            )
            if final and final["status"] in (TaskStatus.COMPLETED, TaskStatus.NEEDS_REVIEW):
                await self._publish_worker_event(task_id, project_id, "completed", title)
            elif final:
                await self._publish_worker_event(
                    task_id, project_id, "failed", title,
                    error=final.get("error", ""),
                )
        except Exception as exc:
            logger.error("execute_and_report: task %s raised: %s", task_id[:8], exc)
            await self._publish_worker_event(
                task_id, project_id, "failed", title, error=str(exc),
            )

    async def _handle_dispatch_command(self, message) -> None:
        """Handle a dispatch_command from Odin via the sentinel bus.

        Expected payload::

            {
                "task_id": "abc123",
                "provider": "claude_code",   # optional
                "model": "...",              # optional
                "priority": 1,              # optional
                "timeout": 600,             # optional
            }
        """
        payload = message.payload or {}
        task_id = payload.get("task_id")
        if not task_id:
            logger.warning("dispatch_command missing task_id: %s", payload)
            return

        logger.info(
            "dispatch_command received: task=%s provider=%s",
            task_id[:8], payload.get("provider", "default"),
        )
        ok = await self.dispatch_task(task_id)
        if not ok:
            logger.warning("dispatch_command: could not dispatch task %s", task_id[:8])

    async def _publish_worker_event(
        self, task_id: str, project_id: str, event: str, title: str = "",
        error: str = "",
    ) -> None:
        """Publish a worker_event to the bus (best-effort, never raises)."""
        if self._bus is None:
            return
        try:
            await self._bus.publish_dict("worker_event", {
                "task_id": task_id,
                "project_id": project_id,
                "event": event,  # started | completed | failed
                "title": title,
                "error": error,
            }, source="executor")
        except Exception:
            logger.debug("Failed to publish worker_event for %s", task_id[:8])

    async def stop(self, grace_seconds: float | None = None):
        """Stop the executor loop, waiting for in-flight tasks to finish.

        Args:
            grace_seconds: How long to wait for in-flight tasks before cancelling.
                           Defaults to SHUTDOWN_GRACE_SECONDS from config.
        """
        if grace_seconds is None:
            grace_seconds = SHUTDOWN_GRACE_SECONDS

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Wait for in-flight tasks up to the grace period
        if self._in_flight:
            logger.info("Waiting up to %.0fs for %d in-flight task(s)", grace_seconds, len(self._in_flight))
            done, pending = await asyncio.wait(
                list(self._in_flight), timeout=grace_seconds,
            )
            if pending:
                logger.warning("Grace period expired, cancelling %d task(s)", len(pending))
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            self._in_flight.clear()

        # Reset internally-dispatched running/queued tasks to pending so they can
        # be re-dispatched after restart. Exclude externally-claimed tasks — their
        # executor is independent of this process lifecycle.
        reset_status = await self._db.execute_write(
            "UPDATE tasks SET status = $1, error = $2, updated_at = $3 "
            "WHERE status IN ($4, $5) AND claimed_by IS NULL",
            (TaskStatus.PENDING, "Interrupted by shutdown", time.time(),
             TaskStatus.RUNNING, TaskStatus.QUEUED),
        )
        reset_count = parse_rowcount(reset_status)
        if reset_count > 0:
            logger.info("Reset %d running/queued task(s) to pending on shutdown", reset_count)

        # Unsubscribe from bus before clearing state
        if self._bus_unsub is not None:
            self._bus_unsub()
            self._bus_unsub = None

        # Close the shared Anthropic client and clear state
        if self._client:
            await self._client.close()
            self._client = None
        self._dispatched.clear()
        self._retry_after.clear()
        self._resource_skip_until.clear()
        self._branch_confirmed.clear()
        logger.info("Executor stopped")

    async def _run_loop(self):
        """Main executor loop. Runs every tick_interval seconds."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Tick error: %s", e)
            await asyncio.sleep(TICK_INTERVAL)

    async def _recover_stale_tasks(self):
        """Reset tasks stuck in 'running' or 'queued' from a prior crash.

        Runs once on startup before the tick loop — all running/queued tasks
        are stale by definition since no executor was dispatching them.
        Only increments retry_count for RUNNING tasks (QUEUED tasks hadn't
        started execution, so consuming a retry attempt would be wrong).
        """
        stale = await self._db.fetchall(
            "SELECT id, title, status, project_id, retry_count FROM tasks "
            "WHERE status IN ($1, $2) AND claimed_by IS NULL",
            (TaskStatus.RUNNING, TaskStatus.QUEUED),
        )
        if not stale:
            return

        now = time.time()
        for row in stale:
            # Check if any dependencies are incomplete → waiting or blocked
            dep_count = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM task_deps d "
                "JOIN tasks dep ON dep.id = d.depends_on "
                "WHERE d.task_id = $1 AND dep.status != $2",
                (row["id"], TaskStatus.COMPLETED),
            )
            has_unmet_deps = dep_count and dep_count["cnt"] > 0
            if has_unmet_deps:
                stuck_count = await self._db.fetchone(
                    "SELECT COUNT(*) as cnt FROM task_deps d "
                    "JOIN tasks dep ON dep.id = d.depends_on "
                    "WHERE d.task_id = $1 AND dep.status IN ($2, $3)",
                    (row["id"], TaskStatus.FAILED, TaskStatus.CANCELLED),
                )
                has_stuck = stuck_count and stuck_count["cnt"] > 0
                new_status = TaskStatus.BLOCKED if has_stuck else TaskStatus.WAITING
            else:
                new_status = TaskStatus.PENDING

            # Only count as a retry attempt if the task was actually running
            if row["status"] == TaskStatus.RUNNING:
                await self._db.execute_write(
                    "UPDATE tasks SET status = $1, retry_count = retry_count + 1, "
                    "error = $2, updated_at = $3 WHERE id = $4",
                    (new_status,
                     f"Recovered from stale state (retry {row['retry_count'] + 1})",
                     now, row["id"]),
                )
            else:
                await self._db.execute_write(
                    "UPDATE tasks SET status = $1, "
                    "error = $2, updated_at = $3 WHERE id = $4",
                    (new_status,
                     "Recovered from queued state after restart",
                     now, row["id"]),
                )
        logger.info("Recovered %d stale task(s) to pending/blocked", len(stale))

    async def _sweep_stale_tasks(self):
        """Detect and reset zombie tasks that are running but have no live coroutine.

        A task is stale when:
        - DB status is 'running'
        - started_at + STALENESS_TIMEOUT has elapsed
        - task_id is NOT in _dispatched (coroutine already exited)

        Resets stale tasks to pending (or blocked if deps unmet) with retry_count + 1.
        Runs every tick but short-circuits quickly when nothing is stale.
        """
        now = time.time()
        cutoff = now - STALENESS_TIMEOUT

        stale = await self._db.fetchall(
            "SELECT id, title, project_id, retry_count, max_retries FROM tasks "
            "WHERE status = $1 AND started_at IS NOT NULL AND started_at < $2 "
            "AND claimed_by IS NULL",
            (TaskStatus.RUNNING, cutoff),
        )
        if not stale:
            return

        for row in stale:
            task_id = row["id"]

            # If the coroutine is still tracked, it's alive but slow — don't touch it
            if task_id in self._dispatched:
                logger.debug("SWEEP: task %s still dispatched, skipping", task_id[:8])
                continue

            # Check if retry budget is exhausted
            if row["retry_count"] >= row["max_retries"]:
                await self._db.execute_write(
                    "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                    (TaskStatus.NEEDS_REVIEW,
                     f"Zombie detected: running for >{STALENESS_TIMEOUT}s with no live coroutine "
                     f"(retries exhausted: {row['retry_count']}/{row['max_retries']})",
                     now, task_id),
                )
                await self._progress.push_event(
                    row["project_id"], "task_needs_review",
                    f"{row['title']}: zombie detected, retries exhausted",
                    task_id=task_id,
                )
                logger.warning("SWEEP: task %s zombie, retries exhausted → needs_review", task_id[:8])
                continue

            # Check deps to decide pending vs waiting vs blocked
            dep_count = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM task_deps d "
                "JOIN tasks dep ON dep.id = d.depends_on "
                "WHERE d.task_id = $1 AND dep.status != $2",
                (task_id, TaskStatus.COMPLETED),
            )
            has_unmet_deps = dep_count and dep_count["cnt"] > 0
            if has_unmet_deps:
                stuck_count = await self._db.fetchone(
                    "SELECT COUNT(*) as cnt FROM task_deps d "
                    "JOIN tasks dep ON dep.id = d.depends_on "
                    "WHERE d.task_id = $1 AND dep.status IN ($2, $3)",
                    (task_id, TaskStatus.FAILED, TaskStatus.CANCELLED),
                )
                has_stuck = stuck_count and stuck_count["cnt"] > 0
                new_status = TaskStatus.BLOCKED if has_stuck else TaskStatus.WAITING
            else:
                new_status = TaskStatus.PENDING

            await self._db.execute_write(
                "UPDATE tasks SET status = $1, retry_count = retry_count + 1, "
                "error = $2, output_text = NULL, started_at = NULL, updated_at = $3 WHERE id = $4",
                (new_status,
                 f"Zombie detected: running for >{STALENESS_TIMEOUT}s with no live coroutine",
                 now, task_id),
            )
            await self._progress.push_event(
                row["project_id"], "task_zombie_recovered",
                f"{row['title']}: zombie task recovered, retrying",
                task_id=task_id,
            )
            logger.warning("SWEEP: task %s zombie → %s (retry %d)",
                           task_id[:8], new_status, row["retry_count"] + 1)

    async def _adjust_concurrency(self):
        """Scale max concurrent tasks based on provider quota utilization.

        Linearly scales from max_concurrent (at 0% usage) down to 1 (at 100%).
        This prevents hitting rate limits by gradually throttling dispatch
        rather than slamming into an 80% cliff.
        """
        if self._quota_manager is None:
            return

        try:
            statuses = await self._quota_manager.get_quota_status()
        except Exception:
            return  # Don't block dispatch on quota query failure

        # Find the highest utilization across all providers with limits
        max_util = 0.0
        for ps in statuses.values():
            if not ps.has_limits:
                continue
            for ws in ps.windows:
                if ws.utilization_pct > max_util:
                    max_util = ws.utilization_pct

        # Scale: 0% usage → full concurrency, 100% → 1 task
        # Linear ramp-down starting at 50% utilization
        if max_util < 50.0:
            target = self._max_concurrent
        else:
            # 50% → max, 100% → 1
            scale = 1.0 - (max_util - 50.0) / 50.0
            target = max(1, int(self._max_concurrent * scale))

        current = self._semaphore._value + len(self._dispatched)
        if target != current:
            logger.info(
                "THROTTLE: provider utilization %.0f%% — concurrency %d → %d",
                max_util, current, target,
            )
            self._semaphore = asyncio.Semaphore(max(1, target - len(self._dispatched)))

    async def _tick(self):
        """One executor tick: find ready tasks and dispatch them."""
        # Adjust concurrency based on quota utilization
        await self._adjust_concurrency()

        # Sweep for zombie tasks before dispatching new ones
        await self._sweep_stale_tasks()

        # Find projects that are executing
        projects = await self._db.fetchall(
            "SELECT id FROM projects WHERE status = $1",
            (ProjectStatus.EXECUTING,),
        )
        logger.debug("TICK: found %d executing projects", len(projects))

        # Terminal statuses: tasks no longer active (done processing)
        _TERMINAL = (TaskStatus.COMPLETED, TaskStatus.FAILED,
                     TaskStatus.CANCELLED, TaskStatus.NEEDS_REVIEW)

        for project in projects:
            pid = project["id"]

            # Read project config to determine execution mode
            project_row = await self._db.fetchone(
                "SELECT config_json FROM projects WHERE id = $1", (pid,)
            )
            project_config = json.loads(project_row["config_json"] or "{}") if project_row else {}
            execution_mode = project_config.get("execution_mode", "auto")

            # Ensure feature branch exists (once per executor session).
            # Multiple projects can share the same repo on different branches.
            if pid not in self._branch_confirmed:
                await self._ensure_project_branch(pid)
                self._branch_confirmed.add(pid)

            # Check budget — skip for projects with only free/CLI tasks remaining.
            # All CLI tiers are subscription-billed ($0). When no API key is set,
            # haiku/sonnet/opus also route through CLI, making them effectively free.
            if not await self._budget.can_spend(0.001):
                _CLI_TIERS = (
                    ModelTier.OLLAMA.value, ModelTier.CLAUDE_CODE.value,
                    ModelTier.GEMINI_CLI.value, ModelTier.CODEX_CLI.value,
                )
                if self._client is None:
                    # No API key — all tiers route through CLI, all are free
                    non_free_count = 0
                else:
                    non_free = await self._db.fetchone(
                        "SELECT COUNT(*) as cnt FROM tasks "
                        "WHERE project_id = $1 AND model_tier NOT IN ($2, $3, $4, $5) "
                        "AND status NOT IN ($6, $7, $8, $9)",
                        (pid, *_CLI_TIERS, *_TERMINAL),
                    )
                    non_free_count = non_free["cnt"] if non_free else 0
                if non_free_count > 0:
                    await self._progress.push_event(pid, "budget_warning", "Budget limit reached. Execution paused.")
                    await self._db.execute_write(
                        "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
                        (ProjectStatus.PAUSED, time.time(), pid),
                    )
                    # PlanSentinel self-terminates on project_complete/failed/blocked
                    self._release_repo(pid)
                    continue

            # Unblock tasks whose dependencies are now met
            await self._update_blocked_tasks(pid)

            # Determine the current wave (lowest wave with incomplete tasks)
            wave_row = await self._db.fetchone(
                "SELECT MIN(wave) as w FROM tasks "
                "WHERE project_id = $1 AND status NOT IN ($2, $3, $4, $5)",
                (pid, *_TERMINAL),
            )
            current_wave = wave_row["w"] if wave_row and wave_row["w"] is not None else 0
            logger.debug("TICK: project %s wave=%s", pid[:8], current_wave)

            # When Odin dispatch is enabled AND the bus subscription is active,
            # skip self-dispatch — Odin publishes dispatch_command events via
            # the bus.  If the bus is unavailable (graceful degradation), fall
            # back to self-dispatch so the system keeps running without Odin.
            if ODIN_DISPATCH and self._bus_unsub is not None:
                logger.debug("TICK: Odin dispatch enabled, skipping self-dispatch for project %s", pid[:8])
            else:
                # Find ready tasks: pending with all deps resolved, filtered to current wave.
                # A dep is "resolved" when completed, or needs_review WITH non-empty output.
                # needs_review with empty output is a hollow completion — not resolved.
                ready = await self._db.fetchall(
                    "SELECT t.* FROM tasks t "
                    "LEFT JOIN task_deps d ON d.task_id = t.id "
                    "LEFT JOIN tasks dep ON dep.id = d.depends_on "
                    "  AND (dep.status NOT IN ($1, $2) "
                    "       OR (dep.status = $3 AND (dep.output_text IS NULL OR TRIM(dep.output_text) = ''))) "
                    "WHERE t.project_id = $4 AND t.status = $5 AND t.wave = $6 "
                    "GROUP BY t.id HAVING COUNT(dep.id) = 0 "
                    "ORDER BY t.priority ASC",
                    (TaskStatus.COMPLETED, TaskStatus.NEEDS_REVIEW,
                     TaskStatus.NEEDS_REVIEW,
                     pid, TaskStatus.PENDING, current_wave),
                )
                logger.info("TICK: found %d ready tasks in wave %s for project %s", len(ready), current_wave, pid[:8])

                for task_row in ready:
                    task_id = task_row["id"]
                    logger.info("TICK: evaluating task %s tier=%s title=%s", task_id[:8], task_row["model_tier"], task_row["title"])

                    # Execution mode gate: skip tasks the executor shouldn't dispatch
                    if execution_mode == ExecutionMode.EXTERNAL:
                        continue  # External executors handle all tasks
                    tier = ModelTier(task_row["model_tier"])
                    if execution_mode == ExecutionMode.HYBRID and tier != ModelTier.OLLAMA:
                        continue  # Hybrid: executor only handles Ollama tasks

                    # Skip tasks still in retry backoff
                    if task_id in self._retry_after and time.time() < self._retry_after[task_id]:
                        logger.info("TICK: task %s skipped (retry backoff)", task_id[:8])
                        continue

                    # Check resource availability for this task
                    if not self._resources_available(task_row):
                        logger.info("TICK: task %s skipped (resources unavailable for tier %s)", task_id[:8], task_row["model_tier"])
                        continue

                    tier = ModelTier(task_row["model_tier"])

                    # Check provider quota before reserving budget
                    if self._quota_manager is not None:
                        provider = _TIER_TO_PROVIDER.get(tier)
                        if provider and not await self._quota_manager.is_provider_available(provider):
                            logger.debug("TICK: task %s skipped (provider %s over quota)", task_id[:8], provider)
                            continue

                        # Check per-model-family quota (e.g., Opus vs Sonnet independent limits)
                        model_id = get_model_id(tier)
                        if not await self._quota_manager.is_model_available(model_id):
                            logger.info("TICK: task %s skipped (model %s over quota)", task_id[:8], model_id)
                            continue

                    # Check per-project budget using reserve_spend (prevents TOCTOU race).
                    # CLI tiers are subscription-billed ($0/call) — skip budget reservation.
                    # When no API key is set, haiku/sonnet/opus route through CLI too.
                    est_cost = 0.0
                    _FREE_EXECUTION = (
                        ModelTier.OLLAMA, ModelTier.CLAUDE_CODE,
                        ModelTier.GEMINI_CLI, ModelTier.CODEX_CLI,
                    )
                    api_tier_via_cli = (
                        tier not in _FREE_EXECUTION and self._client is None
                    )
                    if tier not in _FREE_EXECUTION and not api_tier_via_cli:
                        est_cost = calculate_cost(get_model_id(tier), _EST_TASK_INPUT_TOKENS, task_row["max_tokens"])
                        if not await self._budget.reserve_spend(est_cost):
                            continue
                        if not await self._budget.reserve_spend_project(pid, est_cost):
                            await self._budget.release_reservation(est_cost)
                            continue

                    # Atomic claim: pre-add to _dispatched to prevent duplicate dispatch,
                    # then verify via atomic DB update. Remove on contention.
                    if task_row["id"] in self._dispatched:
                        if est_cost > 0:
                            await self._budget.release_reservation(est_cost)
                            await self._budget.release_reservation_project(pid, est_cost)
                        continue
                    self._dispatched.add(task_row["id"])
                    claim_status = await self._db.execute_write(
                        "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3 AND status = $4",
                        (TaskStatus.QUEUED, time.time(), task_row["id"], TaskStatus.PENDING),
                    )
                    if parse_rowcount(claim_status) == 0:
                        self._dispatched.discard(task_row["id"])
                        if est_cost > 0:
                            await self._budget.release_reservation(est_cost)
                            await self._budget.release_reservation_project(pid, est_cost)
                        continue  # Another tick already claimed it
                    handle = asyncio.create_task(
                        execute_task(
                            task_row=task_row,
                            est_cost=est_cost,
                            db=self._db,
                            budget=self._budget,
                            progress=self._progress,
                            tool_registry=self._tool_registry,
                            http_client=self._http,
                            client=self._client,
                            semaphore=self._semaphore,
                            dispatched=self._dispatched,
                            retry_after=self._retry_after,
                            rag_cache=self._rag_cache,
                            diagnostic_ingester=self._diagnostic_ingester,
                        )
                    )
                    self._in_flight.add(handle)
                    handle.add_done_callback(self._in_flight.discard)

            # Check for wave completion → PR creation + optional checkpoint pause
            from backend.config import REVIEW_CYCLE_ENABLED, REVIEW_PR_ON_WAVE
            if REVIEW_CYCLE_ENABLED and REVIEW_PR_ON_WAVE:
                wave_done = await self._db.fetchone(
                    "SELECT COUNT(*) as cnt FROM tasks "
                    "WHERE project_id = $1 AND wave = $2 AND status NOT IN ($3, $4, $5, $6)",
                    (pid, current_wave, *_TERMINAL),
                )
                if wave_done and wave_done["cnt"] == 0:
                    await self._create_wave_pr(pid, project["name"], current_wave)

            if WAVE_CHECKPOINTS:
                wave_remaining = await self._db.fetchone(
                    "SELECT COUNT(*) as cnt FROM tasks "
                    "WHERE project_id = $1 AND wave = $2 AND status NOT IN ($3, $4, $5, $6)",
                    (pid, current_wave, *_TERMINAL),
                )
                if wave_remaining and wave_remaining["cnt"] == 0:
                    next_wave = await self._db.fetchone(
                        "SELECT MIN(wave) as w FROM tasks "
                        "WHERE project_id = $1 AND status NOT IN ($2, $3, $4, $5)",
                        (pid, *_TERMINAL),
                    )
                    if next_wave and next_wave["w"] is not None:
                        await self._db.execute_write(
                            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
                            (ProjectStatus.PAUSED, time.time(), pid),
                        )
                        await self._progress.push_event(
                            pid, "wave_checkpoint",
                            f"Wave {current_wave} complete. Resume to start wave {next_wave['w']}.",
                            wave=current_wave, next_wave=next_wave["w"],
                        )
                        # PlanSentinel self-terminates on project_complete/failed/blocked
                        self._release_repo(pid)
                        continue

            # Check if all tasks are done
            remaining = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM tasks WHERE project_id = $1 AND status NOT IN ($2, $3, $4, $5)",
                (pid, *_TERMINAL),
            )
            if remaining and remaining["cnt"] == 0:
                # All tasks reached a terminal state — but NEEDS_REVIEW tasks
                # with empty output are hollow completions that should block
                # project completion.  Treat them as incomplete.
                hollow = await self._db.fetchone(
                    "SELECT COUNT(*) as cnt FROM tasks "
                    "WHERE project_id = $1 AND status = $2 "
                    "AND (output_text IS NULL OR TRIM(output_text) = '')",
                    (pid, TaskStatus.NEEDS_REVIEW),
                )
                hollow_cnt = hollow["cnt"] if hollow else 0
                if hollow_cnt > 0:
                    logger.warning(
                        "Project %s has %d needs_review task(s) with empty output — not completing",
                        pid[:8], hollow_cnt,
                    )
                    await self._progress.push_event(
                        pid, "project_blocked",
                        f"{hollow_cnt} task(s) in needs_review with empty output — resolve before completion.",
                    )
                    # Pause the project so it doesn't spin in the tick loop
                    await self._db.execute_write(
                        "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
                        (ProjectStatus.PAUSED, time.time(), pid),
                    )
                    # PlanSentinel self-terminates on project_complete/failed/blocked
                    self._release_repo(pid)
                    continue

                failed_cnt = await self._db.fetchone(
                    "SELECT COUNT(*) as cnt FROM tasks WHERE project_id = $1 AND status = $2",
                    (pid, TaskStatus.FAILED),
                )
                has_failures = failed_cnt and failed_cnt["cnt"] > 0
                new_status = ProjectStatus.COMPLETED if not has_failures else ProjectStatus.FAILED
                await self._db.execute_write(
                    "UPDATE projects SET status = $1, completed_at = $2, updated_at = $3 WHERE id = $4",
                    (new_status, time.time(), time.time(), pid),
                )
                event_type = "project_complete" if not has_failures else "project_failed"
                msg = "All tasks finished." if not has_failures else f"Project finished with {failed_cnt['cnt']} failed task(s)."
                await self._progress.push_event(pid, event_type, msg)

                # Auto-PR (and auto-merge if review cycle passed) on completion
                if not has_failures and project["git_project_branch"] and project["repo_path"]:
                    asyncio.ensure_future(self._auto_merge_to_main(pid, project))

                # PlanSentinel self-terminates on project_complete/failed/blocked
                self._release_repo(pid)
                continue

            # Detect dead projects: no tasks are pending/queued/running, but some are blocked.
            # Skip for external/hybrid — executor isn't responsible for forward progress.
            if execution_mode in (ExecutionMode.EXTERNAL, ExecutionMode.HYBRID):
                continue
            active = await self._db.fetchone(
                "SELECT COUNT(*) as cnt FROM tasks WHERE project_id = $1 AND status IN ($2, $3, $4, $5)",
                (pid, TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.WAITING),
            )
            if active and active["cnt"] == 0:
                blocked = await self._db.fetchone(
                    "SELECT COUNT(*) as cnt FROM tasks WHERE project_id = $1 AND status = $2",
                    (pid, TaskStatus.BLOCKED),
                )
                if blocked and blocked["cnt"] > 0:
                    await self._db.execute_write(
                        "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
                        (ProjectStatus.FAILED, time.time(), pid),
                    )
                    await self._progress.push_event(
                        pid, "project_failed",
                        f"No forward progress possible: {blocked['cnt']} task(s) blocked by failed dependencies.",
                    )
                    # PlanSentinel self-terminates on project_complete/failed/blocked
                    self._release_repo(pid)

    async def _auto_merge_to_main(self, project_id: str, project) -> None:
        """Push branch, create PR, auto-merge if review cycle passed.

        Fire-and-forget — never blocks project completion.
        If review_cycle was enabled (all tasks reviewed), auto-merges.
        Otherwise creates PR and leaves it for human review.
        """
        branch = project["git_project_branch"]
        base = project.get("git_base_branch") or "main"
        repo_path = project["repo_path"]
        name = project.get("name") or project_id

        try:
            # Push branch to remote
            await self._git.push_branch(repo_path, branch)

            # Build PR body
            tasks = await self._db.fetchall(
                "SELECT title, status FROM tasks WHERE project_id = $1 ORDER BY wave, priority",
                (project_id,),
            )
            task_lines = "\n".join(
                f"- {'[x]' if t['status'] == 'completed' else '[ ]'} {t['title']}"
                for t in tasks
            )
            body = (
                f"## Auto-generated by Hekate Orchestration\n\n"
                f"### Tasks\n{task_lines}\n\n"
                f"---\n"
                f"Generated by Athena, executed by Nobody, watched by Argos."
            )

            # Create PR
            pr = await self._git.create_pr(repo_path, branch, base, f"orch: {name}", body)
            if not pr:
                logger.warning("Auto-merge: could not create PR for %s", name)
                return

            pr_url = pr.get("url", "")
            logger.info("Auto-merge: PR created for %s: %s", name, pr_url)
            await self._progress.push_event(
                project_id, "pr_created",
                f"PR created: {pr_url}",
                pr_url=pr_url,
            )

            # Auto-merge only if review cycle was enabled (all tasks passed review)
            import subprocess
            config = json.loads(project.get("config_json") or "{}")
            review_enabled = config.get("review_cycle", {}).get("enabled", False)

            if review_enabled:
                result = await asyncio.to_thread(
                    lambda: subprocess.run(
                        ["gh", "pr", "merge", pr_url, "--merge", "--delete-branch"],
                        cwd=str(repo_path),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                )
                if result.returncode == 0:
                    logger.info("Auto-merge: merged %s to %s", branch, base)
                    await self._progress.push_event(
                        project_id, "branch_merged",
                        f"Branch {branch} merged to {base}",
                    )

                    # Clean up: checkout base branch and delete local feature branch
                    try:
                        await self._git.checkout(repo_path, base)
                        await self._git.delete_branch(repo_path, branch)
                        logger.info("Auto-merge: cleaned up local branch %s, now on %s", branch, base)
                    except Exception as cleanup_exc:
                        logger.warning("Auto-merge: branch cleanup failed for %s: %s", branch, cleanup_exc)
                else:
                    logger.warning("Auto-merge: gh merge failed: %s", result.stderr[:200])
            else:
                logger.info("Auto-merge: PR open for review — review_cycle not enabled for %s", name)

        except Exception as exc:
            logger.warning("Auto-merge failed for %s: %s", name, exc)

    async def _ensure_project_branch(self, project_id: str) -> bool:
        """Ensure the project has an isolated worktree for its feature branch.

        Creates a git worktree at .worktrees/{branch-slug}/ so each project
        gets its own copy of the repo. No branch switching on the main repo.
        The worktree path is stored in self._worktrees for resolve_cwd().

        Returns True if the worktree is ready (or no repo_path).
        """
        # Already set up
        if project_id in self._worktrees:
            return True

        row = await self._db.fetchone(
            "SELECT name, repo_path, git_base_branch FROM projects WHERE id = $1",
            (project_id,),
        )
        if not row or not row["repo_path"]:
            return True

        repo_path = row["repo_path"]
        branch_name = f"{GIT_BRANCH_PREFIX}/{slugify(row['name'])}"
        base_branch = row["git_base_branch"] or "main"
        worktree_dir = str(Path(repo_path) / ".worktrees" / slugify(row["name"]))

        # Ensure workspace directory exists with CLI config
        from backend.services.task_lifecycle import _ensure_workspace
        await _ensure_workspace(repo_path, self._db, project_id)

        # Pull latest base branch in the main repo
        try:
            await self._git.pull_branch(repo_path, base_branch)
        except Exception as e:
            logger.warning("Failed to pull %s for project %s: %s",
                           base_branch, project_id[:8], e)

        # Check if worktree already exists on disk (e.g., from server restart)
        if Path(worktree_dir).exists():
            self._worktrees[project_id] = worktree_dir
            logger.info("Reusing existing worktree for %s: %s", project_id[:8], worktree_dir)
            return True

        # Check if branch already exists (from previous run)
        try:
            branch_exists = await self._git.branch_exists(repo_path, branch_name)
        except Exception:
            branch_exists = False

        try:
            if branch_exists:
                # Branch exists — create worktree on existing branch
                Path(worktree_dir).parent.mkdir(parents=True, exist_ok=True)
                import asyncio as _aio
                await _aio.to_thread(
                    self._git._run_git_sync,
                    "worktree", "add", worktree_dir, branch_name,
                    cwd=repo_path,
                )
            else:
                # New branch — create_worktree creates branch + worktree
                await self._git.create_worktree(repo_path, worktree_dir, branch_name)

            self._worktrees[project_id] = worktree_dir
            logger.info(
                "Worktree created for project %s: %s (branch: %s)",
                project_id[:8], worktree_dir, branch_name,
            )

            # Update project row with branch name
            await self._db.execute_write(
                "UPDATE projects SET git_project_branch = $1 WHERE id = $2 AND git_project_branch IS NULL",
                (branch_name, project_id),
            )
            return True

        except Exception as e:
            logger.warning("Failed to create worktree for project %s: %s", project_id[:8], e)
            # Fallback: use repo_path directly (old behavior)
            self._worktrees[project_id] = repo_path
            try:
                await self._git.ensure_feature_branch(repo_path, branch_name, base_branch)
            except Exception:
                pass
            return True

    def _release_repo(self, project_id: str) -> None:
        """Clean up worktree and caches when a project leaves executing state."""
        self._branch_confirmed.discard(project_id)
        worktree_path = self._worktrees.pop(project_id, None)
        if worktree_path and worktree_path != self._worktrees.get("__repo_path__"):
            # Schedule async worktree removal (don't block the tick)
            asyncio.ensure_future(self._cleanup_worktree(project_id, worktree_path))

    async def _cleanup_worktree(self, project_id: str, worktree_path: str) -> None:
        """Remove a worktree after project completion."""
        try:
            # Find the main repo_path
            row = await self._db.fetchone(
                "SELECT repo_path FROM projects WHERE id = $1", (project_id,),
            )
            if row and row["repo_path"] and worktree_path != row["repo_path"]:
                await self._git.remove_worktree(row["repo_path"], worktree_path)
                logger.info("Cleaned up worktree for project %s: %s", project_id[:8], worktree_path)
        except Exception as e:
            logger.warning("Failed to clean up worktree for %s: %s", project_id[:8], e)

    async def _teardown_plan_sentinel(self, project_id: str) -> None:
        """No-op — legacy Plan Sentinel removed, Odin handles monitoring."""
        pass

    async def _update_blocked_tasks(self, project_id: str):
        """Unblock tasks whose dependencies are all resolved, and reclassify
        waiting vs blocked for tasks that still have unmet deps.

        A dependency is "resolved" when it's completed OR needs_review WITH
        non-empty output.  NEEDS_REVIEW with empty output is a hollow
        completion — dependents must stay waiting/blocked because there's no
        usable output to forward.
        """
        now = time.time()

        # Transition WAITING/BLOCKED → PENDING when all deps are resolved
        for status in (TaskStatus.BLOCKED, TaskStatus.WAITING):
            await self._db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 "
                "WHERE project_id = $3 AND status = $4 "
                "AND id NOT IN ("
                "  SELECT d.task_id FROM task_deps d "
                "  JOIN tasks dep ON dep.id = d.depends_on "
                "  WHERE dep.status NOT IN ($5, $6) "
                "     OR (dep.status = $7 AND (dep.output_text IS NULL OR TRIM(dep.output_text) = ''))"
                ")",
                (TaskStatus.PENDING, now, project_id, status,
                 TaskStatus.COMPLETED, TaskStatus.NEEDS_REVIEW,
                 TaskStatus.NEEDS_REVIEW),
            )

        # Reclassify remaining WAITING tasks to BLOCKED if a dep is now failed/cancelled
        await self._db.execute_write(
            "UPDATE tasks SET status = $1, updated_at = $2 "
            "WHERE project_id = $3 AND status = $4 "
            "AND id IN ("
            "  SELECT d.task_id FROM task_deps d "
            "  JOIN tasks dep ON dep.id = d.depends_on "
            "  WHERE dep.status IN ($5, $6)"
            ")",
            (TaskStatus.BLOCKED, now, project_id, TaskStatus.WAITING,
             TaskStatus.FAILED, TaskStatus.CANCELLED),
        )

        # Reclassify BLOCKED tasks back to WAITING if all stuck deps were retried/resolved
        await self._db.execute_write(
            "UPDATE tasks SET status = $1, updated_at = $2 "
            "WHERE project_id = $3 AND status = $4 "
            "AND id NOT IN ("
            "  SELECT d.task_id FROM task_deps d "
            "  JOIN tasks dep ON dep.id = d.depends_on "
            "  WHERE dep.status IN ($5, $6)"
            ")",
            (TaskStatus.WAITING, now, project_id, TaskStatus.BLOCKED,
             TaskStatus.FAILED, TaskStatus.CANCELLED),
        )

    async def _create_wave_pr(self, project_id: str, project_name: str, wave: int) -> None:
        """Create a PR for a completed wave's commits (best-effort)."""
        try:
            project_row = await self._db.fetchone(
                "SELECT repo_path FROM projects WHERE id = $1", (project_id,),
            )
            if not project_row or not project_row["repo_path"]:
                return

            cwd = project_row["repo_path"]
            from backend.services.git_service import GitService
            git = GitService(db=self._db)

            # Check if there are commits to PR (compare against main/master)
            repo_info = await git.validate_repo(cwd)
            if not repo_info["is_git"] or not repo_info["has_remote"]:
                return

            branch = repo_info["current_branch"]
            if branch in ("main", "master"):
                # Create a feature branch from the wave's commits
                wave_branch = f"{GIT_BRANCH_PREFIX}/{slugify(project_name)}-wave-{wave}"
                if await git.branch_exists(cwd, wave_branch):
                    return  # Already created

                # Get completed task titles for PR body
                tasks = await self._db.fetchall(
                    "SELECT title, model_used FROM tasks "
                    "WHERE project_id = $1 AND wave = $2 AND status = $3",
                    (project_id, wave, TaskStatus.COMPLETED),
                )
                task_lines = "\n".join(
                    f"- {t['title']} ({t['model_used'] or 'unknown'})"
                    for t in tasks
                )

                await git.create_branch(cwd, wave_branch)
                await git.checkout(cwd, wave_branch)
                await git.push_branch(cwd, wave_branch)

                pr_result = await git.create_pr(
                    cwd,
                    head=wave_branch,
                    base="main",
                    title=f"{project_name}: Wave {wave}",
                    body=(
                        f"## Summary\n"
                        f"Automated wave {wave} completion for project **{project_name}**.\n\n"
                        f"### Tasks completed\n{task_lines}\n\n"
                        f"## Test plan\n"
                        f"- [ ] Review generated code\n"
                        f"- [ ] Run tests\n\n"
                        f"Generated by Orchestration Engine · Claude Opus 4.6"
                    ),
                )
                if pr_result:
                    await self._progress.push_event(
                        project_id, "wave_pr_created",
                        f"PR created for wave {wave}: {pr_result['url']}",
                        wave=wave,
                    )
                    logger.info("Created PR for wave %d: %s", wave, pr_result.get("url"))

                # Switch back to main for next wave
                await git.checkout(cwd, "main")

        except Exception as e:
            logger.warning("Failed to create wave PR for project %s wave %d: %s", project_id, wave, e)

    def _check_resource(self, resource_name: str) -> bool:
        """Check if a resource is available, with circuit breaker caching.

        If a resource was recently found offline, skip re-checking for the
        configured skip period to avoid hammering health endpoints on every tick.
        If the resource state is unknown (not yet checked), assume available
        to avoid blocking dispatch on startup before the first health check.
        """
        now = time.time()
        if now < self._resource_skip_until.get(resource_name, 0):
            return False
        state = self._resource_monitor.get(resource_name)
        if state is None or state.status.value == "checking":
            # Resource not yet checked — assume available rather than blocking
            return True
        if not self._resource_monitor.is_available(resource_name):
            self._resource_skip_until[resource_name] = now + RESOURCE_SKIP_SECONDS
            return False
        self._resource_skip_until.pop(resource_name, None)
        return True

    def _resources_available(self, task_row) -> bool:
        """Check if the resources this task needs are available."""
        tier = ModelTier(task_row["model_tier"])
        tools = json.loads(task_row["tools_json"]) if task_row["tools_json"] else []

        # Ollama tasks need Ollama online
        if tier == ModelTier.OLLAMA:
            if not self._check_resource("ollama_local"):
                return False

        # Claude API tasks need API key OR any CLI available (subscriptions cover all tiers)
        if tier in (ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS):
            if not (self._check_resource("anthropic_api") or
                    self._check_resource("claude_code_cli") or
                    self._check_resource("gemini_cli") or
                    self._check_resource("codex_cli")):
                return False

        # Gemini CLI tasks need gemini CLI available
        if tier == ModelTier.GEMINI_CLI:
            if not self._check_resource("gemini_cli"):
                return False

        # Codex CLI tasks need codex CLI available
        if tier == ModelTier.CODEX_CLI:
            if not self._check_resource("codex_cli"):
                return False

        # ComfyUI tool needs ComfyUI online
        if "generate_image" in tools:
            if not (self._check_resource("comfyui_local") or
                    self._check_resource("comfyui_server")):
                return False

        # RAG tools need Ollama for embeddings
        if any(t in tools for t in ("search_knowledge", "lookup_type")):
            # lookup_type doesn't need Ollama (FTS only), but search_knowledge does
            if "search_knowledge" in tools and not self._check_resource("ollama_local"):
                return False

        return True
