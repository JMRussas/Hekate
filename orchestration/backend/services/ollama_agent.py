#  Orchestration Engine - Ollama Agent
#
#  Runs a single task via local Ollama (free inference).
#  Supports multi-turn tool calling via /api/chat for models that support it
#  (qwen3.5, llama3.1+, mistral-nemo, etc.).
#  Falls back to plain /api/generate when no tools are available.
#
#  Depends on: config.py, services/budget.py, tools/registry.py
#  Used by:    services/task_lifecycle.py

import json
import logging
import re

import httpx

from backend.config import (
    OLLAMA_DEFAULT_MODEL,
    OLLAMA_GENERATE_TIMEOUT,
    OLLAMA_HOSTS,
    OLLAMA_MAX_TOOL_ROUNDS,
)
from backend.services.prompt_renderer import (
    ContextEntry,
    ContextType,
    PromptSpec,
    render_prompt,
)
from backend.services.cli_common import _map_context_type

logger = logging.getLogger("orchestration.executor")


async def run_ollama_task(*, task_row, http_client, budget, tool_registry=None) -> dict:
    """Execute a task via local Ollama (free).

    When tool_registry is provided and the task has tools assigned, uses
    /api/chat with function calling. Otherwise falls back to /api/generate.

    Args:
        task_row: Task database row.
        http_client: Shared httpx.AsyncClient (or None to create a temporary one).
        budget: BudgetManager instance.
        tool_registry: ToolRegistry instance (optional). Enables tool calling.
    """
    model = OLLAMA_DEFAULT_MODEL
    host_url = OLLAMA_HOSTS.get("local", "http://localhost:11434")

    # Build context via PromptSpec → OllamaRenderer
    context = json.loads(task_row["context_json"]) if task_row["context_json"] else []
    context_entries: list[ContextEntry] = []
    for ctx in context:
        ctx_type = ctx.get("type", "context")
        content = ctx.get("content", "")
        if content:
            sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", ctx_type)
            context_entries.append(ContextEntry(
                type=_map_context_type(sanitized),
                tag=sanitized,
                content=content,
            ))

    spec = PromptSpec(
        role="task_executor",
        identity=task_row["system_prompt"] or "You are a focused task executor.",
        task_description=task_row["description"],
        context=context_entries,
        task_type=task_row.get("task_type", "") or "",
    )
    rendered = render_prompt(spec, "ollama")
    system_prompt = rendered.system_prompt

    # Resolve tools
    tool_names = json.loads(task_row["tools_json"]) if task_row.get("tools_json") else []
    tools = tool_registry.get_many(tool_names) if tool_registry and tool_names else []
    tool_map = {t.name: t for t in tools}
    tool_defs = [t.to_ollama_tool() for t in tools]

    client = http_client or httpx.AsyncClient(timeout=OLLAMA_GENERATE_TIMEOUT)
    try:
        if tool_defs:
            result = await _run_with_tools(
                client=client,
                host_url=host_url,
                model=model,
                system_prompt=system_prompt,
                description=task_row["description"],
                tool_defs=tool_defs,
                tool_map=tool_map,
                project_id=task_row["project_id"],
                task_id=task_row["id"],
            )
        else:
            result = await _run_plain(
                client=client,
                host_url=host_url,
                model=model,
                system_prompt=system_prompt,
                description=task_row["description"],
            )
    finally:
        if not http_client:
            await client.aclose()

    output = result["output"]
    prompt_tokens = result["prompt_tokens"]
    completion_tokens = result["completion_tokens"]

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


async def _run_plain(*, client, host_url, model, system_prompt, description) -> dict:
    """Original /api/generate path — no tool support."""
    body = {
        "model": model,
        "prompt": description,
        "system": system_prompt,
        "stream": False,
    }
    resp = await client.post(
        f"{host_url}/api/generate", json=body, timeout=OLLAMA_GENERATE_TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "output": data.get("response", ""),
        "prompt_tokens": data.get("prompt_eval_count", 0),
        "completion_tokens": data.get("eval_count", 0),
    }


async def _run_with_tools(
    *,
    client,
    host_url,
    model,
    system_prompt,
    description,
    tool_defs,
    tool_map,
    project_id,
    task_id,
) -> dict:
    """/api/chat path with multi-turn tool calling loop."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": description},
    ]

    total_prompt = 0
    total_completion = 0
    text_parts: list[str] = []

    for round_num in range(OLLAMA_MAX_TOOL_ROUNDS):
        body = {
            "model": model,
            "messages": messages,
            "tools": tool_defs,
            "stream": False,
        }

        resp = await client.post(
            f"{host_url}/api/chat", json=body, timeout=OLLAMA_GENERATE_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()

        # Accumulate tokens
        total_prompt += data.get("prompt_eval_count", 0)
        total_completion += data.get("eval_count", 0)

        msg = data.get("message", {})
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")

        if content:
            text_parts.append(content)

        # Append the assistant message to history
        messages.append(msg)

        if not tool_calls:
            break

        # Execute each tool call and feed results back
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            tool_input = fn.get("arguments", {})

            # Auto-inject project_id for file tools
            if tool_name in ("read_file", "write_file"):
                tool_input["project_id"] = project_id

            tool = tool_map.get(tool_name)
            if tool:
                try:
                    tool_result = await tool.execute(tool_input)
                except Exception as e:
                    tool_result = f"Tool error: {type(e).__name__}: operation failed"
                    logger.debug("Ollama tool %s error detail: %s", tool_name, e)
            else:
                tool_result = f"Unknown tool: {tool_name}"

            logger.info(
                "Ollama tool call [task=%s round=%d]: %s",
                task_id, round_num + 1, tool_name,
            )

            # Ollama expects tool results as role=tool messages
            messages.append({
                "role": "tool",
                "content": tool_result,
            })

        # Prune history to prevent context overflow — keep system + user + last N rounds
        # Each round is roughly: assistant (with tool_calls) + N tool results + assistant response
        max_msgs = 2 + (OLLAMA_MAX_TOOL_ROUNDS * 4)
        if len(messages) > max_msgs:
            messages = messages[:2] + messages[-(OLLAMA_MAX_TOOL_ROUNDS * 4):]

    return {
        "output": "\n".join(text_parts),
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
    }
