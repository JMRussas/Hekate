#  Orchestration Engine - Internal Routes
#
#  Unauthenticated endpoints for internal use: chat proxy for editor
#  integration, multi-model routing across CLI providers and Ollama.
#
#  Depends on: (none — standalone, no DB or DI required)
#  Used by:    app.py

import asyncio
import logging
import os
import shutil
import sys
from typing import Optional

import traceback

import httpx

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.container import Container
from backend.services.planner import PlannerService

logger = logging.getLogger("orchestration.internal")

router = APIRouter(prefix="/internal", tags=["internal"])


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ChatMessageEntry(BaseModel):
    """A single message in a conversation history."""

    role: str  # "user", "assistant", "system"
    content: str


class ChatRequest(BaseModel):
    prompt: str
    context: Optional[str] = None
    provider: Optional[str] = None
    messages: Optional[list[ChatMessageEntry]] = None
    model: Optional[str] = None


class ChatResponse(BaseModel):
    response: str
    provider: Optional[str] = None
    model_used: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/chat")
async def chat(request: ChatRequest):
    """Chat endpoint for editor integration with conversation history support.

    Routes to CLI providers (Claude, Gemini, Codex) via subprocess or to
    Ollama via HTTP API. Supports conversation history and model selection.
    """
    # Build prompt with optional context and conversation history
    parts: list[str] = []

    if request.context:
        parts.append(f"Context: {request.context}")

    if request.messages:
        # Include last 20 messages to keep prompt size manageable
        recent = request.messages[-20:]
        history_lines = [f"[{m.role}]: {m.content}" for m in recent]
        parts.append("Previous conversation:\n" + "\n".join(history_lines))

    parts.append(f"Current request:\n{request.prompt}" if request.messages else request.prompt)

    full_prompt = "\n\n".join(parts)

    # Determine provider (default to gemini)
    provider = (request.provider or "gemini").lower()

    # Ollama: use HTTP API directly (supports messages natively)
    if provider == "ollama":
        return await _chat_ollama(request, full_prompt)

    # CLI-based providers — pipe prompt via stdin to avoid command line length limits
    model_used: Optional[str] = None

    if provider == "claude":
        cmd_args = ["claude", "-p", "--output-format", "text"]
    elif provider == "codex":
        cmd_args = ["codex", "exec"]
        if request.model:
            cmd_args.extend(["--model", request.model])
            model_used = request.model
    else:  # gemini default
        cmd_args = ["gemini", "-p", ""]
        if request.model:
            cmd_args.extend(["-m", request.model])
            model_used = request.model

    # On Windows, npm global binaries are .cmd — resolve to full path
    if sys.platform == "win32":
        resolved = shutil.which(cmd_args[0])
        if resolved:
            cmd_args[0] = resolved

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode()), timeout=120,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ChatResponse(
                response="Request timed out",
                provider=provider,
                model_used=model_used,
            )

        stdout_text = stdout.decode().strip()
        stderr_text = stderr.decode().strip()

        if proc.returncode != 0:
            error_msg = stderr_text or f"Command exited with code {proc.returncode}"
            return ChatResponse(
                response=f"Error: {error_msg}",
                provider=provider,
                model_used=model_used,
            )

        return ChatResponse(
            response=stdout_text,
            provider=provider,
            model_used=model_used,
        )
    except FileNotFoundError:
        return ChatResponse(
            response=f"Provider '{provider}' CLI not found",
            provider=provider,
            model_used=model_used,
        )


async def _chat_ollama(request: ChatRequest, full_prompt: str) -> ChatResponse:
    """Route chat to Ollama HTTP API with native message support."""
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    ollama_model = request.model or os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:14b")

    # Build Ollama messages array if conversation history provided
    if request.messages:
        messages: list[dict] = []

        if request.context:
            messages.append({"role": "system", "content": request.context})

        for m in request.messages[-20:]:
            messages.append({"role": m.role, "content": m.content})

        # Add current prompt as the latest user message
        messages.append({"role": "user", "content": request.prompt})

        payload = {
            "model": ollama_model,
            "messages": messages,
            "stream": False,
        }
        api_path = "/api/chat"
    else:
        # Simple generate mode (no history)
        payload = {
            "model": ollama_model,
            "prompt": full_prompt,
            "stream": False,
        }
        api_path = "/api/generate"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{ollama_url}{api_path}", json=payload)
            resp.raise_for_status()
            data = resp.json()

        # /api/chat returns {"message": {"content": "..."}},
        # /api/generate returns {"response": "..."}
        if "message" in data:
            response_text = data["message"].get("content", "")
        else:
            response_text = data.get("response", "")

        return ChatResponse(
            response=response_text.strip(),
            provider="ollama",
            model_used=ollama_model,
        )
    except httpx.HTTPStatusError as exc:
        return ChatResponse(
            response=f"Ollama error: {exc.response.status_code} {exc.response.text}",
            provider="ollama",
            model_used=ollama_model,
        )
    except httpx.ConnectError:
        return ChatResponse(
            response=f"Cannot connect to Ollama at {ollama_url}",
            provider="ollama",
            model_used=ollama_model,
        )


class PlanRequest(BaseModel):
    project_id: str
    provider: Optional[str] = None


@router.post("/plan")
@inject
async def plan(
    request: PlanRequest,
    planner: PlannerService = Depends(Provide[Container.planner]),
):
    """Unauthenticated plan endpoint for internal use. Routes through CLI."""
    try:
        result = await planner.generate(request.project_id, provider=request.provider)
        return result
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Plan failed: %s\n%s", e, tb)
        return {"error": str(e), "traceback": tb}
