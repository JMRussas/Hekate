#  Orchestration Engine - Context Store Client
#
#  Shared resilient HTTP client for the context store API (C#/.NET, port 5102).
#  Used by enrichment_service.py (preview endpoint) and telemetry_feedback.py
#  (node creation, graph edges, embeddings).
#
#  All connectivity/timeout errors are swallowed — callers receive None on failure.
#  A single httpx.AsyncClient is reused across calls to avoid per-request overhead.
#
#  Depends on: config.py (cfg)
#  Used by:    services/enrichment_service.py, services/telemetry_feedback.py

import logging
import time
from dataclasses import dataclass

import httpx

from backend.config import cfg

logger = logging.getLogger("orchestration.context_store")

# Defaults — overridden by config
_DEFAULT_URL = "http://localhost:5102"
_DEFAULT_TIMEOUT = 5.0  # seconds — keep low so a dead store doesn't stall dispatch


@dataclass
class ContextStoreResult:
    nodes: list[dict]
    latency_ms: float


class ContextStoreClient:
    """Shared async client for the context store.

    Reuses a single httpx.AsyncClient across all callers. Returns None
    (not raises) on any connectivity or timeout error so callers can
    silently skip without affecting task dispatch.
    """

    def __init__(self, base_url: str | None = None, timeout: float = _DEFAULT_TIMEOUT):
        self._base_url = (base_url or cfg("context_enrichment.context_store_url", _DEFAULT_URL)).rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init the shared httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Enrichment: preview endpoint
    # ------------------------------------------------------------------

    async def preview(self, query: str, max_nodes: int = 20) -> ContextStoreResult | None:
        """Call POST /api/preview and return parsed nodes."""
        url = f"{self._base_url}/api/preview"
        payload = {"query": query, "maxNodes": max_nodes}

        t0 = time.monotonic()
        try:
            resp = await self._get_client().post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.debug("Context store unavailable for preview: %s", type(exc).__name__)
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

    # ------------------------------------------------------------------
    # Telemetry: node creation, embeddings, graph edges
    # ------------------------------------------------------------------

    async def create_node(self, parent_id: str, payload: dict) -> str | None:
        """POST /api/node/{parent_id}/children — create a child node. Returns node ID or None."""
        url = f"{self._base_url}/api/node/{parent_id}/children"
        try:
            resp = await self._get_client().post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("id") or payload.get("id")
        except (httpx.TimeoutException, httpx.ConnectError):
            logger.debug("Context store unavailable for create_node")
            return None
        except httpx.HTTPStatusError as exc:
            logger.debug("Context store returned %s for create_node", exc.response.status_code)
            return None
        except Exception as exc:
            logger.debug("Context store create_node failed: %s", exc)
            return None

    async def update_attributes(self, node_id: str, attributes: dict) -> bool:
        """PUT /api/node/{node_id}/attributes — update node attributes."""
        url = f"{self._base_url}/api/node/{node_id}/attributes"
        try:
            resp = await self._get_client().put(url, json=attributes)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.debug("Context store update_attributes failed for %s: %s", node_id, exc)
            return False

    async def create_edge(self, edge: dict) -> bool:
        """POST /api/graph/edges — create a graph edge."""
        url = f"{self._base_url}/api/graph/edges"
        try:
            resp = await self._get_client().post(url, json=edge)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.debug("Context store create_edge failed: %s", exc)
            return False


def _extract_nodes(data: object) -> list[dict]:
    """Normalise the API response into a flat list of node dicts."""
    if isinstance(data, list):
        return [n for n in data if isinstance(n, dict)]
    if isinstance(data, dict):
        for key in ("nodes", "results", "items", "data"):
            if isinstance(data.get(key), list):
                return [n for n in data[key] if isinstance(n, dict)]
    return []
