#  Orchestration Engine - Context Store Client
#
#  Shared resilient HTTP client for the context store API (C#/.NET, port 5102).
#  Used by enrichment_service.py (preview endpoint) and telemetry_feedback.py
#  (node creation, graph edges, embeddings).
#
#  Circuit breaker: after FAILURE_THRESHOLD consecutive failures, the client
#  "opens" and short-circuits all requests for RECOVERY_WINDOW seconds.
#  This prevents hundreds of debug-log lines per project run when the
#  context store is down, and avoids hammering a dead service.
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

# Circuit breaker settings
FAILURE_THRESHOLD = int(cfg("context_store.circuit_breaker.failure_threshold", 5))
RECOVERY_WINDOW = float(cfg("context_store.circuit_breaker.recovery_seconds", 60))


@dataclass
class ContextStoreResult:
    nodes: list[dict]
    latency_ms: float


class ContextStoreClient:
    """Shared async client for the context store with circuit breaker.

    Reuses a single httpx.AsyncClient across all callers. Returns None
    (not raises) on any connectivity or timeout error so callers can
    silently skip without affecting task dispatch.

    Circuit breaker: after N consecutive failures, all requests are
    short-circuited for a recovery window. A single success resets
    the breaker. This prevents log spam and wasted I/O when the
    context store is down.
    """

    def __init__(self, base_url: str | None = None, timeout: float = _DEFAULT_TIMEOUT):
        self._base_url = (base_url or cfg("context_enrichment.context_store_url", _DEFAULT_URL)).rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None
        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0  # monotonic timestamp

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init the shared httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _is_circuit_open(self) -> bool:
        """Check if the circuit breaker is open (requests should be skipped)."""
        if self._circuit_open_until > 0 and time.monotonic() < self._circuit_open_until:
            return True
        if self._circuit_open_until > 0 and time.monotonic() >= self._circuit_open_until:
            # Recovery window elapsed — allow a probe request
            self._circuit_open_until = 0.0
            logger.info("Context store circuit breaker half-open — allowing probe request")
        return False

    def _record_success(self) -> None:
        """Reset failure counter on success."""
        if self._consecutive_failures > 0:
            logger.info("Context store recovered after %d failures", self._consecutive_failures)
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _record_failure(self) -> None:
        """Increment failure counter; open circuit if threshold reached."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= FAILURE_THRESHOLD:
            self._circuit_open_until = time.monotonic() + RECOVERY_WINDOW
            logger.warning(
                "Context store circuit breaker OPEN — %d consecutive failures, "
                "skipping requests for %.0fs",
                self._consecutive_failures, RECOVERY_WINDOW,
            )

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
        if self._is_circuit_open():
            return None

        url = f"{self._base_url}/api/preview"
        payload = {"query": query, "maxNodes": max_nodes}

        t0 = time.monotonic()
        try:
            resp = await self._get_client().post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            self._record_failure()
            logger.debug("Context store unavailable for preview: %s", type(exc).__name__)
            return None
        except httpx.HTTPStatusError as exc:
            self._record_failure()
            logger.debug("Context store returned %s for preview", exc.response.status_code)
            return None
        except Exception as exc:
            self._record_failure()
            logger.debug("Context store preview failed: %s", exc)
            return None

        self._record_success()
        latency_ms = (time.monotonic() - t0) * 1000
        nodes = _extract_nodes(data)
        return ContextStoreResult(nodes=nodes, latency_ms=latency_ms)

    # ------------------------------------------------------------------
    # Telemetry: node creation, embeddings, graph edges
    # ------------------------------------------------------------------

    async def create_node(self, parent_id: str, payload: dict) -> str | None:
        """POST /api/node/{parent_id}/children — create a child node. Returns node ID or None."""
        if self._is_circuit_open():
            return None

        url = f"{self._base_url}/api/node/{parent_id}/children"
        try:
            resp = await self._get_client().post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            self._record_success()
            return data.get("id") or payload.get("id")
        except (httpx.TimeoutException, httpx.ConnectError):
            self._record_failure()
            return None
        except httpx.HTTPStatusError as exc:
            self._record_failure()
            logger.debug("Context store returned %s for create_node", exc.response.status_code)
            return None
        except Exception as exc:
            self._record_failure()
            logger.debug("Context store create_node failed: %s", exc)
            return None

    async def update_attributes(self, node_id: str, attributes: dict) -> bool:
        """PUT /api/node/{node_id}/attributes — update node attributes."""
        if self._is_circuit_open():
            return False

        url = f"{self._base_url}/api/node/{node_id}/attributes"
        try:
            resp = await self._get_client().put(url, json=attributes)
            resp.raise_for_status()
            self._record_success()
            return True
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            self._record_failure()
            logger.debug("Context store unavailable for update_attributes: %s", type(exc).__name__)
            return False
        except httpx.HTTPStatusError as exc:
            self._record_failure()
            logger.debug("Context store returned %s for update_attributes", exc.response.status_code)
            return False
        except Exception as exc:
            self._record_failure()
            logger.debug("Context store update_attributes failed for %s: %s", node_id, exc)
            return False

    async def create_edge(self, edge: dict) -> bool:
        """POST /api/graph/edges — create a graph edge."""
        if self._is_circuit_open():
            return False

        url = f"{self._base_url}/api/graph/edges"
        try:
            resp = await self._get_client().post(url, json=edge)
            resp.raise_for_status()
            self._record_success()
            return True
        except Exception as exc:
            self._record_failure()
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
