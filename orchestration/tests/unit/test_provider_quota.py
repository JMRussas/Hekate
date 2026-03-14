#  Orchestration Engine - Provider Quota Manager Tests
#
#  Unit tests for real-time provider utilization tracking against usage_log.
#  Covers sliding window boundaries, weekly caps, request-based tracking (Gemini),
#  warn_pct thresholds, Ollama unlimited behaviour, and asyncio.Lock concurrency.
#
#  Depends on: backend/services/provider_quota.py, backend/db/connection.py,
#              backend/services/model_router.py
#  Used by:    pytest

import asyncio
import time

import pytest

from backend.models.enums import ModelTier
from backend.services.model_router import get_available_tiers
from backend.services.provider_quota import ProviderQuotaManager


# ---------------------------------------------------------------------------
# Config constants matching config.example.json shapes
# ---------------------------------------------------------------------------

_CLAUDE_CFG = {
    "warn_pct": 80,
    "windows": [
        {"name": "sliding_5h", "duration_hours": 5, "metric": "tokens", "limit": 88000},
        {"name": "weekly", "duration_hours": 168, "metric": "tokens", "limit": 500000},
    ],
}

_GEMINI_CFG = {
    "warn_pct": 80,
    "windows": [
        {"name": "daily", "duration_hours": 24, "metric": "requests", "limit": 1000},
        {"name": "per_minute", "duration_minutes": 1, "metric": "requests", "limit": 60},
    ],
}

_CODEX_CFG = {
    "warn_pct": 80,
    "windows": [
        {"name": "sliding_5h", "duration_hours": 5, "metric": "tokens", "limit": 40000},
        {"name": "weekly", "duration_hours": 168, "metric": "tokens", "limit": 250000},
    ],
}

_OLLAMA_CFG = {
    "warn_pct": 80,
    "windows": [],
}

_ALL_PROVIDERS = {
    "claude_code": _CLAUDE_CFG,
    "gemini_cli": _GEMINI_CFG,
    "codex_cli": _CODEX_CFG,
    "ollama": _OLLAMA_CFG,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _insert_tokens(db, provider: str, prompt: int, completion: int, ts: float) -> None:
    """Insert a single usage_log row with explicit token counts and timestamp."""
    await db.execute_write(
        "INSERT INTO usage_log (provider, model, prompt_tokens, completion_tokens, cost_usd, timestamp) "
        "VALUES (?, 'test-model', ?, ?, 0.0, ?)",
        (provider, prompt, completion, ts),
    )


async def _insert_request(db, provider: str, ts: float) -> None:
    """Insert a zero-token usage_log row (request-count providers like Gemini)."""
    await _insert_tokens(db, provider, 0, 0, ts)


def _make_manager(db, quota_config: dict) -> ProviderQuotaManager:
    """Construct a ProviderQuotaManager and inject quota_config directly, bypassing cfg()."""
    mgr = ProviderQuotaManager(db=db)
    mgr._quota_config = quota_config
    return mgr


# ---------------------------------------------------------------------------
# Sliding window boundary rollover (5h)
# ---------------------------------------------------------------------------

class TestSlidingWindowBoundary:
    """Entries at the window edge must be included; entries outside must be excluded."""

    async def test_entry_inside_window_counted(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 40000, 4000, now - (5 * 3600) + 1)

        status = await mgr.get_quota_status()
        w5h = next(w for w in status["claude_code"].windows if w.name == "sliding_5h")
        assert w5h.used == 44000

    async def test_entry_outside_window_excluded(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 40000, 4000, now - (5 * 3600) - 1)

        status = await mgr.get_quota_status()
        w5h = next(w for w in status["claude_code"].windows if w.name == "sliding_5h")
        assert w5h.used == 0

    async def test_only_in_window_entries_counted(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 10000, 1000, now - 3600)      # inside  → 11 000
        await _insert_tokens(tmp_db, "claude_code", 50000, 5000, now - 6 * 3600)  # outside → excluded

        status = await mgr.get_quota_status()
        w5h = next(w for w in status["claude_code"].windows if w.name == "sliding_5h")
        assert w5h.used == 11000

    async def test_multiple_in_window_entries_summed(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        for _ in range(5):
            await _insert_tokens(tmp_db, "claude_code", 1000, 500, now - 1800)

        status = await mgr.get_quota_status()
        w5h = next(w for w in status["claude_code"].windows if w.name == "sliding_5h")
        assert w5h.used == 5 * 1500  # 5 × (1000 + 500)

    async def test_empty_db_returns_zero_used(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})

        status = await mgr.get_quota_status()
        for w in status["claude_code"].windows:
            assert w.used == 0


# ---------------------------------------------------------------------------
# Weekly cap enforcement
# ---------------------------------------------------------------------------

class TestWeeklyCap:
    async def test_tokens_well_below_weekly_not_warned(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 100000, 10000, now - 48 * 3600)  # 110 000

        status = await mgr.get_quota_status()
        weekly = next(w for w in status["claude_code"].windows if w.name == "weekly")
        assert weekly.used == 110000
        assert not weekly.is_warned

    async def test_tokens_at_80pct_of_weekly_triggers_warning(self, tmp_db):
        # 80% of 500 000 = 400 000 tokens
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 200000, 200000, now - 48 * 3600)

        status = await mgr.get_quota_status()
        weekly = next(w for w in status["claude_code"].windows if w.name == "weekly")
        assert weekly.used == 400000
        assert weekly.utilization_pct == 80.0
        assert weekly.is_warned
        assert status["claude_code"].is_warned

    async def test_entries_beyond_weekly_window_excluded(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 250000, 250000, now - 169 * 3600)

        status = await mgr.get_quota_status()
        weekly = next(w for w in status["claude_code"].windows if w.name == "weekly")
        assert weekly.used == 0
        assert not weekly.is_warned

    async def test_5h_and_weekly_warn_independently(self, tmp_db):
        """Short-window breach and weekly breach can co-exist; provider reports is_warned=True."""
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        # 5h window: 74 800 / 88 000 ≈ 85% → warned
        await _insert_tokens(tmp_db, "claude_code", 40000, 34800, now - 3600)
        # Weekly: also push the cumulative total well above 80% of 500 000
        await _insert_tokens(tmp_db, "claude_code", 200000, 200000, now - 48 * 3600)

        status = await mgr.get_quota_status()
        claude = status["claude_code"]
        w5h = next(w for w in claude.windows if w.name == "sliding_5h")
        weekly = next(w for w in claude.windows if w.name == "weekly")

        assert w5h.is_warned
        assert weekly.is_warned
        assert claude.is_warned


# ---------------------------------------------------------------------------
# Gemini: mixed request-based windows
# ---------------------------------------------------------------------------

class TestGeminiRequestTracking:
    async def test_daily_counts_requests_not_tokens(self, tmp_db):
        mgr = _make_manager(tmp_db, {"gemini_cli": _GEMINI_CFG})
        now = time.time()

        # Large token values must not affect request-count metric
        for _ in range(5):
            await _insert_tokens(tmp_db, "gemini_cli", 99999, 99999, now - 3600)

        status = await mgr.get_quota_status()
        daily = next(w for w in status["gemini_cli"].windows if w.name == "daily")
        assert daily.metric == "requests"
        assert daily.used == 5

    async def test_per_minute_counts_only_recent_requests(self, tmp_db):
        mgr = _make_manager(tmp_db, {"gemini_cli": _GEMINI_CFG})
        now = time.time()

        for _ in range(10):
            await _insert_request(tmp_db, "gemini_cli", now - 30)   # inside 1-min window
        for _ in range(20):
            await _insert_request(tmp_db, "gemini_cli", now - 90)   # outside window

        status = await mgr.get_quota_status()
        per_min = next(w for w in status["gemini_cli"].windows if w.name == "per_minute")
        assert per_min.metric == "requests"
        assert per_min.used == 10

    async def test_daily_at_80pct_warns(self, tmp_db):
        # 800 of 1000 daily requests = 80%
        mgr = _make_manager(tmp_db, {"gemini_cli": _GEMINI_CFG})
        now = time.time()

        for _ in range(800):
            await _insert_request(tmp_db, "gemini_cli", now - 3600)

        status = await mgr.get_quota_status()
        daily = next(w for w in status["gemini_cli"].windows if w.name == "daily")
        assert daily.utilization_pct == 80.0
        assert daily.is_warned
        assert status["gemini_cli"].is_warned

    async def test_daily_below_80pct_does_not_warn(self, tmp_db):
        # 799 requests → 79.9% < 80%
        mgr = _make_manager(tmp_db, {"gemini_cli": _GEMINI_CFG})
        now = time.time()

        for _ in range(799):
            await _insert_request(tmp_db, "gemini_cli", now - 3600)

        status = await mgr.get_quota_status()
        assert not status["gemini_cli"].is_warned

    async def test_requests_outside_daily_window_excluded(self, tmp_db):
        mgr = _make_manager(tmp_db, {"gemini_cli": _GEMINI_CFG})
        now = time.time()

        for _ in range(1000):
            await _insert_request(tmp_db, "gemini_cli", now - 25 * 3600)

        status = await mgr.get_quota_status()
        daily = next(w for w in status["gemini_cli"].windows if w.name == "daily")
        assert daily.used == 0

    async def test_per_minute_independently_warns(self, tmp_db):
        """Per-minute limit (60) can fire while daily is fine; provider must be warned."""
        mgr = _make_manager(tmp_db, {"gemini_cli": _GEMINI_CFG})
        now = time.time()

        # 48 requests in the last 30s → 80% of per_minute limit
        for _ in range(48):
            await _insert_request(tmp_db, "gemini_cli", now - 10)

        status = await mgr.get_quota_status()
        per_min = next(w for w in status["gemini_cli"].windows if w.name == "per_minute")
        assert per_min.utilization_pct == 80.0
        assert per_min.is_warned
        assert status["gemini_cli"].is_warned


# ---------------------------------------------------------------------------
# warn_pct threshold precision
# ---------------------------------------------------------------------------

class TestWarnPctThreshold:
    async def test_exactly_at_warn_pct_is_warned(self, tmp_db):
        """Utilization at exactly warn_pct must set is_warned=True."""
        mgr = _make_manager(tmp_db, {"codex_cli": _CODEX_CFG})
        now = time.time()

        # 80% of 40 000 = 32 000 tokens
        await _insert_tokens(tmp_db, "codex_cli", 16000, 16000, now - 3600)

        status = await mgr.get_quota_status()
        w5h = next(w for w in status["codex_cli"].windows if w.name == "sliding_5h")
        assert w5h.used == 32000
        assert w5h.utilization_pct == 80.0
        assert w5h.is_warned

    async def test_below_warn_pct_not_warned(self, tmp_db):
        """31 900 tokens → 79.75% (rounds to 79.8) — must NOT warn."""
        mgr = _make_manager(tmp_db, {"codex_cli": _CODEX_CFG})
        now = time.time()

        await _insert_tokens(tmp_db, "codex_cli", 16000, 15900, now - 3600)

        status = await mgr.get_quota_status()
        w5h = next(w for w in status["codex_cli"].windows if w.name == "sliding_5h")
        assert w5h.used == 31900
        assert w5h.utilization_pct < 80.0
        assert not w5h.is_warned
        assert not status["codex_cli"].is_warned

    async def test_one_token_above_warn_pct_is_warned(self, tmp_db):
        """32 001 tokens → just over 80% — must warn."""
        mgr = _make_manager(tmp_db, {"codex_cli": _CODEX_CFG})
        now = time.time()

        await _insert_tokens(tmp_db, "codex_cli", 16000, 16001, now - 3600)

        status = await mgr.get_quota_status()
        w5h = next(w for w in status["codex_cli"].windows if w.name == "sliding_5h")
        assert w5h.is_warned

    async def test_custom_warn_pct_respected(self, tmp_db):
        """Provider configured with warn_pct=50 warns at 50%, not 80%."""
        cfg = {
            "claude_code": {
                "warn_pct": 50,
                "windows": [
                    {"name": "sliding_5h", "duration_hours": 5, "metric": "tokens", "limit": 100000},
                ],
            }
        }
        mgr = _make_manager(tmp_db, cfg)
        now = time.time()

        # Exactly 50% of 100 000 = 50 000 tokens
        await _insert_tokens(tmp_db, "claude_code", 25000, 25000, now - 1800)

        status = await mgr.get_quota_status()
        w5h = status["claude_code"].windows[0]
        assert w5h.utilization_pct == 50.0
        assert w5h.warn_pct == 50
        assert w5h.is_warned
        assert status["claude_code"].is_warned

    async def test_provider_is_warned_if_any_window_hot(self, tmp_db):
        """ProviderStatus.is_warned is True when at least one window exceeds warn_pct."""
        mgr = _make_manager(tmp_db, {"codex_cli": _CODEX_CFG})
        now = time.time()

        # Weekly window fine (small recent spend), but push 5h window over threshold
        await _insert_tokens(tmp_db, "codex_cli", 16000, 16000, now - 1800)   # 32 000 → 80% of 5h

        status = await mgr.get_quota_status()
        assert status["codex_cli"].is_warned


# ---------------------------------------------------------------------------
# Ollama: no limits, always available
# ---------------------------------------------------------------------------

class TestOllamaNoLimits:
    async def test_ollama_has_limits_false(self, tmp_db):
        mgr = _make_manager(tmp_db, {"ollama": _OLLAMA_CFG})

        status = await mgr.get_quota_status()
        assert "ollama" in status
        assert status["ollama"].has_limits is False
        assert status["ollama"].windows == []
        assert not status["ollama"].is_warned

    async def test_ollama_is_always_available(self, tmp_db):
        mgr = _make_manager(tmp_db, {"ollama": _OLLAMA_CFG})

        assert await mgr.is_provider_available("ollama") is True

    async def test_ollama_available_when_all_cloud_hot(self, tmp_db):
        """When all cloud providers exceed warn_pct, Ollama must still be available."""
        mgr = _make_manager(tmp_db, _ALL_PROVIDERS)
        now = time.time()

        # claude_code: 70 400 / 88 000 = 80%
        await _insert_tokens(tmp_db, "claude_code", 35200, 35200, now - 1800)
        # codex_cli: 32 000 / 40 000 = 80%
        await _insert_tokens(tmp_db, "codex_cli", 16000, 16000, now - 1800)
        # gemini_cli: 800 / 1000 = 80%
        for _ in range(800):
            await _insert_request(tmp_db, "gemini_cli", now - 1800)

        assert not await mgr.is_provider_available("claude_code")
        assert not await mgr.is_provider_available("codex_cli")
        assert not await mgr.is_provider_available("gemini_cli")
        assert await mgr.is_provider_available("ollama")


# ---------------------------------------------------------------------------
# is_provider_available
# ---------------------------------------------------------------------------

class TestIsProviderAvailable:
    async def test_unknown_provider_treated_as_available(self, tmp_db):
        mgr = _make_manager(tmp_db, {})
        assert await mgr.is_provider_available("some_unknown_provider") is True

    async def test_available_below_threshold(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 5000, 5000, now - 1800)

        assert await mgr.is_provider_available("claude_code") is True

    async def test_unavailable_at_threshold(self, tmp_db):
        # Exactly at warn_pct must return False
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        # 70 400 = 80% of 88 000
        await _insert_tokens(tmp_db, "claude_code", 35200, 35200, now - 1800)

        assert await mgr.is_provider_available("claude_code") is False

    async def test_empty_db_all_providers_available(self, tmp_db):
        mgr = _make_manager(tmp_db, _ALL_PROVIDERS)

        for provider in ["claude_code", "gemini_cli", "codex_cli", "ollama"]:
            assert await mgr.is_provider_available(provider) is True


# ---------------------------------------------------------------------------
# get_quota_status: structure and metadata
# ---------------------------------------------------------------------------

class TestGetQuotaStatusStructure:
    async def test_all_configured_providers_in_result(self, tmp_db):
        mgr = _make_manager(tmp_db, _ALL_PROVIDERS)

        status = await mgr.get_quota_status()
        assert set(status.keys()) == {"claude_code", "gemini_cli", "codex_cli", "ollama"}

    async def test_window_names_preserved(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})

        status = await mgr.get_quota_status()
        names = {w.name for w in status["claude_code"].windows}
        assert names == {"sliding_5h", "weekly"}

    async def test_duration_seconds_from_hours(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})

        status = await mgr.get_quota_status()
        w5h = next(w for w in status["claude_code"].windows if w.name == "sliding_5h")
        assert w5h.duration_seconds == 5 * 3600

    async def test_duration_seconds_from_minutes(self, tmp_db):
        mgr = _make_manager(tmp_db, {"gemini_cli": _GEMINI_CFG})

        status = await mgr.get_quota_status()
        per_min = next(w for w in status["gemini_cli"].windows if w.name == "per_minute")
        assert per_min.duration_seconds == 60

    async def test_as_dict_structure(self, tmp_db):
        mgr = _make_manager(tmp_db, {"ollama": _OLLAMA_CFG})

        status = await mgr.get_quota_status()
        d = status["ollama"].as_dict()
        assert d["provider"] == "ollama"
        assert d["has_limits"] is False
        assert isinstance(d["windows"], list)

    async def test_empty_config_returns_empty_dict(self, tmp_db):
        mgr = _make_manager(tmp_db, {})

        status = await mgr.get_quota_status()
        assert status == {}

    async def test_provider_not_in_config_not_in_result(self, tmp_db):
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        # Insert rows for a provider not in config — must not appear in result
        await _insert_tokens(tmp_db, "gemini_cli", 1000, 1000, now - 100)

        status = await mgr.get_quota_status()
        assert "gemini_cli" not in status


# ---------------------------------------------------------------------------
# Concurrent asyncio.Lock safety
# ---------------------------------------------------------------------------

class TestConcurrencyLock:
    async def test_concurrent_get_quota_status_all_complete(self, tmp_db):
        """10 concurrent get_quota_status() calls must all complete without error."""
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()
        await _insert_tokens(tmp_db, "claude_code", 10000, 1000, now - 1800)

        results = await asyncio.gather(*[mgr.get_quota_status() for _ in range(10)])
        assert len(results) == 10
        for r in results:
            assert "claude_code" in r

    async def test_concurrent_is_provider_available_consistent(self, tmp_db):
        """All concurrent availability checks must agree on the same answer."""
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        # Provider is hot (80% utilisation)
        await _insert_tokens(tmp_db, "claude_code", 35200, 35200, now - 1800)

        results = await asyncio.gather(
            *[mgr.is_provider_available("claude_code") for _ in range(10)]
        )
        assert all(r is False for r in results)

    async def test_lock_does_not_deadlock_under_contention(self, tmp_db):
        """Concurrent bursts of calls must complete without deadlock or exception."""
        mgr = _make_manager(tmp_db, _ALL_PROVIDERS)

        async def burst():
            for _ in range(3):
                await mgr.get_quota_status()

        # Five concurrent bursters — if the lock is re-entrant or broken, this hangs
        await asyncio.gather(*[burst() for _ in range(5)])

    async def test_mixed_concurrent_read_and_availability_checks(self, tmp_db):
        """get_quota_status() and is_provider_available() can be called concurrently."""
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG, "ollama": _OLLAMA_CFG})

        coros = (
            [mgr.get_quota_status() for _ in range(5)]
            + [mgr.is_provider_available("claude_code") for _ in range(5)]
            + [mgr.is_provider_available("ollama") for _ in range(5)]
        )
        results = await asyncio.gather(*coros)
        # First 5 are dicts, next 10 are booleans — just check no exceptions
        assert len(results) == 15


# ---------------------------------------------------------------------------
# Concurrent routing fallback: Claude→Ollama under contention
# ---------------------------------------------------------------------------

class TestConcurrentRoutingFallback:
    """asyncio.Lock correctness + Ollama fallback routing under concurrency.

    These tests verify that:
    1. The lock serialises DB reads — concurrent availability checks never
       return split-brain answers for the same DB state.
    2. Writes past the warn threshold are immediately visible to subsequent
       lock-protected reads (no stale-read window).
    3. get_available_tiers() correctly falls back to Ollama when all cloud
       providers exceed warn_pct — with no human intervention.
    4. Under burst concurrency, routing decisions are consistent.
    """

    # ------------------------------------------------------------------
    # Routing correctness
    # ------------------------------------------------------------------

    async def test_claude_hot_falls_back_to_gemini_not_ollama(self, tmp_db):
        """When only Claude is warned, the router falls back to Gemini — not Ollama."""
        mgr = _make_manager(tmp_db, _ALL_PROVIDERS)
        now = time.time()

        # Push claude_code to exactly 80%: 70 400 / 88 000
        await _insert_tokens(tmp_db, "claude_code", 35200, 35200, now - 1800)

        # ("code", "complex") recommends CLAUDE_CODE; Gemini is clean
        tier = await get_available_tiers("code", "complex", mgr)
        assert tier == ModelTier.GEMINI_CLI

    async def test_claude_and_gemini_hot_falls_back_to_codex(self, tmp_db):
        """When Claude and Gemini are both warned, the router falls back to Codex."""
        mgr = _make_manager(tmp_db, _ALL_PROVIDERS)
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 35200, 35200, now - 1800)   # 80%
        for _ in range(800):
            await _insert_request(tmp_db, "gemini_cli", now - 1800)              # 80%

        tier = await get_available_tiers("code", "complex", mgr)
        assert tier == ModelTier.CODEX_CLI

    async def test_all_cloud_providers_hot_routes_to_ollama(self, tmp_db):
        """When all cloud providers reach warn_pct, get_available_tiers returns OLLAMA."""
        mgr = _make_manager(tmp_db, _ALL_PROVIDERS)
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 35200, 35200, now - 1800)   # 80% of 88 000
        await _insert_tokens(tmp_db, "codex_cli", 16000, 16000, now - 1800)     # 80% of 40 000
        for _ in range(800):
            await _insert_request(tmp_db, "gemini_cli", now - 1800)              # 80% of 1 000

        tier = await get_available_tiers("code", "complex", mgr)
        assert tier == ModelTier.OLLAMA

    async def test_claude_at_100pct_all_cloud_hot_routes_to_ollama(self, tmp_db):
        """Claude at 100% (full window exhausted) + all other cloud providers hot → OLLAMA."""
        mgr = _make_manager(tmp_db, _ALL_PROVIDERS)
        now = time.time()

        # Claude: 88 000 / 88 000 = 100%
        await _insert_tokens(tmp_db, "claude_code", 44000, 44000, now - 1800)
        # Codex: 40 000 / 40 000 = 100%
        await _insert_tokens(tmp_db, "codex_cli", 20000, 20000, now - 1800)
        # Gemini: 1 000 / 1 000 = 100%
        for _ in range(1000):
            await _insert_request(tmp_db, "gemini_cli", now - 1800)

        tier = await get_available_tiers("code", "complex", mgr)
        assert tier == ModelTier.OLLAMA

    async def test_no_quota_manager_returns_static_recommendation(self, tmp_db):
        """Without a quota_manager, get_available_tiers behaves like recommend_tier."""
        tier = await get_available_tiers("code", "complex", quota_manager=None)
        assert tier == ModelTier.CLAUDE_CODE

    # ------------------------------------------------------------------
    # Lock correctness: concurrent availability reads agree
    # ------------------------------------------------------------------

    async def test_50_concurrent_checks_all_agree_when_claude_hot(self, tmp_db):
        """50 concurrent is_provider_available calls return a consistent False
        when Claude is at warn_pct — the lock prevents split-brain reads."""
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        # 70 400 / 88 000 = exactly 80% → is_warned=True → available=False
        await _insert_tokens(tmp_db, "claude_code", 35200, 35200, now - 1800)

        gate = asyncio.Event()

        async def check():
            await gate.wait()
            return await mgr.is_provider_available("claude_code")

        tasks = [asyncio.create_task(check()) for _ in range(50)]
        gate.set()
        results = await asyncio.gather(*tasks)

        assert all(r is False for r in results), (
            "Some concurrent checks returned True — lock failed to serialise reads"
        )

    async def test_50_concurrent_checks_all_agree_when_claude_clear(self, tmp_db):
        """All concurrent availability checks return True when Claude is well below warn_pct."""
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        # 11 000 / 88 000 ≈ 12.5% — far below 80%
        await _insert_tokens(tmp_db, "claude_code", 10000, 1000, now - 1800)

        gate = asyncio.Event()

        async def check():
            await gate.wait()
            return await mgr.is_provider_available("claude_code")

        tasks = [asyncio.create_task(check()) for _ in range(50)]
        gate.set()
        results = await asyncio.gather(*tasks)

        assert all(r is True for r in results), (
            "Some concurrent checks returned False — unexpected false positive"
        )

    # ------------------------------------------------------------------
    # Lock visibility: writes past threshold visible to subsequent reads
    # ------------------------------------------------------------------

    async def test_write_past_threshold_visible_to_subsequent_locked_read(self, tmp_db):
        """A DB write that pushes utilisation past warn_pct is immediately visible
        to the next lock-protected is_provider_available call.

        This validates that the asyncio.Lock provides at-least-once read-after-write
        consistency: a caller who acquires the lock after the write sees the new state.
        """
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        # Start below threshold (79.75%)
        await _insert_tokens(tmp_db, "claude_code", 16000, 15900, now - 1800)
        assert await mgr.is_provider_available("claude_code") is True

        # Push exactly to 80%: add 100 more tokens (32 000 total)
        await _insert_tokens(tmp_db, "claude_code", 50, 50, now - 900)
        # Now 31 900 + 100 = 32 000 / 40 000 would be codex math; for Claude:
        # existing 31 900 + 100 = 32 000 ... but limit is 88 000, so still well under.
        # Use a larger push to cross 80% of 88 000 = 70 400.
        await _insert_tokens(tmp_db, "claude_code", 38400, 0, now - 300)
        # Total now: 31 900 + 100 + 38 400 = 70 400 = exactly 80% of 88 000

        assert await mgr.is_provider_available("claude_code") is False

    async def test_threshold_crossed_mid_burst_detected_eventually(self, tmp_db):
        """Start a burst of availability checks, insert rows mid-burst that push
        utilisation past warn_pct, and verify that checks which run after the
        insert return False (eventual detection, no lock starvation).

        The lock ensures reads are serialised — every caller either sees
        the pre-write or post-write state; none see a torn read.
        """
        mgr = _make_manager(tmp_db, {"claude_code": _CLAUDE_CFG})
        now = time.time()

        # Begin at 79.75% (just below threshold)
        await _insert_tokens(tmp_db, "claude_code", 16000, 15900, now - 3600)

        completed: list[bool] = []

        async def slow_check(delay: float) -> bool:
            await asyncio.sleep(delay)
            result = await mgr.is_provider_available("claude_code")
            completed.append(result)
            return result

        async def push_over_threshold():
            # Wait for the first batch of checks to start, then push over
            await asyncio.sleep(0.005)
            # Add enough tokens to cross 80% of 88 000 (need 70 400 total)
            # Current: 31 900 → add 38 501 to reach 70 401 (just over)
            await _insert_tokens(tmp_db, "claude_code", 38501, 0, now - 1)

        # 10 checks staggered across the write event
        check_tasks = [asyncio.create_task(slow_check(i * 0.001)) for i in range(10)]
        write_task = asyncio.create_task(push_over_threshold())

        await asyncio.gather(*check_tasks, write_task)

        # At least one check must have seen the over-threshold state
        assert any(r is False for r in completed), (
            "Expected at least one check to detect the over-threshold write"
        )
        # None of the reads should have panicked or returned an inconsistent type
        assert all(isinstance(r, bool) for r in completed)

    # ------------------------------------------------------------------
    # Burst routing consistency: get_available_tiers under concurrency
    # ------------------------------------------------------------------

    async def test_20_concurrent_routings_all_return_ollama_when_cloud_saturated(self, tmp_db):
        """When all cloud providers are hot, 20 concurrent get_available_tiers calls
        must all return OLLAMA — not a mix of cloud tiers caused by lock races."""
        mgr = _make_manager(tmp_db, _ALL_PROVIDERS)
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 35200, 35200, now - 1800)
        await _insert_tokens(tmp_db, "codex_cli", 16000, 16000, now - 1800)
        for _ in range(800):
            await _insert_request(tmp_db, "gemini_cli", now - 1800)

        gate = asyncio.Event()

        async def route():
            await gate.wait()
            return await get_available_tiers("code", "complex", mgr)

        tasks = [asyncio.create_task(route()) for _ in range(20)]
        gate.set()
        results = await asyncio.gather(*tasks)

        assert all(r == ModelTier.OLLAMA for r in results), (
            f"Expected all OLLAMA, got: {[r.value for r in results]}"
        )

    async def test_routing_burst_no_exception_no_deadlock(self, tmp_db):
        """100 concurrent get_available_tiers calls complete without deadlock
        or exception under all-cloud-hot conditions."""
        mgr = _make_manager(tmp_db, _ALL_PROVIDERS)
        now = time.time()

        await _insert_tokens(tmp_db, "claude_code", 35200, 35200, now - 1800)
        await _insert_tokens(tmp_db, "codex_cli", 16000, 16000, now - 1800)
        for _ in range(800):
            await _insert_request(tmp_db, "gemini_cli", now - 1800)

        results = await asyncio.gather(
            *[get_available_tiers("code", "complex", mgr) for _ in range(100)],
            return_exceptions=True,
        )

        exceptions = [r for r in results if isinstance(r, Exception)]
        assert not exceptions, f"Unexpected exceptions: {exceptions}"
        assert len(results) == 100
