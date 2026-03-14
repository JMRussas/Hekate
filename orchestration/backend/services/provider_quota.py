#  Orchestration Engine - Provider Quota Manager
#
#  Tracks real-time utilization per provider against configured rate windows.
#  Handles both token-based (Claude, Codex) and request-based (Gemini) limits.
#
#  Depends on: backend/db/connection.py, backend/config.py
#  Used by:    backend/container.py, services/executor.py, routes/usage.py

import asyncio
import logging
import time
from dataclasses import dataclass, field

from backend.config import cfg
from backend.db.connection import Database

logger = logging.getLogger("orchestration.provider_quota")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WindowStatus:
    """Utilization snapshot for a single rate-limit window."""

    name: str
    metric: str           # "tokens" or "requests"
    used: int
    limit: int
    utilization_pct: float
    warn_pct: int
    is_warned: bool       # True when utilization_pct >= warn_pct
    duration_seconds: int


@dataclass
class ProviderStatus:
    """Aggregated quota status for a single provider."""

    provider: str
    warn_pct: int
    windows: list[WindowStatus] = field(default_factory=list)
    is_warned: bool = False   # True if any window is at or above warn_pct
    has_limits: bool = True   # False for Ollama (no configured windows)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "warn_pct": self.warn_pct,
            "is_warned": self.is_warned,
            "has_limits": self.has_limits,
            "windows": [
                {
                    "name": w.name,
                    "metric": w.metric,
                    "used": w.used,
                    "limit": w.limit,
                    "utilization_pct": w.utilization_pct,
                    "warn_pct": w.warn_pct,
                    "is_warned": w.is_warned,
                    "duration_seconds": w.duration_seconds,
                }
                for w in self.windows
            ],
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ProviderQuotaManager:
    """Calculates real-time provider utilization from the usage_log table.

    Token-based providers (claude_code, codex_cli):
        Sums prompt_tokens + completion_tokens within each sliding window.

    Request-based providers (gemini_cli):
        Counts rows within the window (each row = one API call).

    Providers with no windows (ollama):
        Always reported as available with has_limits=False.

    Thread safety:
        asyncio.Lock serializes concurrent quota reads so parallel task
        dispatches don't issue redundant overlapping DB queries.
    """

    def __init__(self, db: Database):
        self._db = db
        self._lock = asyncio.Lock()
        self._quota_config: dict = cfg("provider_quotas", {})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _window_seconds(self, window: dict) -> int:
        """Convert window config to a duration in seconds."""
        if "duration_minutes" in window:
            return int(window["duration_minutes"] * 60)
        return int(window.get("duration_hours", 1) * 3600)

    async def _query_token_usage(self, provider: str, since_ts: float) -> int:
        row = await self._db.fetchone(
            """
            SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total
            FROM usage_log
            WHERE provider = ? AND timestamp >= ?
            """,
            (provider, since_ts),
        )
        return int(row["total"]) if row else 0

    async def _query_request_count(self, provider: str, since_ts: float) -> int:
        row = await self._db.fetchone(
            """
            SELECT COUNT(*) AS total
            FROM usage_log
            WHERE provider = ? AND timestamp >= ?
            """,
            (provider, since_ts),
        )
        return int(row["total"]) if row else 0

    async def _evaluate_window(
        self,
        provider: str,
        window: dict,
        warn_pct: int,
        now: float,
    ) -> WindowStatus:
        """Query the DB and compute utilization for one window config entry."""
        duration_sec = self._window_seconds(window)
        since_ts = now - duration_sec
        metric = window.get("metric", "tokens")
        limit = int(window["limit"])

        if metric == "requests":
            used = await self._query_request_count(provider, since_ts)
        else:
            used = await self._query_token_usage(provider, since_ts)

        utilization_pct = (used / limit * 100.0) if limit > 0 else 0.0

        return WindowStatus(
            name=window["name"],
            metric=metric,
            used=used,
            limit=limit,
            utilization_pct=round(utilization_pct, 1),
            warn_pct=warn_pct,
            is_warned=utilization_pct >= warn_pct,
            duration_seconds=duration_sec,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_quota_status(self) -> dict[str, ProviderStatus]:
        """Return current utilization for all configured providers.

        Re-queries the DB on every call — no caching.  Callers that need
        repeated reads in a tight loop should debounce externally.

        Returns:
            Dict keyed by provider name (e.g. "claude_code", "gemini_cli").
        """
        async with self._lock:
            now = time.time()
            result: dict[str, ProviderStatus] = {}

            for provider, provider_cfg in self._quota_config.items():
                warn_pct = int(provider_cfg.get("warn_pct", 80))
                windows_cfg: list[dict] = provider_cfg.get("windows", [])

                window_statuses: list[WindowStatus] = []
                for window in windows_cfg:
                    ws = await self._evaluate_window(provider, window, warn_pct, now)
                    window_statuses.append(ws)

                is_warned = any(ws.is_warned for ws in window_statuses)

                result[provider] = ProviderStatus(
                    provider=provider,
                    warn_pct=warn_pct,
                    windows=window_statuses,
                    is_warned=is_warned,
                    has_limits=len(windows_cfg) > 0,
                )

            return result

    async def is_provider_available(self, provider: str) -> bool:
        """Return True if the provider is below its warn_pct on all windows.

        Used by the model router to deprioritize hot providers.
        Unknown providers (not in config) are treated as available.
        """
        provider_cfg = self._quota_config.get(provider)
        if not provider_cfg:
            return True

        warn_pct = int(provider_cfg.get("warn_pct", 80))
        now = time.time()

        async with self._lock:
            for window in provider_cfg.get("windows", []):
                ws = await self._evaluate_window(provider, window, warn_pct, now)
                if ws.is_warned:
                    logger.warning(
                        "Provider %s window '%s' at %.1f%% (%d/%d %s) — deprioritizing",
                        provider,
                        ws.name,
                        ws.utilization_pct,
                        ws.used,
                        ws.limit,
                        ws.metric,
                    )
                    return False

        return True
