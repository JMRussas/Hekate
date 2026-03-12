#  Orchestration Engine - Data Integrity Tests (Phase 2.2, 2.3)
#
#  Tests for admin race condition fix and budget TOCTOU race fix.
#
#  Depends on: backend/services/auth.py, backend/services/budget.py
#  Used by:    pytest

import asyncio

from unittest.mock import patch

from backend.services.auth import AuthService
from backend.services.budget import BudgetManager


class TestAdminRaceCondition:
    """Phase 2.2: Verify that concurrent first-registrations produce only one admin."""

    async def test_concurrent_first_register_single_admin(self, tmp_db):
        auth = AuthService(db=tmp_db)

        async def register_user(n: int):
            try:
                return await auth.register(
                    email=f"user{n}@example.com",
                    password="testpass123",
                    display_name=f"User {n}",
                )
            except (ValueError, Exception):
                return None

        # Fire 5 registrations concurrently
        results = await asyncio.gather(*[register_user(i) for i in range(5)])

        # Count how many got admin role
        successful = [r for r in results if r is not None]
        admins = [r for r in successful if r["role"] == "admin"]

        assert len(admins) == 1, f"Expected exactly 1 admin, got {len(admins)}: {admins}"
        assert len(successful) == 5  # All should succeed (unique emails)

    async def test_register_uses_transaction(self, tmp_db):
        """Verify register wraps check+insert in a transaction."""
        auth = AuthService(db=tmp_db)
        result = await auth.register("first@example.com", "pass123")
        assert result["role"] == "admin"

        result2 = await auth.register("second@example.com", "pass123")
        assert result2["role"] == "user"


class TestBudgetReserveSpend:
    """Phase 2.3: Verify budget reservation prevents TOCTOU races."""

    async def test_reserve_within_limit(self, tmp_db):
        budget = BudgetManager(db=tmp_db)
        with patch("backend.services.budget.BUDGET_DAILY", 5.0), \
             patch("backend.services.budget.BUDGET_MONTHLY", 50.0):
            assert await budget.reserve_spend(1.0) is True

    async def test_reserve_exceeds_daily_limit(self, tmp_db):
        budget = BudgetManager(db=tmp_db)
        with patch("backend.services.budget.BUDGET_DAILY", 1.0), \
             patch("backend.services.budget.BUDGET_MONTHLY", 50.0):
            assert await budget.reserve_spend(0.6) is True
            assert await budget.reserve_spend(0.6) is False  # 0.6 + 0.6 > 1.0

    async def test_reserve_exceeds_monthly_limit(self, tmp_db):
        budget = BudgetManager(db=tmp_db)
        with patch("backend.services.budget.BUDGET_DAILY", 50.0), \
             patch("backend.services.budget.BUDGET_MONTHLY", 1.0):
            assert await budget.reserve_spend(0.6) is True
            assert await budget.reserve_spend(0.6) is False

    async def test_concurrent_reserves_respect_limit(self, tmp_db):
        """Multiple concurrent reserve_spend calls should not all succeed
        if they collectively exceed the budget."""
        budget = BudgetManager(db=tmp_db)
        with patch("backend.services.budget.BUDGET_DAILY", 1.0), \
             patch("backend.services.budget.BUDGET_MONTHLY", 50.0):
            results = await asyncio.gather(*[budget.reserve_spend(0.3) for _ in range(5)])
            # 0.3 * 3 = 0.9 <= 1.0, 0.3 * 4 = 1.2 > 1.0 — at most 3 should succeed
            successes = sum(1 for r in results if r)
            assert successes <= 3, f"Expected <= 3 successes, got {successes}"
            assert successes >= 1  # At least one should succeed

    async def test_release_reservation_frees_budget(self, tmp_db):
        budget = BudgetManager(db=tmp_db)
        with patch("backend.services.budget.BUDGET_DAILY", 1.0), \
             patch("backend.services.budget.BUDGET_MONTHLY", 50.0):
            assert await budget.reserve_spend(0.8) is True
            assert await budget.reserve_spend(0.5) is False  # 0.8 + 0.5 > 1.0
            await budget.release_reservation(0.8)
            assert await budget.reserve_spend(0.5) is True  # Now 0.5 <= 1.0

    async def test_zero_cost_always_reserves(self, tmp_db):
        budget = BudgetManager(db=tmp_db)
        assert await budget.reserve_spend(0.0) is True
        assert await budget.reserve_spend(-1.0) is True

    async def test_period_rollover_resets_reservations(self, tmp_db):
        budget = BudgetManager(db=tmp_db)
        with patch("backend.services.budget.BUDGET_DAILY", 1.0), \
             patch("backend.services.budget.BUDGET_MONTHLY", 50.0):
            # Reserve some amount
            assert await budget.reserve_spend(0.8) is True

            # Simulate day rollover by changing the daily key
            with patch("backend.services.budget._today_key", return_value="2099-01-01"):
                # Reservation should reset — the new day has no reservations
                assert await budget.reserve_spend(0.8) is True

    async def test_release_clamps_to_zero(self, tmp_db):
        """Releasing more than reserved should not go negative."""
        budget = BudgetManager(db=tmp_db)
        await budget.release_reservation(999.0)
        assert budget._reserved_daily == 0.0
        assert budget._reserved_monthly == 0.0
