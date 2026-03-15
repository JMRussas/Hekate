#  Sentinel Context Client
#
#  Wraps backend.services.context_store_client to provide sentinel-specific
#  methods for persisting observations and searching past incidents.
#  Reuses the shared circuit breaker so sentinel writes degrade gracefully
#  when the context store is down.
#
#  Depends on: backend/services/context_store_client.py, sentinel/models.py
#  Used by:    sentinel monitors, intervention handlers

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone

from backend.services.context_store_client import ContextStoreClient, ContextStoreResult
from backend.services.sentinel.models import SentinelObservation

logger = logging.getLogger(__name__)

# Node type constant — must match context-store/ContextRouter/NodeTypes.cs
SENTINEL_OBSERVATION_TYPE = "sentinel_observation"

# Edge type constants — must match context-store/GraphLayer/EdgeTypes.cs
EDGE_OBSERVED_BY = "observed_by"
EDGE_RESOLVED_BY = "resolved_by"
EDGE_ESCALATED_TO = "escalated_to"


class SentinelContextClient:
    """Sentinel-specific wrapper around the shared ContextStoreClient.

    Reuses the underlying circuit breaker and HTTP client.  All methods
    return None/False on failure so callers can proceed without blocking.
    """

    def __init__(self, client: ContextStoreClient | None = None):
        self._client = client or ContextStoreClient()

    async def close(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------------
    # Persist observations
    # ------------------------------------------------------------------

    async def save_observation(
        self,
        observation: SentinelObservation,
        parent_id: str | None = None,
    ) -> str | None:
        """Persist a SentinelObservation as a context store node.

        Args:
            observation: The observation to persist.
            parent_id: Parent node ID (e.g. the project node).  Falls back
                       to the observation's project_id.

        Returns:
            The created node ID, or None if the store is unavailable.
        """
        target_parent = parent_id or observation.project_id
        if not target_parent:
            logger.warning("Cannot save observation without a parent/project ID")
            return None

        payload = {
            "id": observation.observation_id,
            "type": SENTINEL_OBSERVATION_TYPE,
            "title": f"[{observation.severity.value}] {observation.category}: {observation.message[:120]}",
            "attributes": _observation_to_attributes(observation),
        }

        node_id = await self._client.create_node(target_parent, payload)
        if node_id:
            logger.debug("Saved sentinel observation %s", node_id)
        return node_id

    async def link_observation_to_task(
        self,
        observation_id: str,
        task_id: str,
    ) -> bool:
        """Create an observed_by edge from a task to this observation."""
        return await self._client.create_edge({
            "sourceId": task_id,
            "targetId": observation_id,
            "edgeType": EDGE_OBSERVED_BY,
        })

    async def link_resolved_by(
        self,
        observation_id: str,
        resolver_id: str,
    ) -> bool:
        """Create a resolved_by edge from an observation to its resolver."""
        return await self._client.create_edge({
            "sourceId": observation_id,
            "targetId": resolver_id,
            "edgeType": EDGE_RESOLVED_BY,
        })

    async def link_escalated_to(
        self,
        observation_id: str,
        target_id: str,
    ) -> bool:
        """Create an escalated_to edge from an observation to an escalation target."""
        return await self._client.create_edge({
            "sourceId": observation_id,
            "targetId": target_id,
            "edgeType": EDGE_ESCALATED_TO,
        })

    # ------------------------------------------------------------------
    # Search past incidents
    # ------------------------------------------------------------------

    async def search_similar_incidents(
        self,
        query: str,
        max_results: int = 10,
    ) -> ContextStoreResult | None:
        """Semantic search for past sentinel observations.

        Uses the context store's preview endpoint (pgvector similarity)
        to find observations that match the query text.  Callers can use
        this to detect recurring patterns or find prior resolutions.
        """
        result = await self._client.preview(query, max_nodes=max_results)
        if result is None:
            return None

        # Filter to sentinel observation nodes only
        sentinel_nodes = [
            n for n in result.nodes
            if n.get("type") == SENTINEL_OBSERVATION_TYPE
        ]
        return ContextStoreResult(nodes=sentinel_nodes, latency_ms=result.latency_ms)

    async def search_similar_observations(
        self,
        observation: SentinelObservation,
        max_results: int = 5,
    ) -> list[dict]:
        """Fetch the most similar past observations for reasoning context.

        Builds a semantic query from the observation's category, message,
        and severity, then filters to sentinel_observation nodes excluding
        the current observation.

        Returns:
            A list of up to *max_results* observation node dicts, or an
            empty list if the context store is unavailable.
        """
        query = (
            f"{observation.severity.value} {observation.category}: "
            f"{observation.message}"
        )
        # Request extra nodes to account for filtering out the current observation
        result = await self._client.preview(query, max_nodes=max_results + 5)
        if result is None:
            return []

        similar: list[dict] = []
        for node in result.nodes:
            if node.get("type") != SENTINEL_OBSERVATION_TYPE:
                continue
            # Exclude the observation we're searching for
            node_id = node.get("id") or node.get("attributes", {}).get("observation_id")
            if node_id == observation.observation_id:
                continue
            similar.append(node)
            if len(similar) >= max_results:
                break
        return similar

    async def update_observation(
        self,
        observation_id: str,
        attributes: dict,
    ) -> bool:
        """Update attributes on an existing observation node."""
        return await self._client.update_attributes(observation_id, attributes)


def _observation_to_attributes(obs: SentinelObservation) -> dict:
    """Convert a SentinelObservation to a flat attribute dict for storage."""
    data = asdict(obs)
    # Serialize enums to their string values
    data["severity"] = obs.severity.value
    if obs.intervention:
        data["intervention"] = obs.intervention.value
    # Ensure timestamp is ISO string
    if isinstance(data.get("timestamp"), datetime):
        data["timestamp"] = data["timestamp"].isoformat()
    return data
