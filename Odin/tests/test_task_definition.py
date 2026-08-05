"""Tests for RetryPolicy compute_delay and TaskDefinition serialization.

Covers: FIXED/EXPONENTIAL/LINEAR strategies, max_delay cap, jitter bounds,
to_dict/from_dict round-trips, invalid strategy fallback, and
TaskDefinition delegation to retry_policy vs legacy logic.
"""

import random

import pytest

from gods.task_definition import (
    RetryLogic,
    RetryPolicy,
    TaskDefinition,
    MAX_RETRY_DELAY,
)


# ---------------------------------------------------------------------------
# RetryPolicy.compute_delay — strategy math (jitter=False for determinism)
# ---------------------------------------------------------------------------


class TestRetryPolicyFixed:
    """FIXED strategy returns base_delay_seconds for all attempts."""

    @pytest.fixture
    def policy(self):
        return RetryPolicy(
            strategy=RetryLogic.FIXED,
            base_delay_seconds=30,
            max_delay_seconds=300,
            jitter=False,
        )

    @pytest.mark.parametrize("attempt", [0, 1, 2, 5, 10])
    def test_returns_base_for_all_attempts(self, policy, attempt):
        assert policy.compute_delay(attempt) == 30.0


class TestRetryPolicyExponential:
    """EXPONENTIAL_BACKOFF: base * (rate ** attempt)."""

    @pytest.fixture
    def policy(self):
        return RetryPolicy(
            strategy=RetryLogic.EXPONENTIAL_BACKOFF,
            base_delay_seconds=30,
            max_delay_seconds=1000,
            jitter=False,
            backoff_rate=2.0,
        )

    @pytest.mark.parametrize(
        "attempt, expected",
        [(0, 30.0), (1, 60.0), (2, 120.0), (3, 240.0)],
    )
    def test_exponential_progression(self, policy, attempt, expected):
        assert policy.compute_delay(attempt) == expected


class TestRetryPolicyLinear:
    """LINEAR_BACKOFF: base * rate * max(attempt, 1)."""

    @pytest.fixture
    def policy(self):
        return RetryPolicy(
            strategy=RetryLogic.LINEAR_BACKOFF,
            base_delay_seconds=30,
            max_delay_seconds=1000,
            jitter=False,
            backoff_rate=2.0,
        )

    @pytest.mark.parametrize(
        "attempt, expected",
        [
            (0, 60.0),   # max(0,1)=1 → 30*2*1=60
            (1, 60.0),   # 30*2*1=60
            (2, 120.0),  # 30*2*2=120
        ],
    )
    def test_linear_progression(self, policy, attempt, expected):
        assert policy.compute_delay(attempt) == expected


class TestRetryPolicyMaxDelay:
    """max_delay_seconds caps computed delay."""

    def test_exponential_capped(self):
        policy = RetryPolicy(
            strategy=RetryLogic.EXPONENTIAL_BACKOFF,
            base_delay_seconds=30,
            max_delay_seconds=100,
            jitter=False,
            backoff_rate=2.0,
        )
        # attempt 5 → 30 * 2^5 = 960, capped at 100
        assert policy.compute_delay(5) == 100.0

    def test_linear_capped(self):
        policy = RetryPolicy(
            strategy=RetryLogic.LINEAR_BACKOFF,
            base_delay_seconds=30,
            max_delay_seconds=100,
            jitter=False,
            backoff_rate=2.0,
        )
        # attempt 10 → 30*2*10=600, capped at 100
        assert policy.compute_delay(10) == 100.0


class TestRetryPolicyJitter:
    """With jitter=True, result is in [computed/2, computed] (equal jitter)."""

    def test_jitter_bounds_100_iterations(self):
        policy = RetryPolicy(
            strategy=RetryLogic.EXPONENTIAL_BACKOFF,
            base_delay_seconds=30,
            max_delay_seconds=1000,
            jitter=True,
            backoff_rate=2.0,
        )
        # attempt 2 → computed = 120, then capped → 120
        # jitter: half=60, result ∈ [60, 120]
        computed_no_jitter = 120.0
        lo = computed_no_jitter / 2
        hi = computed_no_jitter

        rng = random.Random(42)
        orig_uniform = random.uniform

        results = []
        for _ in range(100):
            # Monkey-patch random.uniform to use seeded RNG
            random.uniform = rng.uniform
            results.append(policy.compute_delay(2))
        random.uniform = orig_uniform

        for val in results:
            assert lo <= val <= hi, f"jitter value {val} outside [{lo}, {hi}]"

        # Verify there's actual spread (not all identical)
        assert len(set(results)) > 1


# ---------------------------------------------------------------------------
# RetryPolicy serialization
# ---------------------------------------------------------------------------


class TestRetryPolicySerialization:
    """to_dict / from_dict round-trip."""

    def test_round_trip_preserves_all_fields(self):
        original = RetryPolicy(
            strategy=RetryLogic.LINEAR_BACKOFF,
            base_delay_seconds=45,
            max_delay_seconds=500,
            jitter=False,
            backoff_rate=1.5,
        )
        d = original.to_dict()
        restored = RetryPolicy.from_dict(d)

        assert restored.strategy == original.strategy
        assert restored.base_delay_seconds == original.base_delay_seconds
        assert restored.max_delay_seconds == original.max_delay_seconds
        assert restored.jitter == original.jitter
        assert restored.backoff_rate == original.backoff_rate

    def test_round_trip_dict_equality(self):
        original = RetryPolicy(
            strategy=RetryLogic.EXPONENTIAL_BACKOFF,
            base_delay_seconds=60,
            max_delay_seconds=300,
            jitter=True,
            backoff_rate=2.0,
        )
        assert RetryPolicy.from_dict(original.to_dict()).to_dict() == original.to_dict()

    def test_from_dict_invalid_strategy_falls_back_to_default(self):
        data = {
            "strategy": "DOES_NOT_EXIST",
            "base_delay_seconds": 10,
            "max_delay_seconds": 50,
        }
        policy = RetryPolicy.from_dict(data)
        # strategy should be the default (EXPONENTIAL_BACKOFF)
        assert policy.strategy == RetryLogic.EXPONENTIAL_BACKOFF
        # other fields should be parsed
        assert policy.base_delay_seconds == 10
        assert policy.max_delay_seconds == 50

    def test_from_dict_none_returns_default(self):
        policy = RetryPolicy.from_dict(None)
        assert policy.strategy == RetryLogic.EXPONENTIAL_BACKOFF
        assert policy.base_delay_seconds == 30

    def test_from_dict_empty_dict_returns_default(self):
        policy = RetryPolicy.from_dict({})
        default = RetryPolicy()
        assert policy.strategy == default.strategy
        assert policy.base_delay_seconds == default.base_delay_seconds


# ---------------------------------------------------------------------------
# TaskDefinition.compute_retry_delay delegation
# ---------------------------------------------------------------------------


class TestTaskDefinitionRetryDelegation:
    """TaskDefinition delegates to retry_policy when set, else uses legacy logic."""

    def test_delegates_to_retry_policy(self):
        rp = RetryPolicy(
            strategy=RetryLogic.EXPONENTIAL_BACKOFF,
            base_delay_seconds=30,
            max_delay_seconds=1000,
            jitter=False,
            backoff_rate=2.0,
        )
        td = TaskDefinition(retry_policy=rp)

        # attempt 2 → 30 * 2^2 = 120
        assert td.compute_retry_delay(2) == 120

    def test_legacy_fixed_when_no_policy(self):
        td = TaskDefinition(
            retry_logic=RetryLogic.FIXED,
            retry_delay_seconds=45,
            retry_policy=None,
        )
        assert td.compute_retry_delay(0) == 45
        assert td.compute_retry_delay(3) == 45

    def test_legacy_exponential_when_no_policy(self):
        td = TaskDefinition(
            retry_logic=RetryLogic.EXPONENTIAL_BACKOFF,
            retry_delay_seconds=30,
            retry_policy=None,
        )
        # Legacy uses 2**attempt (hardcoded rate)
        assert td.compute_retry_delay(0) == 30   # 30 * 2^0
        assert td.compute_retry_delay(3) == 240   # 30 * 2^3

    def test_legacy_linear_when_no_policy(self):
        td = TaskDefinition(
            retry_logic=RetryLogic.LINEAR_BACKOFF,
            retry_delay_seconds=30,
            backoff_rate=2.0,
            retry_policy=None,
        )
        # Legacy: delay * rate * max(attempt,1)
        assert td.compute_retry_delay(0) == 60   # 30*2*1
        assert td.compute_retry_delay(2) == 120   # 30*2*2

    def test_legacy_respects_max_cap(self):
        td = TaskDefinition(
            retry_logic=RetryLogic.EXPONENTIAL_BACKOFF,
            retry_delay_seconds=1000,
            retry_policy=None,
        )
        # attempt 5 → 1000 * 2^5 = 32000, capped at MAX_RETRY_DELAY (3600)
        assert td.compute_retry_delay(5) == MAX_RETRY_DELAY
