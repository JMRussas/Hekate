#  Orchestration Engine - Ollama Agent
#
#  Runs a single task via local Ollama (free inference).
#  Extracted from executor.py for modularity.
#
#  Depends on: config.py, services/budget.py
#  Used by:    services/task_lifecycle.py

import json
import logging

import httpx

from backend.config import OLLAMA_DEFAULT_MODEL, OLLAMA_GENERATE_TIMEOUT, OLLAMA_HOSTS

logger = logging.getLogger("orchestration.executor")


async def run_ollama_task(*, task_row, http_client, budget) -> dict:
    """Execute a task via local Ollama (free).

    Args:
        task_row: Task database row.
        http_client: Shared httpx.AsyncClient (or None to create a temporary one).
        budget: BudgetManager instance.
    """
    model = OLLAMA_DEFAULT_MODEL
    host_url = OLLAMA_HOSTS.get("local", "http://localhost:11434")

    # Build context
    context = json.loads(task_row["context_json"]) if task_row["context_json"] else []
    system_parts = [task_row["system_prompt"] or "You are a focused task executor."]
    for ctx in context:
        ctx_type = ctx.get("type", "context")
        system_parts.append(f"\n<{ctx_type}>\n{ctx.get('content', '')}\n</{ctx_type}>")
    system_prompt = "\n".join(system_parts)

    body = {
        "model": model,
        "prompt": task_row["description"],
        "system": system_prompt,
        "stream": False,
    }

    client = http_client or httpx.AsyncClient(timeout=OLLAMA_GENERATE_TIMEOUT)
    try:
        resp = await client.post(
            f"{host_url}/api/generate", json=body, timeout=OLLAMA_GENERATE_TIMEOUT
        )
        resp.raise_for_status()
    finally:
        if not http_client:
            await client.aclose()
    data = resp.json()

    output = data.get("response", "")
    # Ollama provides token counts in some versions
    prompt_tokens = data.get("prompt_eval_count", 0)
    completion_tokens = data.get("eval_count", 0)

    # Record usage (cost = 0 for Ollama)
    await budget.record_spend(
        cost_usd=0.0,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        provider="ollama",
        model=model,
        purpose="execution",
        project_id=task_row["project_id"],
        task_id=task_row["id"],
    )

    return {
        "output": output,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": 0.0,
        "model_used": model,
    }
