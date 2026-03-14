#  Orchestration Engine - Telemetry Feedback Service
#
#  Pushes task execution outcomes to the context store as knowledge nodes,
#  generates Ollama embeddings for semantic search, and creates graph edges
#  linking outcomes to provider and task_type nodes.
#
#  Depends on: config.py, services/context_store_client.py
#  Used by:    services/task_lifecycle.py

import logging
import time
import uuid

import httpx

from backend.config import (
    OLLAMA_EMBED_MODEL,
    OLLAMA_HOSTS,
    TELEMETRY_FEEDBACK_EMBED_OUTCOMES,
    TELEMETRY_FEEDBACK_ENABLED,
)
from backend.services.context_store_client import ContextStoreClient

logger = logging.getLogger("orchestration.telemetry")

# Context store node type for execution outcomes
_OUTCOME_NODE_TYPE = "execution_outcome"

# Ollama embedding timeout
_EMBED_TIMEOUT = 15.0

# Shared client instance — lazy-initialized on first call
_cs_client: ContextStoreClient | None = None


def _get_client() -> ContextStoreClient:
    """Get or create the shared context store client for telemetry."""
    global _cs_client
    if _cs_client is None:
        from backend.config import TELEMETRY_FEEDBACK_URL
        _cs_client = ContextStoreClient(base_url=TELEMETRY_FEEDBACK_URL)
    return _cs_client


async def push_execution_outcome(
    *,
    task_id: str,
    project_id: str,
    task_title: str,
    task_description: str,
    task_type: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_seconds: float,
    status: str,
    verification_outcome: str | None = None,
    complexity: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> str | None:
    """Push a task execution outcome to the context store.

    Returns the created node ID, or None if telemetry is disabled / store offline.
    Never raises — all failures are logged and swallowed.
    """
    if not TELEMETRY_FEEDBACK_ENABLED:
        return None

    try:
        cs = _get_client()

        # Create outcome node
        node_id = uuid.uuid4().hex[:16]
        payload = {
            "id": node_id,
            "type": _OUTCOME_NODE_TYPE,
            "label": f"Outcome: {task_title[:80]}",
            "attributes": {
                "task_id": task_id,
                "project_id": project_id,
                "task_type": task_type,
                "provider": provider,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "duration_seconds": round(duration_seconds, 2),
                "status": status,
                "verification_outcome": verification_outcome or "none",
                "complexity": complexity or "unknown",
                "recorded_at": time.time(),
            },
        }

        outcome_id = await cs.create_node(project_id, payload)
        if outcome_id is None:
            return None

        # Generate and store embedding
        if TELEMETRY_FEEDBACK_EMBED_OUTCOMES:
            embedding = await _generate_embedding(
                task_description=task_description,
                status=status,
                task_type=task_type,
                provider=provider,
                model=model,
                verification_outcome=verification_outcome,
            )
            if embedding:
                await cs.update_attributes(outcome_id, {"embedding": embedding})

        # Create graph edges
        for edge in [
            {"from_id": outcome_id, "to_id": f"provider:{provider}",
             "relation": "executed_by", "attributes": {"project_id": project_id}},
            {"from_id": outcome_id, "to_id": f"task_type:{task_type}",
             "relation": "has_type", "attributes": {"project_id": project_id}},
        ]:
            await cs.create_edge(edge)

        logger.debug(
            "Telemetry: outcome node %s created for task %s (status=%s)",
            outcome_id, task_id, status,
        )
        return outcome_id

    except Exception as e:
        logger.debug("Telemetry feedback failed for task %s: %s", task_id, e)
        return None


async def _generate_embedding(
    *,
    task_description: str,
    status: str,
    task_type: str,
    provider: str,
    model: str,
    verification_outcome: str | None,
) -> list[float] | None:
    """Generate embedding for the outcome via Ollama nomic-embed-text."""
    text = (
        f"Task type: {task_type}. "
        f"Provider: {provider}. Model: {model}. "
        f"Status: {status}. Verification: {verification_outcome or 'none'}. "
        f"Description: {task_description[:500]}"
    )

    ollama_url = next(iter(OLLAMA_HOSTS.values()), "http://localhost:11434")

    try:
        async with httpx.AsyncClient(timeout=_EMBED_TIMEOUT) as client:
            resp = await client.post(
                f"{ollama_url}/api/embeddings",
                json={"model": OLLAMA_EMBED_MODEL, "prompt": text},
            )
            resp.raise_for_status()
            data = resp.json()
            embedding = data.get("embedding")
            if isinstance(embedding, list) and embedding:
                return embedding
            return None
    except Exception as e:
        logger.debug("Embedding generation failed: %s", e)
        return None
