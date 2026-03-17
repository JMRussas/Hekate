#  Orchestration Engine - Chat Agent
#
#  Multi-round streaming chat with tool calling, context assembly,
#  and auto-discovered tools. Inspired by noz-ai chat patterns.
#
#  Architecture:
#    - Default: Ollama (qwen3.5) via OpenAI-compatible streaming — free, local
#    - @claude/@sonnet/@opus: Anthropic SDK streaming — paid, powerful
#    - Up to MAX_ROUNDS of LLM + tool call loops per request
#    - Tools from ToolRegistry (auto-discovered at startup)
#    - Brain services (resolve, assemble) from context store
#    - Message history maintained per conversation
#    - Slash commands bypass LLM entirely
#
#  Depends on: context_store_client.py, tools/registry.py, config.py
#  Used by:    routes/chat.py

import json
import logging
import os
import re
import time

import httpx
from starlette.responses import StreamingResponse

from backend.config import ANTHROPIC_API_KEY, cfg
from backend.services.context_store_client import ContextStoreClient
from backend.tools.registry import ToolRegistry

logger = logging.getLogger("orchestration.chat_agent")

MAX_ROUNDS = 4
MAX_CONTEXT_TOKENS = int(cfg("chat.context_limit", 100000))
MAX_OUTPUT_TOKENS = int(cfg("chat.max_tokens", 4096))
OLLAMA_URL = os.environ.get("OLLAMA_URL", cfg("ollama.url", "http://localhost:11434"))
DEFAULT_MODEL = cfg("chat.default_model", "qwen3.5:latest")

# @mention → (backend, model_id, display_name)
# backend: "ollama" (free, local) or "anthropic" (paid, API)
_MENTION_MAP = {
    # Anthropic (paid) — opt-in via @mention
    "@sonnet": ("anthropic", "claude-sonnet-4-20250514", "sonnet"),
    "@opus": ("anthropic", "claude-opus-4-20250514", "opus"),
    "@haiku": ("anthropic", "claude-haiku-4-5-20251001", "haiku"),
    "@claude": ("anthropic", "claude-sonnet-4-20250514", "sonnet"),
    # Ollama (free, local)
    "@qwen": ("ollama", "qwen3.5:latest", "qwen3.5"),
    "@coder": ("ollama", "qwen3-coder:30b-a3b-q4_K_M", "qwen3-coder"),
    "@coder14": ("ollama", "qwen2.5-coder:14b", "qwen2.5-coder-14b"),
    "@coder7": ("ollama", "qwen2.5-coder:7b", "qwen2.5-coder-7b"),
    "@deepseek": ("ollama", "deepseek-coder:latest", "deepseek-coder"),
    "@ollama": ("ollama", DEFAULT_MODEL, "ollama"),
}

# Slash commands
_COMMANDS: dict[str, dict] = {}


def register_command(name: str, description: str, handler):
    _COMMANDS[name] = {"description": description, "handler": handler}


def _parse_mention(message: str) -> tuple[str, str, str, str]:
    """Extract @model mention. Returns (cleaned, backend, model_id, display)."""
    match = re.match(r"^(@\w+)\s*", message)
    if match:
        mention = match.group(1).lower()
        cleaned = message[match.end():].strip()
        if mention in _MENTION_MAP:
            backend, model_id, display = _MENTION_MAP[mention]
            return cleaned, backend, model_id, display
    # Default: local qwen3.5 (free)
    return message, "ollama", DEFAULT_MODEL, "qwen3.5"


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _estimate_tokens(messages: list[dict], tools: list[dict]) -> int:
    return len(json.dumps(messages + tools)) // 4


def _truncate_messages(messages: list[dict], tools: list[dict], budget: int) -> list[dict]:
    if not messages:
        return messages
    tool_tokens = len(json.dumps(tools)) // 4
    available = budget - tool_tokens
    system_msgs = []
    user_msgs = list(messages)
    if user_msgs and user_msgs[0].get("role") == "system":
        system_msgs = [user_msgs.pop(0)]
    while user_msgs and _estimate_tokens(system_msgs + user_msgs, []) > available:
        user_msgs.pop(0)
    return system_msgs + user_msgs


def _tools_to_openai(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool format to OpenAI function calling format."""
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        })
    return result


# ---------------------------------------------------------------------------
# Built-in slash commands
# ---------------------------------------------------------------------------

async def _cmd_help(args: str) -> str:
    lines = ["**Available commands:**"]
    for name, info in sorted(_COMMANDS.items()):
        lines.append(f"- `/{name}` — {info['description']}")
    return "\n".join(lines)


async def _cmd_clear(args: str) -> str:
    return "[conversation cleared]"


async def _cmd_models(args: str) -> str:
    lines = ["**Available models** (default: local qwen3.5, free):"]
    for mention, (backend, model_id, display) in sorted(_MENTION_MAP.items()):
        cost = "free" if backend == "ollama" else "paid"
        lines.append(f"- `{mention}` → {display} ({cost})")
    return "\n".join(lines)


async def _cmd_tools(args: str) -> str:
    return "_tools list not available outside chat context_"


register_command("help", "List available commands", _cmd_help)
register_command("clear", "Clear conversation history", _cmd_clear)
register_command("models", "List available models and @mentions", _cmd_models)
register_command("tools", "List available tools", _cmd_tools)


# ---------------------------------------------------------------------------
# Ollama OpenAI-compatible streaming backend
# ---------------------------------------------------------------------------

async def _stream_ollama(
    model: str,
    system_prompt: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
) -> tuple[str, list[dict]]:
    """
    Stream from Ollama's OpenAI-compatible endpoint.
    Returns (response_text, tool_calls).

    Yields SSE-ready chunks via the caller's generator.
    This is a coroutine that collects the full response — streaming
    to the client happens in the caller.
    """
    url = f"{OLLAMA_URL}/v1/chat/completions"

    # Build messages with system prompt
    api_messages = [{"role": "system", "content": system_prompt}] + messages

    payload = {
        "model": model,
        "messages": api_messages,
        "stream": True,
        "max_tokens": max_tokens,
        "tools": tools if tools else None,
        "parallel_tool_calls": False,
    }
    # Remove None values
    payload = {k: v for k, v in payload.items() if v is not None}

    response_text = ""
    pending_calls: dict[int, dict] = {}

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", url, json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                delta = chunk.get("choices", [{}])[0].get("delta", {})

                # Text content
                if "content" in delta and delta["content"]:
                    response_text += delta["content"]

                # Tool calls (accumulated incrementally)
                if "tool_calls" in delta:
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in pending_calls:
                            pending_calls[idx] = {
                                "id": tc.get("id", f"call_{idx}"),
                                "name": tc.get("function", {}).get("name", ""),
                                "arguments": "",
                            }
                        fn = tc.get("function", {})
                        if "name" in fn and fn["name"]:
                            pending_calls[idx]["name"] = fn["name"]
                        if "arguments" in fn:
                            pending_calls[idx]["arguments"] += fn["arguments"]

    # Parse tool call arguments
    tool_calls = []
    for idx in sorted(pending_calls):
        call = pending_calls[idx]
        try:
            call["arguments"] = json.loads(call["arguments"]) if call["arguments"] else {}
        except json.JSONDecodeError:
            call["arguments"] = {}
        tool_calls.append(call)

    return response_text, tool_calls


async def _stream_ollama_chunks(
    model: str,
    system_prompt: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
):
    """
    Async generator that yields (chunk_type, data) tuples:
      ("text", str)        — text content chunk
      ("tool_calls", list) — completed tool calls
      ("done", None)       — stream finished
    """
    url = f"{OLLAMA_URL}/v1/chat/completions"
    api_messages = [{"role": "system", "content": system_prompt}] + messages

    payload = {
        "model": model,
        "messages": api_messages,
        "stream": True,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["parallel_tool_calls"] = False

    pending_calls: dict[int, dict] = {}

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("POST", url, json=payload) as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                delta = chunk.get("choices", [{}])[0].get("delta", {})

                if "content" in delta and delta["content"]:
                    yield ("text", delta["content"])

                if "tool_calls" in delta:
                    for tc in delta["tool_calls"]:
                        idx = tc.get("index", 0)
                        if idx not in pending_calls:
                            pending_calls[idx] = {
                                "id": tc.get("id", f"call_{idx}"),
                                "name": tc.get("function", {}).get("name", ""),
                                "arguments": "",
                            }
                        fn = tc.get("function", {})
                        if "name" in fn and fn["name"]:
                            pending_calls[idx]["name"] = fn["name"]
                        if "arguments" in fn:
                            pending_calls[idx]["arguments"] += fn["arguments"]

    # Emit completed tool calls
    if pending_calls:
        tool_calls = []
        for idx in sorted(pending_calls):
            call = pending_calls[idx]
            try:
                call["arguments"] = json.loads(call["arguments"]) if call["arguments"] else {}
            except json.JSONDecodeError:
                call["arguments"] = {}
            tool_calls.append(call)
        yield ("tool_calls", tool_calls)

    yield ("done", None)


# ---------------------------------------------------------------------------
# Chat Agent
# ---------------------------------------------------------------------------

class ChatAgent:
    """Multi-round streaming chat with tool calling.

    Default: qwen3.5 via Ollama (free, local, streaming + tools).
    @claude/@sonnet/@opus: Anthropic SDK (paid, streaming + native tools).
    """

    def __init__(self, context_store: ContextStoreClient, tool_registry: ToolRegistry | None = None):
        self._cs = context_store
        self._tools = tool_registry
        self._anthropic = None

    def _get_anthropic_client(self):
        if self._anthropic is None:
            import anthropic
            api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set — use @qwen for free local chat")
            self._anthropic = anthropic.AsyncAnthropic(api_key=api_key)
        return self._anthropic

    def _get_claude_tools(self) -> list[dict]:
        if not self._tools:
            return []
        return [self._tools.get(n).to_claude_tool() for n in self._tools.all_names()]

    def _get_openai_tools(self) -> list[dict]:
        return _tools_to_openai(self._get_claude_tools())

    async def stream_response(
        self,
        message: str,
        conversation_id: str | None = None,
        messages: list[dict] | None = None,
    ) -> StreamingResponse:

        async def _generate():
            nonlocal conversation_id
            t0 = time.monotonic()

            # --- Slash commands ---
            if message.startswith("/"):
                cmd_match = re.match(r"^/(\w+)\s*(.*)", message)
                if cmd_match:
                    cmd_name = cmd_match.group(1).lower()
                    cmd_args = cmd_match.group(2).strip()
                    if cmd_name in _COMMANDS:
                        if cmd_name == "tools" and self._tools:
                            names = self._tools.all_names()
                            result = "**Available tools:**\n" + "\n".join(f"- `{n}`" for n in sorted(names))
                        else:
                            result = await _COMMANDS[cmd_name]["handler"](cmd_args)
                        yield _sse("command", {"name": cmd_name, "result": result})
                        yield _sse("done", {})
                        return

            # --- Parse @mention ---
            cleaned, backend, model_id, display = _parse_mention(message)
            yield _sse("debug_parse", {
                "originalMessage": message,
                "mention": f"@{display}",
                "cleanedMessage": cleaned,
                "backend": backend,
                "model": model_id,
            })

            # --- Create/resume conversation ---
            if not conversation_id:
                conversation_id = await self._cs.brain_create_conversation()
            if conversation_id:
                yield _sse("conversation_id", {"id": conversation_id})

            # --- Brain: resolve + assemble ---
            yield _sse("phase", {"phase": "resolving"})
            t1 = time.monotonic()
            resolved = await self._cs.brain_resolve(cleaned, conversation_id)
            resolve_ms = (time.monotonic() - t1) * 1000

            if resolved:
                yield _sse("debug_interpret", {
                    "intent": str(resolved.get("displayIntent", "Ideation")),
                    "confidence": resolved.get("confidence", 0.3),
                    "durationMs": resolve_ms,
                })

            yield _sse("phase", {"phase": "assembling"})
            t2 = time.monotonic()
            subject_state = None
            if resolved:
                subject_state = await self._cs.brain_assemble(resolved, conversation_id)
            assemble_ms = (time.monotonic() - t2) * 1000

            if subject_state:
                node_count = (
                    len(subject_state.get("resolvedNodeStates", []))
                    + len(subject_state.get("connectedNodes", []))
                    + len(subject_state.get("relatedNodes", []))
                )
                yield _sse("debug_context", {"nodeCount": node_count, "durationMs": assemble_ms})

            # --- Build prompt ---
            system_prompt = _build_system_prompt(subject_state, display)

            # --- Message history ---
            history = messages or []
            history.append({"role": "user", "content": cleaned})

            yield _sse("model", {"backend": backend, "model": model_id, "display": display})

            # --- Route to backend ---
            if backend == "anthropic":
                async for event in self._stream_anthropic(system_prompt, history, model_id, display):
                    yield event
            else:
                async for event in self._stream_local(system_prompt, history, model_id, display):
                    yield event

            # --- Store turns ---
            if conversation_id and hasattr(self, '_last_response'):
                await self._cs.brain_store_turn(conversation_id, "user", message)
                await self._cs.brain_store_turn(conversation_id, display, self._last_response)

            # --- Timing ---
            total_ms = (time.monotonic() - t0) * 1000
            yield _sse("debug_timing", {
                "resolveMs": resolve_ms,
                "assembleMs": assemble_ms,
                "totalMs": total_ms,
            })
            yield _sse("done", {})

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    async def _stream_local(self, system_prompt, history, model_id, display):
        """Stream via Ollama OpenAI-compatible endpoint. Free, local, streaming + tools."""
        openai_tools = self._get_openai_tools()

        token_budget = MAX_CONTEXT_TOKENS - MAX_OUTPUT_TOKENS
        working = _truncate_messages(history, openai_tools, token_budget)
        yield _sse("context", {
            "messages": len(working),
            "tools": len(openai_tools),
            "estimatedTokens": _estimate_tokens(working, openai_tools),
        })

        full_response = ""

        for round_num in range(MAX_ROUNDS):
            yield _sse("phase", {"phase": "generating" if round_num == 0 else f"round_{round_num + 1}"})

            try:
                response_text = ""
                tool_calls = []

                async for chunk_type, data in _stream_ollama_chunks(
                    model_id, system_prompt, working, openai_tools, MAX_OUTPUT_TOKENS,
                ):
                    if chunk_type == "text":
                        response_text += data
                        if round_num == 0:
                            yield _sse("token", {"text": data})
                    elif chunk_type == "tool_calls":
                        tool_calls = data

                if round_num == 0:
                    full_response = response_text

                if not tool_calls:
                    break

                # Build assistant message with tool calls (OpenAI format)
                assistant_msg = {"role": "assistant", "content": response_text or None}
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                        }
                        for tc in tool_calls
                    ]
                working.append(assistant_msg)

                # Execute tools
                for tc in tool_calls:
                    tool_name = tc["name"]
                    tool_input = tc["arguments"]
                    yield _sse("tool_call", {"name": tool_name, "toolId": tc["id"], "input": tool_input})

                    tool = self._tools.get(tool_name) if self._tools else None
                    if tool:
                        try:
                            result_text = await tool.execute(tool_input)
                        except Exception as e:
                            result_text = f"Tool error: {e}"
                            logger.error("Tool %s failed: %s", tool_name, e)
                    else:
                        result_text = f"Unknown tool: {tool_name}"

                    yield _sse("tool_result", {"name": tool_name, "toolId": tc["id"], "resultLength": len(result_text)})

                    # OpenAI format: tool results are role=tool messages
                    working.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_text[:10000],
                    })

            except Exception as exc:
                logger.error("Ollama call failed (round %d): %s", round_num, exc)
                if round_num == 0:
                    full_response = f"Error: {exc}"
                    yield _sse("token", {"text": full_response})
                break

        self._last_response = full_response

    async def _stream_anthropic(self, system_prompt, history, model_id, display):
        """Stream via Anthropic SDK. Paid, powerful, native tool_use."""
        claude_tools = self._get_claude_tools()

        token_budget = MAX_CONTEXT_TOKENS - MAX_OUTPUT_TOKENS
        working = _truncate_messages(history, claude_tools, token_budget)
        yield _sse("context", {
            "messages": len(working),
            "tools": len(claude_tools),
            "estimatedTokens": _estimate_tokens(working, claude_tools),
        })

        client = self._get_anthropic_client()
        full_response = ""

        for round_num in range(MAX_ROUNDS):
            yield _sse("phase", {"phase": "generating" if round_num == 0 else f"round_{round_num + 1}"})

            try:
                response_text = ""
                tool_use_blocks = []

                async with client.messages.stream(
                    model=model_id,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    system=system_prompt,
                    messages=working,
                    tools=claude_tools or None,
                ) as stream:
                    async for event in stream:
                        if event.type == "content_block_start":
                            if hasattr(event.content_block, "type") and event.content_block.type == "tool_use":
                                tool_use_blocks.append({
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "input": "",
                                })
                        elif event.type == "content_block_delta":
                            if hasattr(event.delta, "text"):
                                chunk = event.delta.text
                                response_text += chunk
                                if round_num == 0:
                                    yield _sse("token", {"text": chunk})
                            elif hasattr(event.delta, "partial_json"):
                                if tool_use_blocks:
                                    tool_use_blocks[-1]["input"] += event.delta.partial_json

                if round_num == 0:
                    full_response = response_text

                if not tool_use_blocks:
                    break

                # Parse tool inputs
                for block in tool_use_blocks:
                    try:
                        block["input"] = json.loads(block["input"]) if block["input"] else {}
                    except json.JSONDecodeError:
                        block["input"] = {}

                # Append assistant message
                assistant_content = []
                if response_text:
                    assistant_content.append({"type": "text", "text": response_text})
                for block in tool_use_blocks:
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": block["input"],
                    })
                working.append({"role": "assistant", "content": assistant_content})

                # Execute tools
                tool_results = []
                for block in tool_use_blocks:
                    tool_name = block["name"]
                    tool_input = block["input"]
                    yield _sse("tool_call", {"name": tool_name, "toolId": block["id"], "input": tool_input})

                    tool = self._tools.get(tool_name) if self._tools else None
                    if tool:
                        try:
                            result_text = await tool.execute(tool_input)
                        except Exception as e:
                            result_text = f"Tool error: {e}"
                            logger.error("Tool %s failed: %s", tool_name, e)
                    else:
                        result_text = f"Unknown tool: {tool_name}"

                    yield _sse("tool_result", {"name": tool_name, "toolId": block["id"], "resultLength": len(result_text)})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result_text[:10000],
                    })

                working.append({"role": "user", "content": tool_results})

            except Exception as exc:
                logger.error("Anthropic call failed (round %d): %s", round_num, exc)
                if round_num == 0:
                    full_response = f"Error: {exc}"
                    yield _sse("token", {"text": full_response})
                break

        self._last_response = full_response


def _build_system_prompt(subject_state: dict | None, model_display: str) -> str:
    parts = [
        f"You are {model_display}, an AI assistant in the Hekate platform.",
        "You help with coding, writing, analysis, planning, and creative work.",
        "Be concise and direct. Use markdown for formatting when helpful.",
        "You have access to tools — use them when they would help answer the question.",
    ]

    if not subject_state:
        return "\n".join(parts)

    resolved_nodes = subject_state.get("resolvedNodeStates", [])
    if resolved_nodes:
        parts.append("\n## Relevant Context")
        for node in resolved_nodes[:5]:
            name = node.get("name") or node.get("nodeType", "unknown")
            value = node.get("value", "")
            parts.append(f"- **{name}**: {value[:300] if value else '(no content)'}")

    connected = subject_state.get("connectedNodes", [])
    if connected:
        parts.append("\n## Related")
        for node in connected[:5]:
            parts.append(f"- {node.get('name') or node.get('nodeType', '?')}")

    open_items = subject_state.get("openItems", [])
    if open_items:
        parts.append("\n## Open Items")
        for item in open_items[:3]:
            parts.append(f"- {item.get('name') or item.get('nodeType', '?')}")

    contract = subject_state.get("contract")
    if contract:
        parts.append(f"\n## Your Role\n{contract.get('function', '')}")
        constraints = contract.get("constraints", [])
        if constraints:
            parts.append("Constraints: " + "; ".join(constraints))

    return "\n".join(parts)
