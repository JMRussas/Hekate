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
    # Project management
    # ------------------------------------------------------------------

    async def ensure_project(self, name: str, root_path: str | None = None) -> str | None:
        """POST /api/projects — get or create a project. Returns project ID or None."""
        if self._is_circuit_open():
            return None

        url = f"{self._base_url}/api/projects"
        payload = {"name": name}
        if root_path:
            payload["rootPath"] = root_path
        try:
            resp = await self._get_client().post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            self._record_success()
            return data.get("id")
        except Exception as exc:
            self._record_failure()
            logger.debug("Context store ensure_project failed: %s", exc)
            return None

    async def create_root_node(self, project_id: str, payload: dict) -> str | None:
        """POST /api/project/{project_id}/nodes — create a root-level node. Returns node ID or None."""
        if self._is_circuit_open():
            return None

        url = f"{self._base_url}/api/project/{project_id}/nodes"
        try:
            resp = await self._get_client().post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            self._record_success()
            return data.get("id")
        except Exception as exc:
            self._record_failure()
            logger.debug("Context store create_root_node failed: %s", exc)
            return None

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
            # Response may be {"node": {"id": ...}} or {"id": ...}
            node = data.get("node", data)
            return node.get("id") or payload.get("id")
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

    async def get_node(self, node_id: str) -> dict | None:
        """GET /api/node/{node_id} — retrieve a single node. Returns node dict or None."""
        if self._is_circuit_open():
            return None

        url = f"{self._base_url}/api/node/{node_id}"
        try:
            resp = await self._get_client().get(url)
            resp.raise_for_status()
            self._record_success()
            return resp.json()
        except Exception as exc:
            self._record_failure()
            logger.debug("Context store get_node failed for %s: %s", node_id, exc)
            return None

    # ------------------------------------------------------------------
    # Revision nodes (Athena Loop — wave reassessment)
    # ------------------------------------------------------------------

    async def create_revision_node(
        self,
        plan_node_id: str,
        *,
        wave_number: int,
        outcome: str,
        rationale: str,
        delta: dict,
        observation_ids: list[str] | None = None,
    ) -> str | None:
        """Create a 'revision' node as a child of the plan, then link it to triggering observations.

        Args:
            plan_node_id: Context store node ID of the plan being revised.
            wave_number: Which wave triggered the reassessment.
            outcome: The reassessment decision (e.g. replan_remaining, escalate_to_human).
            rationale: LLM-generated explanation of why the plan changed.
            delta: Dict describing what changed — added/removed/modified tasks/waves.
            observation_ids: Sentinel observation node IDs that triggered the replanning.

        Returns:
            The revision node ID, or None if the context store is unavailable.
        """
        payload = {
            "nodeType": "revision",
            "name": f"Wave {wave_number} reassessment — {outcome}",
            "value": rationale,
            "attributes": {
                "wave_number": str(wave_number),
                "outcome": outcome,
                "delta": str(delta),
            },
        }

        revision_id = await self.create_node(plan_node_id, payload)
        if revision_id is None:
            return None

        # Link revision → each triggering observation
        for obs_id in (observation_ids or []):
            await self.create_edge({
                "sourceId": revision_id,
                "targetId": obs_id,
                "type": "triggered_by",
            })

        return revision_id

    # ------------------------------------------------------------------
    # Brain services (called by ChatAgent for universal chat)
    # ------------------------------------------------------------------

    async def brain_resolve(self, message: str, conversation_id: str | None = None) -> dict | None:
        """POST /api/brain/resolve — entity resolution. Returns ResolvedSubject or None."""
        if self._is_circuit_open():
            return None
        url = f"{self._base_url}/api/brain/resolve"
        payload: dict = {"message": message}
        if conversation_id:
            payload["conversationId"] = conversation_id
        try:
            resp = await self._get_client().post(url, json=payload, timeout=15.0)
            resp.raise_for_status()
            self._record_success()
            return resp.json()
        except Exception as exc:
            self._record_failure()
            logger.debug("Brain resolve failed: %s", exc)
            return None

    async def brain_assemble(self, resolved_subject: dict, conversation_id: str) -> dict | None:
        """POST /api/brain/assemble — context assembly. Returns SubjectState or None."""
        if self._is_circuit_open():
            return None
        url = f"{self._base_url}/api/brain/assemble"
        payload = {"resolvedSubject": resolved_subject, "conversationId": conversation_id}
        try:
            resp = await self._get_client().post(url, json=payload, timeout=15.0)
            resp.raise_for_status()
            self._record_success()
            return resp.json()
        except Exception as exc:
            self._record_failure()
            logger.debug("Brain assemble failed: %s", exc)
            return None

    async def brain_extract(self, response_text: str, conversation_id: str, thread_id: str) -> dict | None:
        """POST /api/brain/extract — extract ideas/decisions from response. Returns items or None."""
        if self._is_circuit_open():
            return None
        url = f"{self._base_url}/api/brain/extract"
        payload = {"responseText": response_text, "conversationId": conversation_id, "threadId": thread_id}
        try:
            resp = await self._get_client().post(url, json=payload, timeout=30.0)
            resp.raise_for_status()
            self._record_success()
            return resp.json()
        except Exception as exc:
            self._record_failure()
            logger.debug("Brain extract failed: %s", exc)
            return None

    async def brain_store_turn(self, conversation_id: str, speaker: str, content: str, thread_id: str | None = None) -> str | None:
        """POST /api/brain/turn — persist a conversation turn. Returns turn ID or None."""
        if self._is_circuit_open():
            return None
        url = f"{self._base_url}/api/brain/turn"
        payload: dict = {"conversationId": conversation_id, "speaker": speaker, "content": content}
        if thread_id:
            payload["threadId"] = thread_id
        try:
            resp = await self._get_client().post(url, json=payload, timeout=5.0)
            resp.raise_for_status()
            self._record_success()
            data = resp.json()
            return data.get("id")
        except Exception as exc:
            self._record_failure()
            logger.debug("Brain store_turn failed: %s", exc)
            return None

    async def brain_create_conversation(self) -> str | None:
        """POST /api/brain/conversation — create a new conversation. Returns ID or None."""
        if self._is_circuit_open():
            return None
        url = f"{self._base_url}/api/brain/conversation"
        try:
            resp = await self._get_client().post(url)
            resp.raise_for_status()
            self._record_success()
            data = resp.json()
            return data.get("id")
        except Exception as exc:
            self._record_failure()
            logger.debug("Brain create_conversation failed: %s", exc)
            return None

    async def brain_get_conversation(self, conversation_id: str) -> dict | None:
        """GET /api/brain/conversation/{id} — get conversation with turns."""
        if self._is_circuit_open():
            return None
        url = f"{self._base_url}/api/brain/conversation/{conversation_id}"
        try:
            resp = await self._get_client().get(url, timeout=5.0)
            resp.raise_for_status()
            self._record_success()
            return resp.json()
        except Exception as exc:
            self._record_failure()
            logger.debug("Brain get_conversation failed: %s", exc)
            return None

    async def brain_get_permissions(self, conversation_id: str) -> dict | None:
        """GET /api/brain/permissions/{id} — get permission config."""
        if self._is_circuit_open():
            return None
        url = f"{self._base_url}/api/brain/permissions/{conversation_id}"
        try:
            resp = await self._get_client().get(url, timeout=5.0)
            resp.raise_for_status()
            self._record_success()
            return resp.json()
        except Exception as exc:
            self._record_failure()
            logger.debug("Brain get_permissions failed: %s", exc)
            return None

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
