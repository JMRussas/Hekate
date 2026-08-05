"""Token-bucket rate limiter for provider calls.

In-memory only — no DB persistence needed. Buckets refill based on
elapsed wall-clock time. Thread-safe via a simple lock per bucket.

Usage:
    from gods.rate_limit import ProviderRateLimiter
    from gods.config import RATE_LIMITS

    limiter = ProviderRateLimiter(RATE_LIMITS)
    if limiter.try_acquire("claude_code"):
        # proceed with call
    else:
        wait = limiter.time_until_available("claude_code")
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("gods.rate_limit")


class TokenBucket:
    """Fixed-window token bucket with burst capacity."""

    __slots__ = ("rate", "window_seconds", "burst", "_tokens", "_last_refill", "_lock")

    def __init__(self, rate: int, window_seconds: int, burst: int | None = None):
        self.rate = rate
        self.window_seconds = window_seconds
        self.burst = burst if burst is not None else rate
        self._tokens = float(self.burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """Add tokens based on elapsed time, capped at burst."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        added = elapsed * (self.rate / self.window_seconds)
        self._tokens = min(self.burst, self._tokens + added)
        self._last_refill = now

    def try_acquire(self) -> bool:
        """Consume one token. Returns True if available, False otherwise."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def tokens_available(self) -> int:
        """Current whole tokens available (after refill)."""
        with self._lock:
            self._refill()
            return int(self._tokens)

    def time_until_available(self) -> float:
        """Seconds until at least one token is available. 0.0 if already available."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                return 0.0
            deficit = 1.0 - self._tokens
            return deficit / (self.rate / self.window_seconds)


class ProviderRateLimiter:
    """Holds a TokenBucket per provider name.

    Constructed from the RATE_LIMITS dict in gods.config:
        {"claude_code": {"rate": 10, "window_seconds": 60, "burst": 12}, ...}
    """

    def __init__(self, config: dict[str, dict]):
        self._buckets: dict[str, TokenBucket] = {}
        for provider, params in config.items():
            self._buckets[provider] = TokenBucket(
                rate=params["rate"],
                window_seconds=params["window_seconds"],
                burst=params.get("burst", params["rate"]),
            )

    def try_acquire(self, provider: str) -> bool:
        """Consume a token for *provider*. Returns True if allowed.

        Unknown providers are allowed through (no bucket = no limit).
        """
        bucket = self._buckets.get(provider)
        if bucket is None:
            return True
        return bucket.try_acquire()

    def time_until_available(self, provider: str) -> float:
        """Seconds until *provider* has a token. 0.0 if ready or unknown."""
        bucket = self._buckets.get(provider)
        if bucket is None:
            return 0.0
        return bucket.time_until_available()

    def status(self) -> dict[str, dict]:
        """Introspection: current state of every bucket."""
        result = {}
        for name, bucket in self._buckets.items():
            result[name] = {
                "rate": bucket.rate,
                "window_seconds": bucket.window_seconds,
                "burst": bucket.burst,
                "tokens_available": bucket.tokens_available(),
                "time_until_available": bucket.time_until_available(),
            }
        return result
