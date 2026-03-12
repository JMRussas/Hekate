#  Orchestration Engine - Login Lockout Unit Tests
#
#  Tests for the brute-force login protection in AuthService.
#
#  Depends on: backend/services/auth.py
#  Used by:    CI

import time
from unittest.mock import patch

import pytest


class TestLoginLockout:
    """Tests for per-account brute-force login protection."""

    async def test_below_threshold_still_allowed(self, auth_service):
        """4 failed logins should not lock out (threshold is 5)."""
        # Register a user
        await auth_service.register("lockout@test.com", "correct-password-123")

        # Fail 4 times
        for _ in range(4):
            with pytest.raises(ValueError, match="Invalid email or password"):
                await auth_service.login("lockout@test.com", "wrong-password")

        # 5th attempt with correct password should succeed
        result = await auth_service.login("lockout@test.com", "correct-password-123")
        assert "access_token" in result

    async def test_lockout_after_threshold(self, auth_service):
        """After 5 failures, 6th attempt returns same error (no enumeration)."""
        await auth_service.register("locked@test.com", "correct-password-123")

        # Fail 5 times to trigger lockout
        for _ in range(5):
            with pytest.raises(ValueError, match="Invalid email or password"):
                await auth_service.login("locked@test.com", "wrong-password")

        # 6th attempt should fail even with correct password — same error message
        with pytest.raises(ValueError, match="Invalid email or password"):
            await auth_service.login("locked@test.com", "correct-password-123")

    async def test_successful_login_clears_counter(self, auth_service):
        """A successful login should reset the failure counter."""
        await auth_service.register("clear@test.com", "correct-password-123")

        # Fail 3 times
        for _ in range(3):
            with pytest.raises(ValueError, match="Invalid email or password"):
                await auth_service.login("clear@test.com", "wrong-password")

        # Successful login resets counter
        result = await auth_service.login("clear@test.com", "correct-password-123")
        assert "access_token" in result

        # Can fail 4 more times without lockout (counter was reset)
        for _ in range(4):
            with pytest.raises(ValueError, match="Invalid email or password"):
                await auth_service.login("clear@test.com", "wrong-password")

        # 5th failure after reset — still below new threshold
        result = await auth_service.login("clear@test.com", "correct-password-123")
        assert "access_token" in result

    async def test_failures_expire_after_window(self, auth_service):
        """Failures older than the window should be forgotten."""
        await auth_service.register("expire@test.com", "correct-password-123")

        # Fail 5 times to trigger lockout
        for _ in range(5):
            with pytest.raises(ValueError, match="Invalid email or password"):
                await auth_service.login("expire@test.com", "wrong-password")

        # Should be locked out now
        with pytest.raises(ValueError, match="Invalid email or password"):
            await auth_service.login("expire@test.com", "correct-password-123")

        # Fast-forward past the lockout window by manipulating the timestamp
        email_entry = auth_service._login_failures.get("expire@test.com")
        assert email_entry is not None
        fail_count, _ = email_entry
        # Set the first_fail_timestamp to 301 seconds ago (window is 300s)
        auth_service._login_failures["expire@test.com"] = (
            fail_count,
            time.time() - 301,
        )

        # Now login should succeed — failures have expired
        result = await auth_service.login("expire@test.com", "correct-password-123")
        assert "access_token" in result

    async def test_different_accounts_independent(self, auth_service):
        """Lockout for one account should not affect another."""
        await auth_service.register("alice@test.com", "alice-pass-123")
        await auth_service.register("bob@test.com", "bob-pass-12345")

        # Lock out alice
        for _ in range(5):
            with pytest.raises(ValueError, match="Invalid email or password"):
                await auth_service.login("alice@test.com", "wrong-password")

        # Alice is locked out
        with pytest.raises(ValueError, match="Invalid email or password"):
            await auth_service.login("alice@test.com", "alice-pass-123")

        # Bob should still be fine
        result = await auth_service.login("bob@test.com", "bob-pass-12345")
        assert "access_token" in result

    async def test_memory_cap_evicts_stale(self, auth_service):
        """When exceeding max tracked emails, stale entries are evicted."""
        with patch(
            "backend.services.auth.AUTH_LOGIN_MAX_TRACKED", 5
        ):
            # Insert 6 old entries (older than lockout window)
            old_ts = time.time() - 400  # older than 300s window
            for i in range(6):
                auth_service._login_failures[f"old{i}@test.com"] = (3, old_ts)

            # Register and try to login — eviction should happen
            await auth_service.register("new@test.com", "new-password-123")
            result = await auth_service.login("new@test.com", "new-password-123")
            assert "access_token" in result

            # Old entries should have been evicted (they were past the window)
            assert len(auth_service._login_failures) <= 5

    async def test_memory_cap_hard_eviction(self, auth_service):
        """When all entries are within the window, oldest are evicted by timestamp."""
        with patch(
            "backend.services.auth.AUTH_LOGIN_MAX_TRACKED", 5
        ):
            now = time.time()
            # Insert 6 recent entries (all within window)
            for i in range(6):
                auth_service._login_failures[f"recent{i}@test.com"] = (
                    3,
                    now - i * 10,  # progressively older but all within 300s
                )

            # Register and trigger eviction via login
            await auth_service.register("trigger@test.com", "trigger-pass-123")
            result = await auth_service.login("trigger@test.com", "trigger-pass-123")
            assert "access_token" in result

            # Should be capped at or below 5
            assert len(auth_service._login_failures) <= 5
            # The oldest entry should have been evicted
            assert "recent5@test.com" not in auth_service._login_failures

    async def test_nonexistent_user_records_failure(self, auth_service):
        """Login attempts for non-existent users should also be tracked."""
        # Attempt to login with a non-existent email 5 times
        for _ in range(5):
            with pytest.raises(ValueError, match="Invalid email or password"):
                await auth_service.login("ghost@test.com", "any-password")

        # Should be tracked in failures
        assert "ghost@test.com" in auth_service._login_failures
        fail_count, _ = auth_service._login_failures["ghost@test.com"]
        assert fail_count == 5
