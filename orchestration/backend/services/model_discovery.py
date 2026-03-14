#  Orchestration Engine - Model Discovery Service
#
#  Queries provider REST APIs to discover available models at startup.
#  Caches results with a configurable TTL (default 1 hour).
#  Also checks CLI tool availability via shutil.which().
#  Falls back to config.json values if any API call fails.
#
#  Depends on: backend/config.py
#  Used by:    services/model_router.py, routes/services.py

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass, field

import httpx

from backend.config import ANTHROPIC_API_KEY, OLLAMA_HOSTS, cfg

logger = logging.getLogger("orchestration.model_discovery")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_DEFAULT_DISCOVERY_TTL = 3600  # 1 hour


@dataclass
class ProviderResult:
    """Discovered models for a single provider."""
    provider: str
    models: list[str]
    available: bool
    cached_at: float = field(default_factory=time.monotonic)
    error: str | None = None


@dataclass
class CliInfo:
    """CLI availability record for a single tool."""
    name: str       # binary name: 'claude', 'gemini', 'codex'
    tier: str       # model tier key: 'claude_code', 'gemini_cli', 'codex_cli'
    available: bool
    path: str | None


# ---------------------------------------------------------------------------
# Config-based fallbacks
# ---------------------------------------------------------------------------

def _config_fallbacks() -> dict[str, list[str]]:
    """Read model IDs from config.json as fallback values when APIs are unreachable."""
    return {
        "anthropic": [
            cfg("anthropic.models.haiku", "claude-haiku-4-5-20251001"),
            cfg("anthropic.models.sonnet", "claude-sonnet-4-6"),
            cfg("anthropic.models.opus", "claude-opus-4-6"),
        ],
        "google": [
            cfg("gemini_cli.model", "gemini-2.5-pro"),
        ],
        "openai": [
            cfg("codex_cli.model", "gpt-4o"),
        ],
        "ollama": [
            cfg("ollama.default_model", "qwen2.5-coder:14b"),
        ],
    }


# ---------------------------------------------------------------------------
# Provider-specific fetch helpers (async)
# ---------------------------------------------------------------------------

async def _fetch_anthropic(client: httpx.AsyncClient) -> ProviderResult:
    api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ProviderResult(
            provider="anthropic",
            models=_config_fallbacks()["anthropic"],
            available=False,
            error="ANTHROPIC_API_KEY not set — using config fallbacks",
        )
    try:
        resp = await client.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        models = [m["id"] for m in data.get("data", []) if m.get("id")]
        logger.info("Anthropic: discovered %d models", len(models))
        return ProviderResult(provider="anthropic", models=models, available=True)
    except Exception as exc:
        logger.warning("Anthropic model discovery failed: %s — using config fallbacks", exc)
        return ProviderResult(
            provider="anthropic",
            models=_config_fallbacks()["anthropic"],
            available=False,
            error=str(exc),
        )


async def _fetch_google(client: httpx.AsyncClient) -> ProviderResult:
    api_key = os.environ.get("GOOGLE_API_KEY", "") or cfg("google.api_key", "")
    if not api_key:
        return ProviderResult(
            provider="google",
            models=_config_fallbacks()["google"],
            available=False,
            error="GOOGLE_API_KEY not set — using config fallbacks",
        )
    try:
        resp = await client.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": api_key},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        # Each entry has "name" like "models/gemini-2.5-pro"; strip the prefix.
        models = []
        for m in data.get("models", []):
            name = m.get("name", "")
            model_id = name.removeprefix("models/") if name else ""
            if model_id:
                models.append(model_id)
        logger.info("Google: discovered %d models", len(models))
        return ProviderResult(provider="google", models=models, available=True)
    except Exception as exc:
        logger.warning("Google model discovery failed: %s — using config fallbacks", exc)
        return ProviderResult(
            provider="google",
            models=_config_fallbacks()["google"],
            available=False,
            error=str(exc),
        )


async def _fetch_openai(client: httpx.AsyncClient) -> ProviderResult:
    api_key = os.environ.get("OPENAI_API_KEY", "") or cfg("openai.api_key", "")
    if not api_key:
        return ProviderResult(
            provider="openai",
            models=_config_fallbacks()["openai"],
            available=False,
            error="OPENAI_API_KEY not set — using config fallbacks",
        )
    try:
        resp = await client.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        models = [m["id"] for m in data.get("data", []) if m.get("id")]
        logger.info("OpenAI: discovered %d models", len(models))
        return ProviderResult(provider="openai", models=models, available=True)
    except Exception as exc:
        logger.warning("OpenAI model discovery failed: %s — using config fallbacks", exc)
        return ProviderResult(
            provider="openai",
            models=_config_fallbacks()["openai"],
            available=False,
            error=str(exc),
        )


async def _fetch_ollama(client: httpx.AsyncClient) -> ProviderResult:
    # Use the first configured Ollama host.
    host_url = next(iter(OLLAMA_HOSTS.values()), "http://localhost:11434")
    url = f"{host_url.rstrip('/')}/api/tags"
    try:
        resp = await client.get(url, timeout=5.0)
        resp.raise_for_status()
        data = resp.json()
        models = [m["name"] for m in data.get("models", []) if m.get("name")]
        logger.info("Ollama: discovered %d models", len(models))
        return ProviderResult(provider="ollama", models=models, available=True)
    except Exception as exc:
        logger.warning("Ollama model discovery failed: %s — using config fallbacks", exc)
        return ProviderResult(
            provider="ollama",
            models=_config_fallbacks()["ollama"],
            available=False,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# CLI availability (sync — runs in executor)
# ---------------------------------------------------------------------------

_CLI_TOOLS = [
    ("claude", "claude_code"),
    ("gemini", "gemini_cli"),
    ("codex",  "codex_cli"),
]


def _check_cli_availability() -> list[CliInfo]:
    """Check which CLI tools are installed. Sync — safe to call from asyncio.to_thread."""
    results = []
    for name, tier in _CLI_TOOLS:
        path = shutil.which(name)
        results.append(CliInfo(name=name, tier=tier, available=path is not None, path=path))
        if path:
            logger.debug("CLI '%s' found at %s", name, path)
        else:
            logger.debug("CLI '%s' not found on PATH", name)
    return results


# ---------------------------------------------------------------------------
# ModelDiscoveryService
# ---------------------------------------------------------------------------

class ModelDiscoveryService:
    """Discovers available models from provider APIs and CLI tools.

    Thread safety: all cache mutations happen on the asyncio event loop.
    Discovery runs concurrently via asyncio.gather for all providers.
    """

    def __init__(self, ttl_seconds: int | None = None):
        self._ttl = ttl_seconds if ttl_seconds is not None else cfg(
            "model_discovery.cache_ttl_seconds", _DEFAULT_DISCOVERY_TTL
        )
        self._provider_cache: dict[str, ProviderResult] = {}
        self._cli_cache: list[CliInfo] = []
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def discover(self) -> None:
        """Run a full discovery cycle: query all provider APIs + check CLIs."""
        if self._http is None:
            self._http = httpx.AsyncClient()
        async with self._lock:
            results = await asyncio.gather(
                _fetch_anthropic(self._http),
                _fetch_google(self._http),
                _fetch_openai(self._http),
                _fetch_ollama(self._http),
                return_exceptions=False,
            )
            for result in results:
                self._provider_cache[result.provider] = result

            self._cli_cache = await asyncio.to_thread(_check_cli_availability)
        logger.info(
            "Discovery complete — providers: %s | CLIs available: %s",
            list(self._provider_cache.keys()),
            [c.name for c in self._cli_cache if c.available],
        )

    def get_available_models(self) -> dict[str, list[str]]:
        """Return discovered models per provider.

        If a provider's cache is missing or stale, returns config fallbacks.
        Config overrides (if set) take precedence over discovered values.
        """
        fallbacks = _config_fallbacks()
        result: dict[str, list[str]] = {}

        for provider in ("anthropic", "google", "openai", "ollama"):
            cached = self._provider_cache.get(provider)
            if cached and self._is_fresh(cached):
                models = cached.models
            else:
                models = fallbacks[provider]

            # Config overrides always win for explicit single-model configs.
            override = self._config_override(provider)
            if override and override not in models:
                models = [override] + models

            result[provider] = models

        return result

    def get_provider_result(self, provider: str) -> ProviderResult | None:
        """Return the raw cached result for a provider (or None if not yet discovered)."""
        return self._provider_cache.get(provider)

    def get_cli_availability(self) -> list[CliInfo]:
        """Return CLI availability records (empty list if discover() not yet called)."""
        return list(self._cli_cache)

    def is_cli_available(self, tier: str) -> bool:
        """Return True if the CLI for the given model tier is installed."""
        for info in self._cli_cache:
            if info.tier == tier:
                return info.available
        return False

    def cache_age_seconds(self, provider: str) -> float | None:
        """Return seconds since last discovery for a provider, or None if not cached."""
        cached = self._provider_cache.get(provider)
        if cached is None:
            return None
        return time.monotonic() - cached.cached_at

    def needs_refresh(self, provider: str) -> bool:
        """True if the provider cache is missing or past TTL."""
        cached = self._provider_cache.get(provider)
        return cached is None or not self._is_fresh(cached)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http:
            await self._http.aclose()
            self._http = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_fresh(self, result: ProviderResult) -> bool:
        return (time.monotonic() - result.cached_at) < self._ttl

    @staticmethod
    def _config_override(provider: str) -> str | None:
        """Return a single-model config override for a provider, if set."""
        overrides = {
            "google": cfg("gemini_cli.model", ""),
            "openai": cfg("codex_cli.model", ""),
        }
        val = overrides.get(provider, "")
        return val if val else None
