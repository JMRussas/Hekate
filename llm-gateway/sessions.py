"""Session store for ClaudeConversation instances.

In-memory dict keyed by conversation_id. Sessions are ephemeral —
not persisted across gateway restarts.

Usage:
    conv, cid = await get_or_create(conversation_id, model="claude-sonnet-4-6")
    async for event in conv.send("hello"):
        ...
    await close_session(cid)

Idle cleanup runs as a background task:
    asyncio.create_task(run_cleanup_loop())
"""

from __future__ import annotations

import asyncio
import logging
import time

from conversation import ClaudeConversation

logger = logging.getLogger("llm_gateway.sessions")

# In-memory session store: conversation_id → ClaudeConversation
_sessions: dict[str, ClaudeConversation] = {}

# Default idle timeout before a session is closed
_DEFAULT_IDLE_SECONDS = 600  # 10 minutes


async def get_or_create(
    conversation_id: str | None,
    *,
    model: str | None = "claude-sonnet-4-6",
    mcp_config: str | None = None,
    allowed_tools: list[str] | None = None,
) -> tuple[ClaudeConversation, str]:
    """Get existing session or create a new one.

    Returns (conversation, conversation_id).
    If conversation_id is None or not found, a new session is created.
    """
    if conversation_id and conversation_id in _sessions:
        conv = _sessions[conversation_id]
        logger.debug("sessions: resuming conversation %s", conversation_id[:8])
        return conv, conversation_id

    conv = await ClaudeConversation.create(
        model=model,
        mcp_config=mcp_config,
        allowed_tools=allowed_tools,
    )
    _sessions[conv.conversation_id] = conv
    logger.info("sessions: created conversation %s", conv.conversation_id[:8])
    return conv, conv.conversation_id


async def close_session(conversation_id: str) -> None:
    """Close and remove a session from the store."""
    conv = _sessions.pop(conversation_id, None)
    if conv:
        await conv.close()
        logger.info("sessions: closed conversation %s", conversation_id[:8])


async def cleanup_idle(max_idle_seconds: int = _DEFAULT_IDLE_SECONDS) -> None:
    """Close sessions that have been idle longer than max_idle_seconds.

    Called periodically by the cleanup loop.
    """
    now = time.time()
    to_close = [
        cid for cid, conv in list(_sessions.items())
        if now - conv._last_activity > max_idle_seconds
    ]
    for cid in to_close:
        logger.info(
            "sessions: closing idle conversation %s (idle %.0fs)",
            cid[:8], now - _sessions[cid]._last_activity,
        )
        await close_session(cid)


async def run_cleanup_loop(interval_seconds: int = 60) -> None:
    """Background task that periodically cleans up idle sessions."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await cleanup_idle()
        except Exception as e:
            logger.error("sessions: cleanup error: %s", e)
