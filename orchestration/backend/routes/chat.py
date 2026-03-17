#  Orchestration Engine - Chat Routes
#
#  Universal chat endpoint — SSE streaming with brain services + model routing.
#  Replaces the context-store's direct CLI spawning.
#
#  Depends on: services/chat_agent.py, container.py
#  Used by:    app.py

import logging
from typing import Optional

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.container import Container
from backend.services.chat_agent import ChatAgent

logger = logging.getLogger("orchestration.chat")

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatStreamRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    context_node_id: Optional[str] = None  # For scoped chat (workspace)


class ConversationListRequest(BaseModel):
    pass


@router.post("/stream")
@inject
async def chat_stream(
    request: ChatStreamRequest,
    chat_agent: ChatAgent = Depends(Provide[Container.chat_agent]),
):
    """Stream a chat response via SSE.

    Resolves entities via context-store brain, assembles context,
    routes to the right model via llm_router, and streams back.
    """
    return await chat_agent.stream_response(
        message=request.message,
        conversation_id=request.conversation_id,
    )
