"""Tests for TokenBucket and ProviderRateLimiter.

Covers: capacity enforcement, refill over time, burst capacity,
time_until_available accuracy, provider isolation, unknown provider passthrough.
"""

from unittest.mock import patch

import pytest

from gods.rate_limit import TokenBucket, ProviderRateLimiter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bucket(rate=10, window=60, burst=None):
    """Create a TokenBucket with a pinned monotonic clock."""
    with patch("gods.rate_limit.time.monotonic", return_value=0.0):
        return TokenBucket(rate=rate, window_seconds=window, burst=burst)


# ---------------------------------------------------------------------------
# TokenBucket — capacity enforcement
# ---------------------------------------------------------------------------

class TestTokenBucketCapacity:
    """Bucket allows exactly `burst` (defaults to rate) calls then blocks."""

    def test_allows_rate_calls_then_blocks(self):
        bucket = _make_bucket(rate=5, window=60)

        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            for _ in range(5):
                assert bucket.try_acquire() is True
            assert bucket.try_acquire() is False

    def test_tokens_available_decrements(self):
        bucket = _make_bucket(rate=3, window=60)

        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            assert bucket.tokens_available() == 3
            bucket.try_acquire()
            assert bucket.tokens_available() == 2
            bucket.try_acquire()
            bucket.try_acquire()
            assert bucket.tokens_available() == 0


# ---------------------------------------------------------------------------
# TokenBucket — refill
# ---------------------------------------------------------------------------

class TestTokenBucketRefill:
    """Tokens refill based on elapsed time."""

    def test_tokens_refill_after_window(self):
        bucket = _make_bucket(rate=10, window=60)

        # Drain all tokens at t=0
        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            for _ in range(10):
                bucket.try_acquire()
            assert bucket.try_acquire() is False

        # Advance full window — should have all 10 tokens back
        with patch("gods.rate_limit.time.monotonic", return_value=60.0):
            assert bucket.try_acquire() is True
            assert bucket.tokens_available() == 9

    def test_partial_refill(self):
        bucket = _make_bucket(rate=10, window=60)

        # Drain all at t=0
        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            for _ in range(10):
                bucket.try_acquire()

        # Advance 6s → should add 1 token (10/60 * 6 = 1.0)
        with patch("gods.rate_limit.time.monotonic", return_value=6.0):
            assert bucket.try_acquire() is True
            assert bucket.try_acquire() is False

    def test_refill_capped_at_burst(self):
        bucket = _make_bucket(rate=10, window=60, burst=12)

        # Wait a very long time — should not exceed burst
        with patch("gods.rate_limit.time.monotonic", return_value=600.0):
            assert bucket.tokens_available() == 12


# ---------------------------------------------------------------------------
# TokenBucket — burst
# ---------------------------------------------------------------------------

class TestTokenBucketBurst:
    """Burst allows exceeding the base rate briefly."""

    def test_burst_exceeds_rate(self):
        bucket = _make_bucket(rate=5, window=60, burst=8)

        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            # Should allow 8 calls (burst), not just 5 (rate)
            for i in range(8):
                assert bucket.try_acquire() is True, f"call {i+1} should succeed"
            assert bucket.try_acquire() is False

    def test_burst_default_equals_rate(self):
        bucket = _make_bucket(rate=7, window=60)
        assert bucket.burst == 7


# ---------------------------------------------------------------------------
# TokenBucket — time_until_available
# ---------------------------------------------------------------------------

class TestTimeUntilAvailable:
    """time_until_available returns correct delay."""

    def test_returns_zero_when_tokens_available(self):
        bucket = _make_bucket(rate=10, window=60)

        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            assert bucket.time_until_available() == 0.0

    def test_returns_correct_delay_when_empty(self):
        bucket = _make_bucket(rate=10, window=60)

        # Drain all at t=0
        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            for _ in range(10):
                bucket.try_acquire()

        # Still at t=0 — need 1 token, rate is 10/60s = 1 token per 6s
        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            delay = bucket.time_until_available()
            assert delay == pytest.approx(6.0, abs=0.01)

    def test_delay_decreases_as_time_passes(self):
        bucket = _make_bucket(rate=10, window=60)

        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            for _ in range(10):
                bucket.try_acquire()

        # At t=3, half the deficit is recovered
        with patch("gods.rate_limit.time.monotonic", return_value=3.0):
            delay = bucket.time_until_available()
            assert delay == pytest.approx(3.0, abs=0.01)


# ---------------------------------------------------------------------------
# ProviderRateLimiter — provider isolation
# ---------------------------------------------------------------------------

class TestProviderIsolation:
    """Draining one provider must not affect another."""

    def _make_limiter(self):
        return ProviderRateLimiter({
            "claude_code": {"rate": 3, "window_seconds": 60},
            "ollama": {"rate": 5, "window_seconds": 60},
        })

    def test_drain_one_provider_other_unaffected(self):
        limiter = self._make_limiter()

        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            # Drain claude_code
            for _ in range(3):
                limiter.try_acquire("claude_code")
            assert limiter.try_acquire("claude_code") is False

            # ollama should still be fully available
            for _ in range(5):
                assert limiter.try_acquire("ollama") is True

    def test_status_shows_all_providers(self):
        limiter = self._make_limiter()
        status = limiter.status()
        assert set(status.keys()) == {"claude_code", "ollama"}
        assert status["claude_code"]["rate"] == 3
        assert status["ollama"]["rate"] == 5


# ---------------------------------------------------------------------------
# ProviderRateLimiter — unknown provider
# ---------------------------------------------------------------------------

class TestUnknownProvider:
    """Unknown providers pass through (no bucket = no limit)."""

    def test_unknown_provider_always_allowed(self):
        limiter = ProviderRateLimiter({
            "claude_code": {"rate": 2, "window_seconds": 60},
        })

        with patch("gods.rate_limit.time.monotonic", return_value=0.0):
            # Unknown provider should always return True
            for _ in range(100):
                assert limiter.try_acquire("unknown_provider") is True

    def test_unknown_provider_time_until_available_is_zero(self):
        limiter = ProviderRateLimiter({
            "claude_code": {"rate": 2, "window_seconds": 60},
        })
        assert limiter.time_until_available("unknown_provider") == 0.0
