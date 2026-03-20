#  Orchestration Engine - Budget Manager
#
#  Tracks API spending and enforces daily/monthly/per-project limits.
#  Uses in-memory reservations to prevent TOCTOU budget races.
#
#  Depends on: backend/db/connection.py, backend/config.py
#  Used by:    container.py, routes/usage.py, services/executor.py

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from types import TracebackType

from backend.config import BUDGET_DAILY, BUDGET_MONTHLY, BUDGET_PER_PROJECT, BUDGET_WARN_PCT
from backend.exceptions import BudgetExhaustedError
from backend.models.schemas import BudgetStatus, UsageSummary

logger = logging.getLogger(__name__)


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class BudgetManager:
    """Tracks spending and enforces configurable limits.

    Reservation tracking prevents TOCTOU races: concurrent tasks call
    reserve_spend() which holds an asyncio.Lock, so only one check+reserve
    runs at a time. Reservations are approximate — a task reserved before
    midnight but completing after creates a stale daily reservation, bounded
    by MAX_CONCURRENT_TASKS * max_single_task_cost. Self-corrects on next
    period rollover.
    """

    def __init__(self, db):
        self._db = db
        self._lock = asyncio.Lock()
        self._reserved_daily: float = 0.0
        self._reserved_monthly: float = 0.0
        self._reserved_per_project: dict[str, float] = {}
        self._last_daily_key: str = ""
        self._last_monthly_key: str = ""

    async def record_spend(
        self,
        cost_usd: float,
        prompt_tokens: int,
        completion_tokens: int,
        provider: str,
        model: str,
        purpose: str = "",
        project_id: str | None = None,
        task_id: str | None = None,
    ):
        """Record a single API call's cost and tokens.

        All three writes (usage_log, daily period, monthly period) run in a
        single transaction for atomicity.
        """
        now = time.time()
        day_key = _today_key()
        month_key = _month_key()

        await self._db.execute_many_write([
            # Insert into usage_log
            (
                "INSERT INTO usage_log (project_id, task_id, provider, model, "
                "prompt_tokens, completion_tokens, cost_usd, purpose, timestamp) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                (project_id, task_id, provider, model, prompt_tokens,
                 completion_tokens, cost_usd, purpose, now),
            ),
            # Update daily budget period
            (
                "INSERT INTO budget_periods (period_key, period_type, total_cost_usd, "
                "total_prompt_tokens, total_completion_tokens, api_call_count) "
                "VALUES ($1, 'daily', $2, $3, $4, 1) "
                "ON CONFLICT(period_key) DO UPDATE SET "
                "total_cost_usd = budget_periods.total_cost_usd + excluded.total_cost_usd, "
                "total_prompt_tokens = budget_periods.total_prompt_tokens + excluded.total_prompt_tokens, "
                "total_completion_tokens = budget_periods.total_completion_tokens + excluded.total_completion_tokens, "
                "api_call_count = budget_periods.api_call_count + 1",
                (day_key, cost_usd, prompt_tokens, completion_tokens),
            ),
            # Update monthly budget period
            (
                "INSERT INTO budget_periods (period_key, period_type, total_cost_usd, "
                "total_prompt_tokens, total_completion_tokens, api_call_count) "
                "VALUES ($1, 'monthly', $2, $3, $4, 1) "
                "ON CONFLICT(period_key) DO UPDATE SET "
                "total_cost_usd = budget_periods.total_cost_usd + excluded.total_cost_usd, "
                "total_prompt_tokens = budget_periods.total_prompt_tokens + excluded.total_prompt_tokens, "
                "total_completion_tokens = budget_periods.total_completion_tokens + excluded.total_completion_tokens, "
                "api_call_count = budget_periods.api_call_count + 1",
                (month_key, cost_usd, prompt_tokens, completion_tokens),
            ),
        ])

    async def get_budget_status(self) -> BudgetStatus:
        """Get current spending vs. limits."""
        day_row = await self._db.fetchone(
            "SELECT budget_periods.total_cost_usd FROM budget_periods WHERE period_key = $1",
            (_today_key(),),
        )
        month_row = await self._db.fetchone(
            "SELECT budget_periods.total_cost_usd FROM budget_periods WHERE period_key = $1",
            (_month_key(),),
        )

        daily_spent = day_row["total_cost_usd"] if day_row else 0.0
        monthly_spent = month_row["total_cost_usd"] if month_row else 0.0

        return BudgetStatus(
            daily_spent_usd=round(daily_spent, 4),
            daily_limit_usd=BUDGET_DAILY,
            daily_pct=round((daily_spent / BUDGET_DAILY * 100) if BUDGET_DAILY > 0 else 0, 1),
            monthly_spent_usd=round(monthly_spent, 4),
            monthly_limit_usd=BUDGET_MONTHLY,
            monthly_pct=round((monthly_spent / BUDGET_MONTHLY * 100) if BUDGET_MONTHLY > 0 else 0, 1),
        )

    async def can_spend(self, estimated_cost_usd: float) -> bool:
        """Check if spending this amount would exceed any limit.

        Note: this does NOT reserve the amount. For concurrent task dispatch,
        use reserve_spend() instead to prevent TOCTOU races.
        """
        if estimated_cost_usd <= 0:
            return True  # Free (Ollama)

        status = await self.get_budget_status()

        if status.daily_spent_usd + estimated_cost_usd > BUDGET_DAILY:
            return False
        if status.monthly_spent_usd + estimated_cost_usd > BUDGET_MONTHLY:
            return False

        return True

    async def reserve_spend(self, estimated_cost: float) -> bool:
        """Atomically check budget and reserve estimated_cost.

        Returns True if the reservation succeeded (budget allows it),
        False if it would exceed limits. The reservation is held until
        release_reservation() is called (after record_spend or on failure).
        """
        if estimated_cost <= 0:
            return True

        async with self._lock:
            # Reset reservations on period rollover
            daily_key = _today_key()
            monthly_key = _month_key()
            if daily_key != self._last_daily_key:
                self._reserved_daily = 0.0
                self._reserved_per_project.clear()
                self._last_daily_key = daily_key
            if monthly_key != self._last_monthly_key:
                self._reserved_monthly = 0.0
                self._last_monthly_key = monthly_key

            status = await self.get_budget_status()
            daily_ok = (
                status.daily_spent_usd + self._reserved_daily + estimated_cost
                <= BUDGET_DAILY
            )
            monthly_ok = (
                status.monthly_spent_usd + self._reserved_monthly + estimated_cost
                <= BUDGET_MONTHLY
            )
            if not (daily_ok and monthly_ok):
                return False

            self._reserved_daily += estimated_cost
            self._reserved_monthly += estimated_cost
            return True

    async def release_reservation(self, estimated_cost: float) -> None:
        """Release a previously held reservation (after record_spend or failure)."""
        if estimated_cost <= 0:
            return
        async with self._lock:
            self._reserved_daily = max(0.0, self._reserved_daily - estimated_cost)
            self._reserved_monthly = max(0.0, self._reserved_monthly - estimated_cost)

    async def can_spend_project(self, project_id: str, estimated_cost_usd: float) -> bool:
        """Check if a project has budget remaining.

        Note: this does NOT reserve the amount. For concurrent task dispatch,
        use reserve_spend_project() instead to prevent TOCTOU races.
        """
        if estimated_cost_usd <= 0:
            return True

        row = await self._db.fetchone(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM usage_log WHERE project_id = $1",
            (project_id,),
        )
        project_spent = row["total"] if row else 0.0

        return project_spent + estimated_cost_usd <= BUDGET_PER_PROJECT

    async def reserve_spend_project(self, project_id: str, estimated_cost: float) -> bool:
        """Atomically check per-project budget and reserve estimated_cost.

        Returns True if the reservation succeeded (project budget allows it),
        False if it would exceed the per-project limit. Must be called under
        the same _lock context as reserve_spend() — caller should hold the
        global reservation first.
        """
        if estimated_cost <= 0:
            return True

        async with self._lock:
            row = await self._db.fetchone(
                "SELECT COALESCE(SUM(cost_usd), 0) as total FROM usage_log WHERE project_id = $1",
                (project_id,),
            )
            project_spent = row["total"] if row else 0.0
            reserved = self._reserved_per_project.get(project_id, 0.0)

            if project_spent + reserved + estimated_cost > BUDGET_PER_PROJECT:
                return False

            self._reserved_per_project[project_id] = reserved + estimated_cost
            return True

    async def release_reservation_project(self, project_id: str, estimated_cost: float) -> None:
        """Release a previously held per-project reservation."""
        if estimated_cost <= 0:
            return
        async with self._lock:
            current = self._reserved_per_project.get(project_id, 0.0)
            self._reserved_per_project[project_id] = max(0.0, current - estimated_cost)

    async def is_warning(self) -> bool:
        """Check if we're at or above the warning threshold."""
        status = await self.get_budget_status()
        return status.daily_pct >= BUDGET_WARN_PCT or status.monthly_pct >= BUDGET_WARN_PCT

    async def get_usage_summary(self, project_id: str | None = None) -> UsageSummary:
        """Get aggregate usage statistics."""
        where = "WHERE 1=1"
        params: list = []
        if project_id:
            where += " AND project_id = $1"
            params.append(project_id)

        # Totals
        row = await self._db.fetchone(
            f"SELECT COALESCE(SUM(cost_usd), 0) as cost, "
            f"COALESCE(SUM(prompt_tokens), 0) as pt, "
            f"COALESCE(SUM(completion_tokens), 0) as ct, "
            f"COUNT(*) as calls FROM usage_log {where}",
            params,
        )

        # By model
        model_rows = await self._db.fetchall(
            f"SELECT model, SUM(cost_usd) as cost, SUM(prompt_tokens) as pt, "
            f"SUM(completion_tokens) as ct, COUNT(*) as calls "
            f"FROM usage_log {where} GROUP BY model",
            params,
        )
        by_model = {
            r["model"]: {
                "cost_usd": round(r["cost"], 4),
                "prompt_tokens": r["pt"],
                "completion_tokens": r["ct"],
                "calls": r["calls"],
            }
            for r in model_rows
        }

        # By provider
        provider_rows = await self._db.fetchall(
            f"SELECT provider, SUM(cost_usd) as cost, COUNT(*) as calls "
            f"FROM usage_log {where} GROUP BY provider",
            params,
        )
        by_provider = {
            r["provider"]: {"cost_usd": round(r["cost"], 4), "calls": r["calls"]}
            for r in provider_rows
        }

        return UsageSummary(
            total_cost_usd=round(row["cost"], 4),
            total_prompt_tokens=row["pt"],
            total_completion_tokens=row["ct"],
            api_call_count=row["calls"],
            by_model=by_model,
            by_provider=by_provider,
        )

    def guard(
        self,
        estimated_cost: float,
        provider: str,
        model: str,
        project_id: str | None = None,
        task_id: str | None = None,
        purpose: str = "",
    ) -> BudgetGuard:
        """Create an async context manager that reserves budget on entry
        and records or rolls back on exit.

        Usage::

            async with budget.guard(0.05, "anthropic", "haiku", project_id="p1") as guard:
                result = await do_llm_call()
                guard.actual_cost = result.cost
                guard.prompt_tokens = result.prompt_tokens
                guard.completion_tokens = result.completion_tokens
        """
        return BudgetGuard(
            manager=self,
            estimated_cost=estimated_cost,
            provider=provider,
            model=model,
            project_id=project_id,
            task_id=task_id,
            purpose=purpose,
        )


class AsyncBudgetManager:
    """Async context manager for transactional budget control.

    Wraps the full lifecycle: check budget → reserve → execute (caller's
    code inside the ``async with``) → record spend or rollback.

    Unlike ``BudgetGuard`` which is created via ``BudgetManager.guard()``,
    this class can be instantiated directly with a ``BudgetManager`` and
    used as a standalone transactional wrapper.

    Usage::

        async with AsyncBudgetManager(
            budget_manager,
            estimated_cost=0.05,
            provider="anthropic",
            model="haiku",
            project_id="p1",
        ) as txn:
            result = await do_llm_call()
            txn.actual_cost = result.cost
            txn.prompt_tokens = result.prompt_tokens
            txn.completion_tokens = result.completion_tokens
    """

    def __init__(
        self,
        manager: BudgetManager,
        estimated_cost: float,
        provider: str,
        model: str,
        project_id: str | None = None,
        task_id: str | None = None,
        purpose: str = "",
    ):
        self._manager = manager
        self._estimated_cost = estimated_cost
        self._provider = provider
        self._model = model
        self._project_id = project_id
        self._task_id = task_id
        self._purpose = purpose

        # Callers set these before exiting to record actuals.
        self.actual_cost: float = 0.0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0

        self._global_reserved = False
        self._project_reserved = False
        self._committed = False

    async def __aenter__(self) -> AsyncBudgetManager:
        """Reserve budget (global + per-project) and mark as in-use.

        Raises ``BudgetExhaustedError`` if either global or per-project
        limits would be exceeded by the estimated cost.
        """
        if self._estimated_cost <= 0:
            return self

        # Global reservation
        if not await self._manager.reserve_spend(self._estimated_cost):
            raise BudgetExhaustedError(
                f"Global budget exceeded for estimated ${self._estimated_cost:.4f}"
            )
        self._global_reserved = True

        # Per-project reservation
        if self._project_id:
            if not await self._manager.reserve_spend_project(
                self._project_id, self._estimated_cost
            ):
                # Roll back global reservation before raising
                await self._manager.release_reservation(self._estimated_cost)
                self._global_reserved = False
                raise BudgetExhaustedError(
                    f"Project {self._project_id} budget exceeded "
                    f"for estimated ${self._estimated_cost:.4f}"
                )
            self._project_reserved = True

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        """Finalize the budget transaction.

        On success (no exception): records the actual spend via
        ``record_spend`` if ``actual_cost > 0``.

        On failure (exception raised): logs a warning and skips
        recording — the reservation is rolled back either way.

        Reservations are always released in the ``finally`` block
        regardless of outcome. Exceptions are never suppressed.
        """
        try:
            if exc_type is None and self.actual_cost > 0:
                await self._manager.record_spend(
                    cost_usd=self.actual_cost,
                    prompt_tokens=self.prompt_tokens,
                    completion_tokens=self.completion_tokens,
                    provider=self._provider,
                    model=self._model,
                    purpose=self._purpose,
                    project_id=self._project_id,
                    task_id=self._task_id,
                )
                self._committed = True
            elif exc_type is not None:
                logger.warning(
                    "AsyncBudgetManager: rolling back reservation of $%.4f "
                    "due to %s: %s",
                    self._estimated_cost,
                    exc_type.__name__,
                    exc_val,
                )
        finally:
            # Always release reservations
            if self._global_reserved:
                await self._manager.release_reservation(self._estimated_cost)
            if self._project_reserved and self._project_id:
                await self._manager.release_reservation_project(
                    self._project_id, self._estimated_cost
                )

        return False  # Never suppress exceptions

    @property
    def committed(self) -> bool:
        """Whether actual spend was successfully recorded."""
        return self._committed


class BudgetGuard:
    """Async context manager that reserves budget on entry and records
    actual spend on successful exit, or rolls back reservations on error.

    Set ``actual_cost``, ``prompt_tokens``, and ``completion_tokens``
    inside the ``async with`` block before exiting to record the real
    spend.  If an exception occurs, reservations are released and the
    exception propagates.
    """

    def __init__(
        self,
        manager: BudgetManager,
        estimated_cost: float,
        provider: str,
        model: str,
        project_id: str | None = None,
        task_id: str | None = None,
        purpose: str = "",
    ):
        self._manager = manager
        self._estimated_cost = estimated_cost
        self._provider = provider
        self._model = model
        self._project_id = project_id
        self._task_id = task_id
        self._purpose = purpose

        # Callers set these before exiting the context to record actuals.
        self.actual_cost: float = 0.0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0

        self._global_reserved = False
        self._project_reserved = False

    async def __aenter__(self) -> BudgetGuard:
        if self._estimated_cost <= 0:
            return self

        if not await self._manager.reserve_spend(self._estimated_cost):
            raise BudgetExhaustedError(
                f"Global budget exceeded for estimated ${self._estimated_cost:.4f}"
            )
        self._global_reserved = True

        if self._project_id:
            if not await self._manager.reserve_spend_project(
                self._project_id, self._estimated_cost
            ):
                await self._manager.release_reservation(self._estimated_cost)
                self._global_reserved = False
                raise BudgetExhaustedError(
                    f"Project {self._project_id} budget exceeded "
                    f"for estimated ${self._estimated_cost:.4f}"
                )
            self._project_reserved = True

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        try:
            if exc_type is None and self.actual_cost > 0:
                await self._manager.record_spend(
                    cost_usd=self.actual_cost,
                    prompt_tokens=self.prompt_tokens,
                    completion_tokens=self.completion_tokens,
                    provider=self._provider,
                    model=self._model,
                    purpose=self._purpose,
                    project_id=self._project_id,
                    task_id=self._task_id,
                )
            elif exc_type is not None:
                logger.warning(
                    "BudgetGuard: rolling back reservation of $%.4f due to %s",
                    self._estimated_cost,
                    exc_type.__name__,
                )
        finally:
            if self._global_reserved:
                await self._manager.release_reservation(self._estimated_cost)
            if self._project_reserved and self._project_id:
                await self._manager.release_reservation_project(
                    self._project_id, self._estimated_cost
                )

        return False  # Don't suppress exceptions
