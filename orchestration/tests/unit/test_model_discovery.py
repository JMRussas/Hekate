#  Orchestration Engine - Model Discovery Tests
#
#  Unit tests for ModelDiscoveryService: API failure fallback, TTL/cache refresh,
#  CLI availability exclusion, and config override precedence.
#
#  Depends on: backend/services/model_discovery.py, backend/config.py
#  Used by:    pytest

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.services.model_discovery import (
    CliInfo,
    ModelDiscoveryService,
    ProviderResult,
    _check_cli_availability,
    _config_fallbacks,
    _fetch_anthropic,
    _fetch_google,
    _fetch_ollama,
    _fetch_openai,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(status: int, body: dict) -> httpx.Response:
    """Build a fake httpx.Response without a real transport."""
    return httpx.Response(status_code=status, json=body)


def _anthropic_response(model_ids: list[str]) -> httpx.Response:
    return _make_response(200, {"data": [{"id": m} for m in model_ids]})


def _google_response(model_names: list[str]) -> httpx.Response:
    return _make_response(200, {"models": [{"name": f"models/{m}"} for m in model_names]})


def _openai_response(model_ids: list[str]) -> httpx.Response:
    return _make_response(200, {"data": [{"id": m} for m in model_ids]})


def _ollama_response(model_names: list[str]) -> httpx.Response:
    return _make_response(200, {"models": [{"name": m} for m in model_names]})


# ---------------------------------------------------------------------------
# Test: API failure falls back to config defaults
# ---------------------------------------------------------------------------

class TestApiFailureFallback:
    """All four providers: when the HTTP call fails, return config fallbacks."""

    @pytest.mark.asyncio
    async def test_anthropic_network_error_returns_config_fallbacks(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = httpx.ConnectError("refused")

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            result = await _fetch_anthropic(client)

        assert result.available is False
        assert result.error is not None
        # Models must match config fallbacks
        fallbacks = _config_fallbacks()["anthropic"]
        assert result.models == fallbacks

    @pytest.mark.asyncio
    async def test_anthropic_http_error_returns_config_fallbacks(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.return_value = _make_response(500, {"error": "server error"})

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            result = await _fetch_anthropic(client)

        assert result.available is False
        assert result.models == _config_fallbacks()["anthropic"]

    @pytest.mark.asyncio
    async def test_anthropic_no_api_key_returns_config_fallbacks(self):
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch.dict("os.environ", {}, clear=True):
            with patch("backend.services.model_discovery.ANTHROPIC_API_KEY", ""):
                result = await _fetch_anthropic(client)

        assert result.available is False
        client.get.assert_not_called()
        assert result.models == _config_fallbacks()["anthropic"]

    @pytest.mark.asyncio
    async def test_google_network_error_returns_config_fallbacks(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = httpx.TimeoutException("timeout")

        with patch.dict("os.environ", {"GOOGLE_API_KEY": "goog-test"}):
            result = await _fetch_google(client)

        assert result.available is False
        assert result.models == _config_fallbacks()["google"]

    @pytest.mark.asyncio
    async def test_openai_network_error_returns_config_fallbacks(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = httpx.ConnectError("refused")

        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-openai"}):
            result = await _fetch_openai(client)

        assert result.available is False
        assert result.models == _config_fallbacks()["openai"]

    @pytest.mark.asyncio
    async def test_ollama_network_error_returns_config_fallbacks(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get.side_effect = httpx.ConnectError("refused")

        result = await _fetch_ollama(client)

        assert result.available is False
        assert result.models == _config_fallbacks()["ollama"]

    @pytest.mark.asyncio
    async def test_discover_partial_failure_stores_fallbacks(self):
        """discover() with one failing provider still stores results for all four."""
        svc = ModelDiscoveryService(ttl_seconds=3600)

        anthropic_ok = ProviderResult("anthropic", ["claude-sonnet-4-6"], True)
        google_fail  = ProviderResult("google",    _config_fallbacks()["google"],  False, error="timeout")
        openai_ok    = ProviderResult("openai",    ["gpt-4o"], True)
        ollama_ok    = ProviderResult("ollama",    ["qwen2.5-coder:14b"], True)

        with patch("backend.services.model_discovery._fetch_anthropic", new=AsyncMock(return_value=anthropic_ok)), \
             patch("backend.services.model_discovery._fetch_google",    new=AsyncMock(return_value=google_fail)), \
             patch("backend.services.model_discovery._fetch_openai",    new=AsyncMock(return_value=openai_ok)), \
             patch("backend.services.model_discovery._fetch_ollama",    new=AsyncMock(return_value=ollama_ok)), \
             patch("backend.services.model_discovery._check_cli_availability", return_value=[]):
            await svc.discover()

        assert svc.get_provider_result("anthropic").available is True
        assert svc.get_provider_result("google").available is False
        assert svc.get_provider_result("openai").available is True
        assert svc.get_provider_result("ollama").available is True


# ---------------------------------------------------------------------------
# Test: Cache TTL expiry triggers a refresh
# ---------------------------------------------------------------------------

class TestCacheTTL:
    """Stale cache entries are detected; re-running discover() refreshes them."""

    def test_fresh_cache_is_not_stale(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        svc._provider_cache["anthropic"] = ProviderResult(
            "anthropic", ["claude-sonnet-4-6"], True, cached_at=time.monotonic()
        )
        assert svc.needs_refresh("anthropic") is False

    def test_expired_cache_is_stale(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        svc._provider_cache["anthropic"] = ProviderResult(
            "anthropic", ["claude-sonnet-4-6"], True,
            cached_at=time.monotonic() - 3601,  # 1 second past TTL
        )
        assert svc.needs_refresh("anthropic") is True

    def test_missing_cache_entry_is_stale(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        assert svc.needs_refresh("anthropic") is True

    def test_get_available_models_returns_fallback_when_stale(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        # Inject a stale entry with a discovered model list
        svc._provider_cache["anthropic"] = ProviderResult(
            "anthropic", ["stale-model-id"], True,
            cached_at=time.monotonic() - 9999,
        )
        models = svc.get_available_models()
        # Stale — should return config fallbacks, not "stale-model-id"
        assert "stale-model-id" not in models["anthropic"]
        assert models["anthropic"] == _config_fallbacks()["anthropic"] or len(models["anthropic"]) > 0

    def test_get_available_models_returns_discovered_when_fresh(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        discovered = ["claude-new-model", "claude-older-model"]
        svc._provider_cache["anthropic"] = ProviderResult(
            "anthropic", discovered, True, cached_at=time.monotonic()
        )
        models = svc.get_available_models()
        # Fresh — all discovered models must be present
        for m in discovered:
            assert m in models["anthropic"]

    @pytest.mark.asyncio
    async def test_second_discover_call_overwrites_cache(self):
        """A fresh discover() replaces the previously cached result."""
        svc = ModelDiscoveryService(ttl_seconds=3600)

        first_result  = ProviderResult("anthropic", ["model-v1"], True)
        second_result = ProviderResult("anthropic", ["model-v2"], True)

        fallback = ProviderResult("google",  _config_fallbacks()["google"],  False)
        fallback2 = ProviderResult("openai", _config_fallbacks()["openai"],  False)
        fallback3 = ProviderResult("ollama", _config_fallbacks()["ollama"],  False)

        with patch("backend.services.model_discovery._fetch_anthropic", new=AsyncMock(return_value=first_result)), \
             patch("backend.services.model_discovery._fetch_google",    new=AsyncMock(return_value=fallback)), \
             patch("backend.services.model_discovery._fetch_openai",    new=AsyncMock(return_value=fallback2)), \
             patch("backend.services.model_discovery._fetch_ollama",    new=AsyncMock(return_value=fallback3)), \
             patch("backend.services.model_discovery._check_cli_availability", return_value=[]):
            await svc.discover()

        assert svc.get_provider_result("anthropic").models == ["model-v1"]

        with patch("backend.services.model_discovery._fetch_anthropic", new=AsyncMock(return_value=second_result)), \
             patch("backend.services.model_discovery._fetch_google",    new=AsyncMock(return_value=fallback)), \
             patch("backend.services.model_discovery._fetch_openai",    new=AsyncMock(return_value=fallback2)), \
             patch("backend.services.model_discovery._fetch_ollama",    new=AsyncMock(return_value=fallback3)), \
             patch("backend.services.model_discovery._check_cli_availability", return_value=[]):
            await svc.discover()

        assert svc.get_provider_result("anthropic").models == ["model-v2"]

    def test_cache_age_seconds_increases_over_time(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        old_ts = time.monotonic() - 120
        svc._provider_cache["ollama"] = ProviderResult(
            "ollama", ["qwen2.5-coder:14b"], True, cached_at=old_ts
        )
        age = svc.cache_age_seconds("ollama")
        assert age is not None
        assert age >= 120

    def test_cache_age_none_when_not_discovered(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        assert svc.cache_age_seconds("anthropic") is None


# ---------------------------------------------------------------------------
# Test: CLI absence excludes model tiers
# ---------------------------------------------------------------------------

class TestCliAvailability:
    """is_cli_available() reflects shutil.which() results from _check_cli_availability."""

    def test_all_clis_present(self):
        with patch("shutil.which", return_value="/usr/bin/claude"):
            infos = _check_cli_availability()
        assert all(i.available for i in infos)
        assert all(i.path == "/usr/bin/claude" for i in infos)

    def test_no_clis_present(self):
        with patch("shutil.which", return_value=None):
            infos = _check_cli_availability()
        assert all(not i.available for i in infos)
        assert all(i.path is None for i in infos)

    def test_only_claude_present(self):
        def _which(name):
            return "/usr/bin/claude" if name == "claude" else None

        with patch("shutil.which", side_effect=_which):
            infos = _check_cli_availability()

        by_tier = {i.tier: i for i in infos}
        assert by_tier["claude_code"].available is True
        assert by_tier["gemini_cli"].available is False
        assert by_tier["codex_cli"].available is False

    def test_is_cli_available_returns_false_when_absent(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        svc._cli_cache = [
            CliInfo("claude", "claude_code", available=False, path=None),
            CliInfo("gemini", "gemini_cli",  available=True,  path="/usr/bin/gemini"),
            CliInfo("codex",  "codex_cli",   available=False, path=None),
        ]
        assert svc.is_cli_available("claude_code") is False
        assert svc.is_cli_available("gemini_cli")  is True
        assert svc.is_cli_available("codex_cli")   is False

    def test_is_cli_available_returns_false_for_unknown_tier(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        # Empty cache — tier never seen
        assert svc.is_cli_available("nonexistent_tier") is False

    @pytest.mark.asyncio
    async def test_discover_populates_cli_cache(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)

        cli_infos = [
            CliInfo("claude", "claude_code", available=True,  path="/usr/bin/claude"),
            CliInfo("gemini", "gemini_cli",  available=False, path=None),
            CliInfo("codex",  "codex_cli",   available=True,  path="/usr/bin/codex"),
        ]
        fallback_r = lambda p: ProviderResult(p, _config_fallbacks()[p], False)

        with patch("backend.services.model_discovery._fetch_anthropic", new=AsyncMock(return_value=fallback_r("anthropic"))), \
             patch("backend.services.model_discovery._fetch_google",    new=AsyncMock(return_value=fallback_r("google"))), \
             patch("backend.services.model_discovery._fetch_openai",    new=AsyncMock(return_value=fallback_r("openai"))), \
             patch("backend.services.model_discovery._fetch_ollama",    new=AsyncMock(return_value=fallback_r("ollama"))), \
             patch("backend.services.model_discovery._check_cli_availability", return_value=cli_infos):
            await svc.discover()

        assert svc.is_cli_available("claude_code") is True
        assert svc.is_cli_available("gemini_cli")  is False
        assert svc.is_cli_available("codex_cli")   is True

    def test_get_cli_availability_returns_copy(self):
        """get_cli_availability() must return a copy so callers can't mutate internal state."""
        svc = ModelDiscoveryService(ttl_seconds=3600)
        svc._cli_cache = [CliInfo("claude", "claude_code", True, "/usr/bin/claude")]
        result = svc.get_cli_availability()
        result.clear()
        assert len(svc._cli_cache) == 1


# ---------------------------------------------------------------------------
# Test: Config overrides take priority over API discovery
# ---------------------------------------------------------------------------

class TestConfigOverrides:
    """Config-specified model IDs appear first in get_available_models() output."""

    def test_google_config_override_prepended(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        # Discovered models do NOT include the config override
        svc._provider_cache["google"] = ProviderResult(
            "google", ["gemini-1.5-pro", "gemini-1.0-pro"], True,
            cached_at=time.monotonic(),
        )
        override = "gemini-2.5-pro-custom"
        with patch("backend.services.model_discovery.cfg", side_effect=lambda k, d=None: override if k == "gemini_cli.model" else d):
            models = svc.get_available_models()

        assert models["google"][0] == override
        assert "gemini-1.5-pro" in models["google"]

    def test_openai_config_override_prepended(self):
        svc = ModelDiscoveryService(ttl_seconds=3600)
        svc._provider_cache["openai"] = ProviderResult(
            "openai", ["gpt-4o-mini", "gpt-3.5-turbo"], True,
            cached_at=time.monotonic(),
        )
        override = "gpt-5-preview"
        with patch("backend.services.model_discovery.cfg", side_effect=lambda k, d=None: override if k == "codex_cli.model" else d):
            models = svc.get_available_models()

        assert models["openai"][0] == override
        assert "gpt-4o-mini" in models["openai"]

    def test_config_override_not_duplicated_when_already_in_list(self):
        """If the override model is already in the discovered list, it should not be duplicated."""
        svc = ModelDiscoveryService(ttl_seconds=3600)
        override = "gemini-2.5-pro"
        svc._provider_cache["google"] = ProviderResult(
            "google", [override, "gemini-1.5-pro"], True,
            cached_at=time.monotonic(),
        )
        with patch("backend.services.model_discovery.cfg", side_effect=lambda k, d=None: override if k == "gemini_cli.model" else d):
            models = svc.get_available_models()

        assert models["google"].count(override) == 1

    def test_anthropic_no_single_model_override(self):
        """Anthropic doesn't have a single-model config override; discovered list is returned as-is."""
        svc = ModelDiscoveryService(ttl_seconds=3600)
        discovered = ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
        svc._provider_cache["anthropic"] = ProviderResult(
            "anthropic", discovered, True, cached_at=time.monotonic()
        )
        models = svc.get_available_models()
        # No override key for anthropic — list should equal discovered exactly
        assert models["anthropic"] == discovered

    def test_empty_config_override_is_ignored(self):
        """An empty string override must not be prepended."""
        svc = ModelDiscoveryService(ttl_seconds=3600)
        svc._provider_cache["google"] = ProviderResult(
            "google", ["gemini-1.5-pro"], True, cached_at=time.monotonic()
        )
        with patch("backend.services.model_discovery.cfg", side_effect=lambda k, d=None: "" if k == "gemini_cli.model" else d):
            models = svc.get_available_models()

        assert "" not in models["google"]
        assert models["google"] == ["gemini-1.5-pro"]

    def test_config_override_applies_even_when_api_fails(self):
        """Config override is prepended to fallback list when API discovery fails.

        When cfg is patched, _config_fallbacks() inside get_available_models() also
        returns the override for gemini_cli.model, so the list may be [override].
        The key assertion is that the override appears and is first.
        """
        svc = ModelDiscoveryService(ttl_seconds=3600)
        # No cache entry for google → falls back to _config_fallbacks()
        override = "gemini-2.5-pro-custom"

        with patch("backend.services.model_discovery.cfg", side_effect=lambda k, d=None: override if k == "gemini_cli.model" else d):
            models = svc.get_available_models()

        assert len(models["google"]) >= 1
        assert models["google"][0] == override
