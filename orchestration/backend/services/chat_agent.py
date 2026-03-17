#  Orchestration Engine - Chat Agent
#
#  Universal conversational agent that combines:
#  - Context-store brain services (resolve, assemble, extract)
#  - LLM routing via llm_router (Claude, Gemini, Codex, Ollama)
#  - SSE streaming to the client
#
#  Uses the existing llm_router for model calls and context_store_client
#  for brain services. Does NOT reimplement CLI spawning or model routing.
#
#  Depends on: context_store_client.py, llm_router.py
#  Used by:    routes/chat.py

import json
import logging
import re
import time

from starlette.responses import StreamingResponse

from backend.services.context_store_client import ContextStoreClient
from backend.services.llm_router import call_llm

logger = logging.getLogger("orchestration.chat_agent")

# @mention → provider for llm_router
_MENTION_TO_PROVIDER = {
    "@sonnet": ("claude", "sonnet"),
    "@opus": ("claude", "opus"),
    "@haiku": ("claude", "haiku"),
    "@claude": ("claude", "sonnet"),
    "@gemini": ("gemini", "gemini"),
    "@flash": ("gemini", "flash"),
    "@pro": ("gemini", "pro"),
    "@codex": ("codex", "codex"),
    "@gpt": ("codex", "gpt"),
    "@ollama": ("ollama", "ollama"),
    "@qwen": ("ollama", "qwen"),
}


def _parse_mention(message: str) -> tuple[str, str, str]:
    """Extract @model mention. Returns (cleaned_message, provider, display)."""
    match = re.match(r"^(@\w+)\s*", message)
    if match:
        mention = match.group(1).lower()
        cleaned = message[match.end():].strip()
        if mention in _MENTION_TO_PROVIDER:
            provider, display = _MENTION_TO_PROVIDER[mention]
            return cleaned, provider, display
    return message, "claude", "sonnet"


def _sse(event: str, data: dict) -> str:
    """Format a single SSE event string."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


class ChatAgent:
    """Universal chat agent — brain services + model routing + SSE streaming."""

    def __init__(self, context_store: ContextStoreClient):
        self._cs = context_store

    async def stream_response(
        self,
        message: str,
        conversation_id: str | None = None,
    ) -> StreamingResponse:
        """Returns a StreamingResponse yielding SSE events."""

        async def _generate():
            nonlocal conversation_id
            t0 = time.monotonic()

            # 1. Parse @mention → route to provider
            cleaned, provider, display = _parse_mention(message)
            yield _sse("debug_parse", {
                "originalMessage": message,
                "mention": f"@{display}",
                "cleanedMessage": cleaned,
                "model": {"provider": provider, "model": display, "display": display},
                "durationMs": 0,
            })

            # 2. Create or resume conversation via brain
            if not conversation_id:
                conversation_id = await self._cs.brain_create_conversation()
                if not conversation_id:
                    yield _sse("error", {"message": "Failed to create conversation in context store"})
                    yield _sse("done", {})
                    return

            yield _sse("conversation_id", {"id": conversation_id})

            # 3. Entity resolution via brain
            yield _sse("phase", {"phase": "resolving"})
            t1 = time.monotonic()
            resolved = await self._cs.brain_resolve(cleaned, conversation_id)
            resolve_ms = (time.monotonic() - t1) * 1000

            if resolved:
                yield _sse("debug_interpret", {
                    "intent": str(resolved.get("displayIntent", "Ideation")),
                    "confidence": resolved.get("confidence", 0.3),
                    "reasoning": resolved.get("reasoning"),
                    "isRegexFallback": resolved.get("isRegexFallback", True),
                    "durationMs": resolve_ms,
                })
                yield _sse("intent", {
                    "intent": str(resolved.get("displayIntent", "Ideation")),
                    "confidence": f"{resolved.get('confidence', 0.3):.2f}",
                    "pattern": resolved.get("reasoning"),
                })

            # 4. Context assembly via brain
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

            # 5. Build prompt from subject state
            system_prompt = _build_system_prompt(subject_state, display)

            # 6. Call model via llm_router (existing infrastructure)
            yield _sse("model", {"provider": provider, "model": display, "display": display})
            yield _sse("phase", {"phase": "generating"})

            t3 = time.monotonic()
            try:
                llm_resp = await call_llm(
                    system_prompt,
                    cleaned,
                    provider=provider,
                    task_type="chat",
                )
                full_response = llm_resp.text
                # Emit the response as a single token block
                # (streaming will be added when llm_router supports it)
                yield _sse("token", {"text": full_response})
            except Exception as exc:
                logger.error("LLM call failed: %s", exc)
                full_response = f"Error calling {provider}: {exc}"
                yield _sse("token", {"text": full_response})

            generate_ms = (time.monotonic() - t3) * 1000

            # 7. Store turns in brain (fire-and-forget pattern)
            await self._cs.brain_store_turn(conversation_id, "user", message)
            await self._cs.brain_store_turn(conversation_id, display, full_response)

            # 8. Extract ideas from response (async, non-blocking)
            yield _sse("phase", {"phase": "extracting"})
            # TODO: create proper thread per exchange, pass thread_id to extract

            # 9. Timing
            total_ms = (time.monotonic() - t0) * 1000
            yield _sse("debug_timing", {
                "resolveMs": resolve_ms,
                "assembleMs": assemble_ms,
                "generateMs": generate_ms,
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
    ]

    if not subject_state:
        return "\n".join(parts)

    # Resolved node context
    resolved_nodes = subject_state.get("resolvedNodeStates", [])
    if resolved_nodes:
        parts.append("\n## Relevant Context")
        for node in resolved_nodes[:5]:
            name = node.get("name") or node.get("nodeType", "unknown")
            value = node.get("value", "")
            parts.append(f"- **{name}**: {value[:300] if value else '(no content)'}")

    # Connected nodes
    connected = subject_state.get("connectedNodes", [])
    if connected:
        parts.append("\n## Related")
        for node in connected[:5]:
            parts.append(f"- {node.get('name') or node.get('nodeType', '?')}")

    # Open items
    open_items = subject_state.get("openItems", [])
    if open_items:
        parts.append("\n## Open Items")
        for item in open_items[:3]:
            parts.append(f"- {item.get('name') or item.get('nodeType', '?')}")

    # Agent contract
    contract = subject_state.get("contract")
    if contract:
        parts.append(f"\n## Your Role\n{contract.get('function', '')}")
        constraints = contract.get("constraints", [])
        if constraints:
            parts.append("Constraints: " + "; ".join(constraints))

    return "\n".join(parts)
