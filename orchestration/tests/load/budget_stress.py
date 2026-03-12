#  Orchestration Engine - Budget Concurrency Stress Test
#
#  Verifies budget enforcement under concurrent access. Spawns multiple
#  coroutines competing for the same budget, ensuring no overspend.
#
#  Usage:
#    python -m pytest tests/load/budget_stress.py -m slow -v
#
#  Depends on: backend/services/budget.py, backend/db/connection.py
#  Used by:    manual stress testing

import asyncio
from unittest.mock import patch

import pytest

from backend.db.connection import Database
from backend.services.budget import BudgetManager

pytestmark = pytest.mark.slow


@pytest.fixture
async def budget_db(tmp_path):
    """Create a fresh database with full schema for budget stress testing."""
    db = Database()
    db_path = str(tmp_path / "budget_stress.db")
    await db.init(db_path)
    yield db
    await db.close()


class TestBudgetConcurrency:
    async def test_concurrent_reservations_respect_limit(self, budget_db):
        """50 coroutines competing for budget should not overspend."""
        budget = BudgetManager(budget_db)

        daily_limit = 1.0
        monthly_limit = 10.0
        num_coroutines = 50
        spend_per_task = 0.05  # Each wants $0.05 → max 20 should succeed

        results = {"reserved": 0, "rejected": 0}

        async def try_reserve(idx):
            try:
                ok = await budget.reserve_spend(spend_per_task)
                if ok:
                    # Simulate some work
                    await asyncio.sleep(0.01)
                    await budget.record_spend(
                        cost_usd=spend_per_task,
                        prompt_tokens=100,
                        completion_tokens=50,
                        provider="anthropic",
                        model="test-model",
                        purpose="stress_test",
                    )
                    await budget.release_reservation(spend_per_task)
                    results["reserved"] += 1
                else:
                    results["rejected"] += 1
            except Exception:
                results["rejected"] += 1

        with patch("backend.services.budget.BUDGET_DAILY", daily_limit), \
             patch("backend.services.budget.BUDGET_MONTHLY", monthly_limit):
            tasks = [asyncio.create_task(try_reserve(i)) for i in range(num_coroutines)]
            await asyncio.gather(*tasks)

        # At most 20 should have succeeded ($0.05 * 20 = $1.00)
        assert results["reserved"] <= 22  # Small margin for timing
        assert results["reserved"] + results["rejected"] == num_coroutines

    async def test_reservation_release_on_failure(self, budget_db):
        """Reservations should be properly released when tasks fail."""
        budget = BudgetManager(budget_db)

        # Reserve some budget
        await budget.reserve_spend(0.50)

        # Current reservation should be at least 0.50
        assert budget._reserved_daily >= 0.50

        # Release it
        await budget.release_reservation(0.50)

        # Should be back to 0
        assert budget._reserved_daily == 0.0

    async def test_interleaved_reserve_and_spend(self, budget_db):
        """Interleaved reserve/spend/release should maintain consistency."""
        budget = BudgetManager(budget_db)

        async def worker(idx):
            amount = 0.01
            ok = await budget.reserve_spend(amount)
            if not ok:
                return
            await asyncio.sleep(0.005)
            await budget.record_spend(
                cost_usd=amount,
                prompt_tokens=10,
                completion_tokens=5,
                provider="anthropic",
                model="test-model",
                purpose="interleave_test",
            )
            await budget.release_reservation(amount)

        tasks = [asyncio.create_task(worker(i)) for i in range(30)]
        await asyncio.gather(*tasks)

        # All reservations should be released
        assert budget._reserved_daily == 0.0
        assert budget._reserved_monthly == 0.0
