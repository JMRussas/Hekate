#  Orchestration Engine - Provider Quota Manager
#
#  Tracks real-time utilization per provider and per model family against
#  configured rate windows. Handles token-based (Claude, Codex) and
#  request-based (Gemini) limits.
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
        self._model_quota_config: dict = cfg("model_quotas", {})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _window_seconds(self, window: dict) -> int:
        """Convert window config to a duration in seconds."""
        if "duration_minutes" in window:
            return int(window["duration_minutes"] * 60)
        return int(window.get("duration_hours", 1) * 3600)

    async def _query_token_usage(self, provider: str, since_ts: float, model_pattern: str | None = None) -> int:
        if model_pattern:
            row = await self._db.fetchone(
                """
                SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total
                FROM usage_log
                WHERE provider = ? AND timestamp >= ? AND model LIKE ?
                """,
                (provider, since_ts, model_pattern),
            )
        else:
            row = await self._db.fetchone(
                """
                SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS total
                FROM usage_log
                WHERE provider = ? AND timestamp >= ?
                """,
                (provider, since_ts),
            )
        return int(row["total"]) if row else 0

    async def _query_request_count(self, provider: str, since_ts: float, model_pattern: str | None = None) -> int:
        if model_pattern:
            row = await self._db.fetchone(
                """
                SELECT COUNT(*) AS total
                FROM usage_log
                WHERE provider = ? AND timestamp >= ? AND model LIKE ?
                """,
                (provider, since_ts, model_pattern),
            )
        else:
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
        model_pattern: str | None = None,
    ) -> WindowStatus:
        """Query the DB and compute utilization for one window config entry.

        Args:
            model_pattern: SQL LIKE pattern to filter by model family
                           (e.g., "%opus%" for all Opus variants).
        """
        duration_sec = self._window_seconds(window)
        since_ts = now - duration_sec
        metric = window.get("metric", "tokens")
        limit = int(window["limit"])

        if metric == "requests":
            used = await self._query_request_count(provider, since_ts, model_pattern)
        else:
            used = await self._query_token_usage(provider, since_ts, model_pattern)

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

    # ------------------------------------------------------------------
    # Per-model-family quotas
    # ------------------------------------------------------------------

    async def is_model_available(self, model_id: str) -> bool:
        """Return True if the model family is below its warn_pct on all windows.

        Checks config.model_quotas for entries whose key matches the model_id.
        Each entry has a 'provider', 'model_pattern' (SQL LIKE), and 'windows'.

        Example config:
            "model_quotas": {
                "opus": {
                    "provider": "claude_code",
                    "model_pattern": "%opus%",
                    "warn_pct": 80,
                    "windows": [
                        {"name": "sliding_5h", "duration_hours": 5, "metric": "tokens", "limit": 220000},
                        {"name": "weekly", "duration_hours": 168, "metric": "tokens", "limit": 1500000}
                    ]
                }
            }

        Unknown models (not in config) are treated as available.
        """
        # Find matching model quota entry
        model_cfg = None
        family_name = None
        for name, cfg_entry in self._model_quota_config.items():
            pattern = cfg_entry.get("model_pattern", "")
            # Convert SQL LIKE pattern to a simple contains check for matching
            search_term = pattern.replace("%", "")
            if search_term and search_term in model_id:
                model_cfg = cfg_entry
                family_name = name
                break

        if not model_cfg:
            return True

        provider = model_cfg.get("provider", "")
        model_pattern = model_cfg.get("model_pattern", "")
        warn_pct = int(model_cfg.get("warn_pct", 80))
        now = time.time()

        async with self._lock:
            for window in model_cfg.get("windows", []):
                ws = await self._evaluate_window(
                    provider, window, warn_pct, now, model_pattern=model_pattern,
                )
                if ws.is_warned:
                    logger.warning(
                        "Model family '%s' window '%s' at %.1f%% (%d/%d %s) — deprioritizing",
                        family_name,
                        ws.name,
                        ws.utilization_pct,
                        ws.used,
                        ws.limit,
                        ws.metric,
                    )
                    return False

        return True

    async def get_model_quota_status(self) -> dict[str, ProviderStatus]:
        """Return current utilization for all configured model families."""
        async with self._lock:
            now = time.time()
            result: dict[str, ProviderStatus] = {}

            for family_name, model_cfg in self._model_quota_config.items():
                provider = model_cfg.get("provider", "")
                model_pattern = model_cfg.get("model_pattern", "")
                warn_pct = int(model_cfg.get("warn_pct", 80))
                windows_cfg: list[dict] = model_cfg.get("windows", [])

                window_statuses: list[WindowStatus] = []
                for window in windows_cfg:
                    ws = await self._evaluate_window(
                        provider, window, warn_pct, now, model_pattern=model_pattern,
                    )
                    window_statuses.append(ws)

                is_warned = any(ws.is_warned for ws in window_statuses)

                result[family_name] = ProviderStatus(
                    provider=family_name,
                    warn_pct=warn_pct,
                    windows=window_statuses,
                    is_warned=is_warned,
                    has_limits=len(windows_cfg) > 0,
                )

            return result
