#  Orchestration Engine - Chat Agent
#
#  Multi-round streaming chat with tool calling, context assembly,
#  and auto-discovered tools. Inspired by noz-ai chat patterns.
#
#  Architecture:
#    - Anthropic SDK for streaming + native tool_use
#    - Up to MAX_ROUNDS of LLM ↔ tool call loops per request
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

from starlette.responses import StreamingResponse

from backend.config import ANTHROPIC_API_KEY, cfg
from backend.services.context_store_client import ContextStoreClient
from backend.tools.registry import ToolRegistry

logger = logging.getLogger("orchestration.chat_agent")

MAX_ROUNDS = 4
MAX_CONTEXT_TOKENS = int(cfg("chat.context_limit", 100000))
MAX_OUTPUT_TOKENS = int(cfg("chat.max_tokens", 4096))

# @mention → (provider, model_id, display_name)
_MENTION_MAP = {
    "@sonnet": ("anthropic", "claude-sonnet-4-20250514", "sonnet"),
    "@opus": ("anthropic", "claude-opus-4-20250514", "opus"),
    "@haiku": ("anthropic", "claude-haiku-4-5-20251001", "haiku"),
    "@claude": ("anthropic", "claude-sonnet-4-20250514", "sonnet"),
    "@gemini": ("gemini", "gemini", "gemini"),
    "@flash": ("gemini", "flash", "flash"),
    "@ollama": ("ollama", "ollama", "ollama"),
    "@qwen": ("ollama", "qwen", "qwen"),
}

# Slash commands — bypass LLM entirely
_COMMANDS: dict[str, str] = {}  # populated by register_command


def register_command(name: str, description: str, handler):
    """Register a slash command. Handler is async fn(args: str) -> str."""
    _COMMANDS[name] = {"description": description, "handler": handler}


def _parse_mention(message: str) -> tuple[str, str, str, str]:
    """Extract @model mention. Returns (cleaned, provider, model_id, display)."""
    match = re.match(r"^(@\w+)\s*", message)
    if match:
        mention = match.group(1).lower()
        cleaned = message[match.end():].strip()
        if mention in _MENTION_MAP:
            provider, model_id, display = _MENTION_MAP[mention]
            return cleaned, provider, model_id, display
    return message, "anthropic", "claude-sonnet-4-20250514", "sonnet"


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _estimate_tokens(messages: list[dict], tools: list[dict]) -> int:
    """Rough token estimate: len(json) / 4."""
    return len(json.dumps(messages + tools)) // 4


def _truncate_messages(
    messages: list[dict], tools: list[dict], budget: int
) -> list[dict]:
    """Drop oldest messages (keep system) to fit within token budget."""
    if not messages:
        return messages

    tool_tokens = len(json.dumps(tools)) // 4
    available = budget - tool_tokens

    # Always keep first message if it's system
    system_msgs = []
    user_msgs = list(messages)
    if user_msgs and user_msgs[0].get("role") == "system":
        system_msgs = [user_msgs.pop(0)]

    # Drop from the front (oldest) until we fit
    while user_msgs and _estimate_tokens(system_msgs + user_msgs, []) > available:
        user_msgs.pop(0)

    return system_msgs + user_msgs


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
    lines = ["**Available models:**"]
    for mention, (provider, model_id, display) in sorted(_MENTION_MAP.items()):
        lines.append(f"- `{mention}` → {provider}/{display}")
    return "\n".join(lines)


async def _cmd_tools(args: str) -> str:
    # Filled dynamically when ChatAgent has access to registry
    return "_tools list not available outside chat context_"


# Register built-in commands
register_command("help", "List available commands", _cmd_help)
register_command("clear", "Clear conversation history", _cmd_clear)
register_command("models", "List available models and @mentions", _cmd_models)
register_command("tools", "List available tools", _cmd_tools)


# ---------------------------------------------------------------------------
# Chat Agent
# ---------------------------------------------------------------------------

class ChatAgent:
    """Multi-round streaming chat with tool calling."""

    def __init__(self, context_store: ContextStoreClient, tool_registry: ToolRegistry | None = None):
        self._cs = context_store
        self._tools = tool_registry
        self._anthropic = None

    def _get_client(self):
        if self._anthropic is None:
            import anthropic
            api_key = ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            self._anthropic = anthropic.AsyncAnthropic(api_key=api_key)
        return self._anthropic

    def _get_tools_for_claude(self) -> list[dict]:
        """Convert registered tools to Anthropic API format."""
        if not self._tools:
            return []
        return [self._tools.get(n).to_claude_tool() for n in self._tools.all_names()]

    async def stream_response(
        self,
        message: str,
        conversation_id: str | None = None,
        messages: list[dict] | None = None,
    ) -> StreamingResponse:

        async def _generate():
            nonlocal conversation_id
            t0 = time.monotonic()

            # --- Slash command check ---
            if message.startswith("/"):
                cmd_match = re.match(r"^/(\w+)\s*(.*)", message)
                if cmd_match:
                    cmd_name = cmd_match.group(1).lower()
                    cmd_args = cmd_match.group(2).strip()
                    if cmd_name in _COMMANDS:
                        # Special: /tools needs registry access
                        if cmd_name == "tools" and self._tools:
                            names = self._tools.all_names()
                            result = "**Available tools:**\n" + "\n".join(f"- `{n}`" for n in sorted(names))
                        else:
                            result = await _COMMANDS[cmd_name]["handler"](cmd_args)
                        yield _sse("command", {"name": cmd_name, "result": result})
                        yield _sse("done", {})
                        return

            # --- Parse @mention ---
            cleaned, provider, model_id, display = _parse_mention(message)
            yield _sse("debug_parse", {
                "originalMessage": message,
                "mention": f"@{display}",
                "cleanedMessage": cleaned,
                "provider": provider,
                "model": model_id,
            })

            # --- Non-Anthropic providers: fall back to llm_router ---
            if provider != "anthropic":
                yield _sse("phase", {"phase": "generating"})
                try:
                    from backend.services.llm_router import call_llm
                    resp = await call_llm("You are a helpful assistant.", cleaned, provider=provider)
                    yield _sse("token", {"text": resp.text})
                except Exception as e:
                    yield _sse("token", {"text": f"Error: {e}"})
                yield _sse("done", {})
                return

            # --- Create/resume conversation ---
            if not conversation_id:
                conversation_id = await self._cs.brain_create_conversation()
            if conversation_id:
                yield _sse("conversation_id", {"id": conversation_id})

            # --- Entity resolution via brain ---
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

            # --- Context assembly via brain ---
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

            # --- Build system prompt ---
            system_prompt = _build_system_prompt(subject_state, display)

            # --- Build message history ---
            history = messages or []
            history.append({"role": "user", "content": cleaned})

            # --- Get tools ---
            claude_tools = self._get_tools_for_claude()
            yield _sse("model", {"provider": provider, "model": model_id, "display": display})

            # --- Truncate to fit context ---
            token_budget = MAX_CONTEXT_TOKENS - MAX_OUTPUT_TOKENS
            working = _truncate_messages(history, claude_tools, token_budget)
            yield _sse("context", {
                "messages": len(working),
                "tools": len(claude_tools),
                "estimatedTokens": _estimate_tokens(working, claude_tools),
            })

            # --- Multi-round LLM + tool loop ---
            client = self._get_client()
            full_response = ""
            tool_context = {}  # shared between tool calls in same round

            for round_num in range(MAX_ROUNDS):
                yield _sse("phase", {"phase": "generating" if round_num == 0 else f"round_{round_num + 1}"})
                t3 = time.monotonic()

                try:
                    # Stream from Anthropic API
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
                                if hasattr(event.content_block, "text"):
                                    pass  # text block starting
                                elif hasattr(event.content_block, "type") and event.content_block.type == "tool_use":
                                    tool_use_blocks.append({
                                        "id": event.content_block.id,
                                        "name": event.content_block.name,
                                        "input": "",
                                    })
                            elif event.type == "content_block_delta":
                                if hasattr(event.delta, "text"):
                                    chunk = event.delta.text
                                    response_text += chunk
                                    # Only stream text to client on first round
                                    if round_num == 0:
                                        yield _sse("token", {"text": chunk})
                                elif hasattr(event.delta, "partial_json"):
                                    if tool_use_blocks:
                                        tool_use_blocks[-1]["input"] += event.delta.partial_json

                    generate_ms = (time.monotonic() - t3) * 1000

                    if round_num == 0:
                        full_response = response_text

                    # --- Execute tool calls ---
                    if not tool_use_blocks:
                        break  # No tools called, we're done

                    # Parse tool inputs from accumulated JSON
                    for block in tool_use_blocks:
                        try:
                            block["input"] = json.loads(block["input"]) if block["input"] else {}
                        except json.JSONDecodeError:
                            block["input"] = {}

                    # Append assistant message with tool_use to history
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

                    # Execute each tool
                    tool_results = []
                    for block in tool_use_blocks:
                        tool_name = block["name"]
                        tool_input = block["input"]
                        yield _sse("tool_call", {
                            "name": tool_name,
                            "toolId": block["id"],
                            "input": tool_input,
                        })

                        tool = self._tools.get(tool_name) if self._tools else None
                        if tool:
                            try:
                                result_text = await tool.execute(tool_input)
                                tool_context[tool_name] = result_text
                            except Exception as e:
                                result_text = f"Tool error: {e}"
                                logger.error("Tool %s failed: %s", tool_name, e)
                        else:
                            result_text = f"Unknown tool: {tool_name}"

                        yield _sse("tool_result", {
                            "name": tool_name,
                            "toolId": block["id"],
                            "resultLength": len(result_text),
                        })

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block["id"],
                            "content": result_text[:10000],  # Cap tool output
                        })

                    # Append tool results to history
                    working.append({"role": "user", "content": tool_results})

                except Exception as exc:
                    logger.error("LLM call failed (round %d): %s", round_num, exc)
                    if round_num == 0:
                        full_response = f"Error: {exc}"
                        yield _sse("token", {"text": full_response})
                    break

            # --- Store turns in brain ---
            if conversation_id:
                await self._cs.brain_store_turn(conversation_id, "user", message)
                if full_response:
                    await self._cs.brain_store_turn(conversation_id, display, full_response)

            # --- Extract ideas (fire and forget) ---
            yield _sse("phase", {"phase": "extracting"})

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


def _build_system_prompt(subject_state: dict | None, model_display: str) -> str:
    """Build system prompt from assembled subject state."""
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
