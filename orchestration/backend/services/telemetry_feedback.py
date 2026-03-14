#  Orchestration Engine - Telemetry Feedback Service
#
#  Pushes task execution outcomes to the context store as knowledge nodes,
#  generates Ollama embeddings for semantic search, and creates graph edges
#  linking outcomes to provider and task_type nodes.
#
#  Depends on: config.py (TELEMETRY_FEEDBACK_ENABLED, TELEMETRY_FEEDBACK_URL,
#              TELEMETRY_FEEDBACK_EMBED_OUTCOMES, OLLAMA_HOSTS, OLLAMA_EMBED_MODEL)
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
    TELEMETRY_FEEDBACK_URL,
)

logger = logging.getLogger("orchestration.telemetry")

# Context store node type for execution outcomes
_OUTCOME_NODE_TYPE = "execution_outcome"

# Timeout for context store requests (non-critical path — keep tight)
_CS_TIMEOUT = 5.0

# Ollama embedding timeout
_EMBED_TIMEOUT = 15.0


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

    Creates an outcome node under the project node, optionally generates an
    embedding for semantic search, and creates graph edges to provider/task_type
    nodes.

    Returns the created node ID, or None if telemetry is disabled / store offline.
    Never raises — all failures are logged and swallowed.
    """
    if not TELEMETRY_FEEDBACK_ENABLED:
        return None

    try:
        return await _push_outcome(
            task_id=task_id,
            project_id=project_id,
            task_title=task_title,
            task_description=task_description,
            task_type=task_type,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_seconds=duration_seconds,
            status=status,
            verification_outcome=verification_outcome,
            complexity=complexity,
            http_client=http_client,
        )
    except Exception as e:
        logger.debug("Telemetry feedback failed for task %s: %s", task_id, e)
        return None


async def _push_outcome(
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
    verification_outcome: str | None,
    complexity: str | None,
    http_client: httpx.AsyncClient | None,
) -> str | None:
    """Internal implementation — raises on failure."""
    own_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=_CS_TIMEOUT)

    try:
        outcome_id = await _create_outcome_node(
            client=client,
            project_id=project_id,
            task_id=task_id,
            task_title=task_title,
            task_type=task_type,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_seconds=duration_seconds,
            status=status,
            verification_outcome=verification_outcome,
            complexity=complexity,
        )

        if outcome_id is None:
            return None

        if TELEMETRY_FEEDBACK_EMBED_OUTCOMES:
            embedding = await _generate_embedding(
                client=client,
                task_description=task_description,
                status=status,
                task_type=task_type,
                provider=provider,
                model=model,
                verification_outcome=verification_outcome,
            )
            if embedding:
                await _store_embedding(client, outcome_id, embedding)

        await _create_graph_edges(
            client=client,
            outcome_id=outcome_id,
            project_id=project_id,
            provider=provider,
            task_type=task_type,
        )

        logger.debug(
            "Telemetry: outcome node %s created for task %s (status=%s)",
            outcome_id, task_id, status,
        )
        return outcome_id

    finally:
        if own_client:
            await client.aclose()


async def _create_outcome_node(
    *,
    client: httpx.AsyncClient,
    project_id: str,
    task_id: str,
    task_title: str,
    task_type: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    duration_seconds: float,
    status: str,
    verification_outcome: str | None,
    complexity: str | None,
) -> str | None:
    """POST /api/node/{project_id}/children — create outcome node under project."""
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

    try:
        url = f"{TELEMETRY_FEEDBACK_URL.rstrip('/')}/api/node/{project_id}/children"
        resp = await client.post(url, json=payload, timeout=_CS_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data.get("id") or node_id
    except httpx.ConnectError:
        logger.debug("Context store unavailable — skipping telemetry for task %s", task_id)
        return None
    except httpx.TimeoutException:
        logger.debug("Context store timed out — skipping telemetry for task %s", task_id)
        return None
    except httpx.HTTPStatusError as e:
        logger.debug("Context store returned %s for outcome node: %s", e.response.status_code, e)
        return None


async def _generate_embedding(
    *,
    client: httpx.AsyncClient,
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
        resp = await client.post(
            f"{ollama_url}/api/embeddings",
            json={"model": OLLAMA_EMBED_MODEL, "prompt": text},
            timeout=_EMBED_TIMEOUT,
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


async def _store_embedding(
    client: httpx.AsyncClient,
    node_id: str,
    embedding: list[float],
) -> None:
    """PUT /api/node/{id}/attributes — attach embedding vector to outcome node."""
    try:
        url = f"{TELEMETRY_FEEDBACK_URL.rstrip('/')}/api/node/{node_id}/attributes"
        resp = await client.put(
            url,
            json={"embedding": embedding},
            timeout=_CS_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as e:
        logger.debug("Failed to store embedding for node %s: %s", node_id, e)


async def _create_graph_edges(
    *,
    client: httpx.AsyncClient,
    outcome_id: str,
    project_id: str,
    provider: str,
    task_type: str,
) -> None:
    """Create graph edges: outcome → provider node and outcome → task_type node.

    These edges enable temporal queries such as:
    'average token cost for code tasks on claude_code this week'
    """
    edges = [
        {
            "from_id": outcome_id,
            "to_id": f"provider:{provider}",
            "relation": "executed_by",
            "attributes": {"project_id": project_id},
        },
        {
            "from_id": outcome_id,
            "to_id": f"task_type:{task_type}",
            "relation": "has_type",
            "attributes": {"project_id": project_id},
        },
    ]

    url = f"{TELEMETRY_FEEDBACK_URL.rstrip('/')}/api/graph/edges"
    for edge in edges:
        try:
            resp = await client.post(url, json=edge, timeout=_CS_TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.debug("Failed to create graph edge %s→%s: %s", edge["from_id"], edge["to_id"], e)
