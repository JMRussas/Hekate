#  Orchestration Engine - Budget Manager Tests
#
#  Tests for spending tracking and limit enforcement.
#
#  Depends on: backend/services/budget.py, backend/db/connection.py
#  Used by:    pytest

import time

import pytest
from unittest.mock import patch

from backend.services.budget import BudgetManager


async def _create_project(db, project_id="proj1"):
    """Helper: insert a project row so FK constraints pass."""
    now = time.time()
    await db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, 'Test', 'test', 'draft', ?, ?)",
        (project_id, now, now),
    )


@pytest.fixture
async def budget_mgr(tmp_db):
    """BudgetManager wired to the test database via constructor injection."""
    yield BudgetManager(db=tmp_db)


class TestRecordSpend:
    async def test_creates_usage_log_entry(self, budget_mgr, tmp_db):
        await _create_project(tmp_db, "proj1")
        await budget_mgr.record_spend(
            cost_usd=0.05, prompt_tokens=100, completion_tokens=200,
            provider="anthropic", model="claude-sonnet-4-6",
            purpose="testing", project_id="proj1",
        )
        row = await tmp_db.fetchone("SELECT * FROM usage_log")
        assert row is not None
        assert row["cost_usd"] == 0.05
        assert row["provider"] == "anthropic"
        assert row["project_id"] == "proj1"

    async def test_updates_daily_period(self, budget_mgr, tmp_db):
        await budget_mgr.record_spend(
            cost_usd=0.10, prompt_tokens=100, completion_tokens=200,
            provider="anthropic", model="test-model",
        )
        row = await tmp_db.fetchone(
            "SELECT * FROM budget_periods WHERE period_type = 'daily'"
        )
        assert row is not None
        assert row["total_cost_usd"] == 0.10
        assert row["api_call_count"] == 1

    async def test_accumulates_across_calls(self, budget_mgr, tmp_db):
        for _ in range(3):
            await budget_mgr.record_spend(
                cost_usd=0.10, prompt_tokens=100, completion_tokens=200,
                provider="anthropic", model="test-model",
            )
        row = await tmp_db.fetchone(
            "SELECT * FROM budget_periods WHERE period_type = 'daily'"
        )
        assert abs(row["total_cost_usd"] - 0.30) < 1e-9
        assert row["api_call_count"] == 3


class TestCanSpend:
    async def test_within_limit_returns_true(self, budget_mgr):
        with patch("backend.services.budget.BUDGET_DAILY", 5.0), \
             patch("backend.services.budget.BUDGET_MONTHLY", 50.0):
            assert await budget_mgr.can_spend(1.0) is True

    async def test_over_daily_limit_returns_false(self, budget_mgr, tmp_db):
        with patch("backend.services.budget.BUDGET_DAILY", 0.20), \
             patch("backend.services.budget.BUDGET_MONTHLY", 50.0):
            await budget_mgr.record_spend(
                cost_usd=0.15, prompt_tokens=100, completion_tokens=200,
                provider="anthropic", model="test-model",
            )
            assert await budget_mgr.can_spend(0.10) is False

    async def test_zero_cost_always_true(self, budget_mgr):
        assert await budget_mgr.can_spend(0.0) is True
        assert await budget_mgr.can_spend(-1.0) is True


class TestCanSpendProject:
    async def test_within_project_limit(self, budget_mgr):
        with patch("backend.services.budget.BUDGET_PER_PROJECT", 10.0):
            assert await budget_mgr.can_spend_project("proj1", 1.0) is True

    async def test_over_project_limit(self, budget_mgr, tmp_db):
        await _create_project(tmp_db, "proj1")
        with patch("backend.services.budget.BUDGET_PER_PROJECT", 0.50):
            await budget_mgr.record_spend(
                cost_usd=0.40, prompt_tokens=100, completion_tokens=200,
                provider="anthropic", model="test-model", project_id="proj1",
            )
            assert await budget_mgr.can_spend_project("proj1", 0.20) is False


class TestGetBudgetStatus:
    async def test_reflects_spending(self, budget_mgr):
        with patch("backend.services.budget.BUDGET_DAILY", 5.0), \
             patch("backend.services.budget.BUDGET_MONTHLY", 50.0):
            await budget_mgr.record_spend(
                cost_usd=1.0, prompt_tokens=100, completion_tokens=200,
                provider="anthropic", model="test-model",
            )
            status = await budget_mgr.get_budget_status()
            assert status.daily_spent_usd == 1.0
            assert status.daily_pct == 20.0  # 1.0 / 5.0 * 100


class TestReserveSpendProject:
    async def test_reserves_within_limit(self, budget_mgr):
        with patch("backend.services.budget.BUDGET_PER_PROJECT", 10.0):
            assert await budget_mgr.reserve_spend_project("proj1", 5.0) is True
            # Second reservation should account for the first
            assert await budget_mgr.reserve_spend_project("proj1", 4.0) is True
            # Third would exceed
            assert await budget_mgr.reserve_spend_project("proj1", 2.0) is False

    async def test_reserves_per_project_independently(self, budget_mgr):
        with patch("backend.services.budget.BUDGET_PER_PROJECT", 10.0):
            assert await budget_mgr.reserve_spend_project("proj1", 8.0) is True
            # Different project should have its own budget
            assert await budget_mgr.reserve_spend_project("proj2", 8.0) is True

    async def test_release_frees_reservation(self, budget_mgr):
        with patch("backend.services.budget.BUDGET_PER_PROJECT", 10.0):
            await budget_mgr.reserve_spend_project("proj1", 8.0)
            await budget_mgr.release_reservation_project("proj1", 8.0)
            # After release, should be able to reserve again
            assert await budget_mgr.reserve_spend_project("proj1", 8.0) is True

    async def test_zero_cost_always_succeeds(self, budget_mgr):
        assert await budget_mgr.reserve_spend_project("proj1", 0.0) is True
        assert await budget_mgr.reserve_spend_project("proj1", -1.0) is True

    async def test_accounts_for_recorded_spend(self, budget_mgr, tmp_db):
        await _create_project(tmp_db, "proj1")
        with patch("backend.services.budget.BUDGET_PER_PROJECT", 1.0):
            await budget_mgr.record_spend(
                cost_usd=0.80, prompt_tokens=100, completion_tokens=200,
                provider="anthropic", model="test-model", project_id="proj1",
            )
            # Already spent 0.80, reserving 0.30 should exceed 1.0 limit
            assert await budget_mgr.reserve_spend_project("proj1", 0.30) is False
            # But 0.15 should fit
            assert await budget_mgr.reserve_spend_project("proj1", 0.15) is True


class TestPeriodRolloverClearsProjectReservations:
    async def test_daily_rollover_clears_project_reservations(self, budget_mgr):
        """Per-project reservations should be cleared when the day rolls over."""
        with patch("backend.services.budget.BUDGET_DAILY", 100.0), \
             patch("backend.services.budget.BUDGET_MONTHLY", 1000.0), \
             patch("backend.services.budget.BUDGET_PER_PROJECT", 50.0):
            # Reserve for proj1
            await budget_mgr.reserve_spend_project("proj1", 40.0)

            # Simulate a global reservation to trigger the rollover code path
            await budget_mgr.reserve_spend(1.0)

            # Force a day rollover by changing the last daily key
            budget_mgr._last_daily_key = "2020-01-01"

            # Next reservation triggers rollover, clearing per-project
            await budget_mgr.reserve_spend(1.0)

            # Per-project dict should be cleared
            assert budget_mgr._reserved_per_project == {}


class TestGetUsageSummary:
    async def test_summary_totals(self, budget_mgr):
        await budget_mgr.record_spend(
            cost_usd=0.50, prompt_tokens=1000, completion_tokens=500,
            provider="anthropic", model="claude-sonnet-4-6", purpose="test",
        )
        await budget_mgr.record_spend(
            cost_usd=0.0, prompt_tokens=200, completion_tokens=100,
            provider="ollama", model="qwen2.5-coder:14b", purpose="test",
        )
        summary = await budget_mgr.get_usage_summary()
        assert summary.total_cost_usd == 0.50
        assert summary.api_call_count == 2
        assert "anthropic" in summary.by_provider
        assert "ollama" in summary.by_provider
        assert "claude-sonnet-4-6" in summary.by_model
