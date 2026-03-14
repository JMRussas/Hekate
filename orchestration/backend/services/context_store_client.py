#  Orchestration Engine - Context Store Client
#
#  HTTP client for the context store API (C#/.NET, port 5102).
#  Calls POST /api/preview to fetch relevant knowledge nodes for a query.
#  All connectivity/timeout errors are swallowed — callers receive None on failure.
#
#  Depends on: config.py (cfg)
#  Used by:    services/enrichment_service.py

import logging
import time
from dataclasses import dataclass

import httpx

from backend.config import cfg

logger = logging.getLogger("orchestration.context_store")

# Defaults — overridden by config.context_enrichment.*
_DEFAULT_URL = "http://localhost:5102"
_DEFAULT_TIMEOUT = 5.0  # seconds — keep low so a dead context store doesn't stall dispatch


@dataclass
class ContextStoreResult:
    nodes: list[dict]
    latency_ms: float


class ContextStoreClient:
    """Thin async client for the context store preview endpoint.

    Returns None (not raises) on any connectivity or timeout error so the
    enrichment layer can silently skip without affecting task dispatch.
    """

    def __init__(self, base_url: str | None = None, timeout: float = _DEFAULT_TIMEOUT):
        self._base_url = (base_url or cfg("context_enrichment.context_store_url", _DEFAULT_URL)).rstrip("/")
        self._timeout = timeout

    async def preview(self, query: str, max_nodes: int = 20) -> ContextStoreResult | None:
        """Call POST /api/preview and return parsed nodes.

        Args:
            query: Natural language description used for semantic search.
            max_nodes: Upper bound on returned nodes (passed to the API).

        Returns:
            ContextStoreResult on success, None on any error.
        """
        url = f"{self._base_url}/api/preview"
        payload = {"query": query, "maxNodes": max_nodes}

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.TimeoutException:
            logger.debug("Context store timed out (url=%s, query=%r)", url, query[:80])
            return None
        except httpx.ConnectError:
            logger.debug("Context store unreachable (url=%s)", url)
            return None
        except httpx.HTTPStatusError as exc:
            logger.debug("Context store returned %s for preview", exc.response.status_code)
            return None
        except Exception as exc:
            logger.debug("Context store preview failed: %s", exc)
            return None

        latency_ms = (time.monotonic() - t0) * 1000
        nodes = _extract_nodes(data)
        return ContextStoreResult(nodes=nodes, latency_ms=latency_ms)


def _extract_nodes(data: object) -> list[dict]:
    """Normalise the API response into a flat list of node dicts."""
    if isinstance(data, list):
        return [n for n in data if isinstance(n, dict)]
    if isinstance(data, dict):
        for key in ("nodes", "results", "items", "data"):
            if isinstance(data.get(key), list):
                return [n for n in data[key] if isinstance(n, dict)]
    return []
