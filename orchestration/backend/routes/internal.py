#  Orchestration Engine - Internal Routes
#
#  Authenticated endpoints for internal use: chat proxy for editor
#  integration, multi-model routing across CLI providers and Ollama.
#
#  Depends on: backend/middleware/auth.py
#  Used by:    app.py

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
from typing import Optional

import traceback

import httpx

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.config import cfg
from backend.container import Container
from backend.db.connection import Database
from backend.middleware.auth import get_current_user
from backend.services.planner import PlannerService

logger = logging.getLogger("orchestration.internal")

router = APIRouter(prefix="/internal", tags=["internal"])

# Allowed providers — reject unknown values to prevent command injection
_ALLOWED_PROVIDERS = {"claude", "gemini", "codex", "ollama"}


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
async def chat(request: ChatRequest, _user: dict = Depends(get_current_user)):
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
    if provider not in _ALLOWED_PROVIDERS:
        return ChatResponse(
            response=f"Unknown provider: {provider}",
            provider=provider,
        )

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
    from backend.config import OLLAMA_URL
    ollama_url = OLLAMA_URL
    ollama_model = request.model or os.environ.get("OLLAMA_MODEL", cfg("ollama.default_model", "qwen3.5:latest"))

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


# ---------------------------------------------------------------------------
# Filesystem Browse
# ---------------------------------------------------------------------------

# Restrict browsing to safe root directories — prevent traversal to system dirs
_BROWSE_ROOTS = [
    os.path.expanduser("~/Documents/git"),
    os.path.expanduser("~/Git"),
]


@router.get("/browse")
async def browse_directories(
    path: str = "",
    _user: dict = Depends(get_current_user),
):
    """List subdirectories for the folder picker.

    If path is empty, returns the allowed root directories.
    If path is provided, returns its subdirectories (must be under an allowed root).
    Returns only directories, not files. Detects git repos.
    """
    if not path:
        # Return root directories that exist
        roots = []
        for root in _BROWSE_ROOTS:
            if os.path.isdir(root):
                roots.append({
                    "path": root,
                    "name": os.path.basename(root),
                    "is_git": os.path.isdir(os.path.join(root, ".git")),
                    "has_children": True,
                })
        return {"directories": roots, "current": ""}

    # Validate path is under an allowed root
    abs_path = os.path.abspath(path)
    if not any(abs_path.startswith(os.path.abspath(r)) for r in _BROWSE_ROOTS):
        return {"directories": [], "current": abs_path, "error": "Path not under allowed roots"}

    if not os.path.isdir(abs_path):
        return {"directories": [], "current": abs_path, "error": "Not a directory"}

    dirs = []
    try:
        for entry in sorted(os.scandir(abs_path), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith('.'):
                dirs.append({
                    "path": entry.path,
                    "name": entry.name,
                    "is_git": os.path.isdir(os.path.join(entry.path, ".git")),
                    "has_children": any(
                        e.is_dir() for e in os.scandir(entry.path)
                        if not e.name.startswith('.')
                    ) if os.access(entry.path, os.R_OK) else False,
                })
    except PermissionError:
        return {"directories": [], "current": abs_path, "error": "Permission denied"}

    return {"directories": dirs, "current": abs_path}


class PlanNodeChildrenRequest(BaseModel):
    children: list[dict]


@router.post("/plan-nodes/{node_id}/children")
@inject
async def submit_plan_node_children(
    node_id: str,
    request: PlanNodeChildrenRequest,
    _user: dict = Depends(get_current_user),
    db: Database = Depends(Provide[Container.db]),
):
    """Save child plan_nodes submitted by a planning sub-agent.

    Called by the planning agent via the prometheus MCP submit_plan_children tool.
    Writes child rows and emits plan_node_created events via the god_relay_events table.
    """
    # Load parent node
    node = await db.fetchone("SELECT * FROM plan_nodes WHERE id = $1", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail=f"Plan node {node_id} not found")

    project_id = node["project_id"]
    plan_id = node["plan_id"]
    index_path = node["index_path"]
    child_level = (node["level"] or 0) + 1
    project_context = node["project_context"] or ""

    # Count existing children (for index continuity)
    cnt_row = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM plan_nodes WHERE project_id = $1 AND parent_index = $2",
        (project_id, index_path),
    )
    start_idx = (cnt_row["cnt"] if cnt_row else 0) + 1

    now = time.time()
    node_ids: list[str] = []
    relay_events: list[dict] = []

    for i, child in enumerate(request.children, start=start_idx):
        child_id = uuid.uuid4().hex[:12]
        child_index = f"{index_path}.{i}"
        child_title = child.get("title", f"Node {child_index}")

        dep_indices = child.get("depends_on_indices", [])
        dep_paths = [f"{index_path}.{d + 1}" for d in dep_indices if isinstance(d, int)]
        child["depends_on_paths"] = dep_paths

        await db.execute_write(
            "INSERT OR IGNORE INTO plan_nodes "
            "(id, plan_id, project_id, index_path, level, status, title, "
            "content_json, project_context, parent_index, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
            (
                child_id, plan_id, project_id, child_index, child_level,
                "stub", child_title, json.dumps(child),
                project_context, index_path, now, now,
            ),
        )
        node_ids.append(child_id)

        # Emit plan_node_created via relay table for the gods pipeline to pick up
        relay_payload = json.dumps({
            "project_id": project_id,
            "node_id": child_id,
            "index_path": child_index,
            "level": child_level,
            "plan_id": plan_id,
        })
        relay_events.append((
            uuid.uuid4().hex[:12],
            "plan_node_created",
            relay_payload,
            "prometheus_mcp",
            now,
        ))

    for relay_row in relay_events:
        await db.execute_write(
            "INSERT INTO god_relay_events (id, event_type, payload, source, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            relay_row,
        )

    logger.info(
        "submit_plan_node_children: saved %d children under node %s for project %s",
        len(node_ids), index_path, project_id[:8],
    )

    return {"saved": len(node_ids), "node_ids": node_ids}


class PlanRequest(BaseModel):
    project_id: str
    provider: Optional[str] = None


@router.post("/plan")
@inject
async def plan(
    request: PlanRequest,
    _user: dict = Depends(get_current_user),
    planner: PlannerService = Depends(Provide[Container.planner]),
):
    """Authenticated plan endpoint for internal use. Routes through CLI."""
    try:
        result = await planner.generate(request.project_id, provider=request.provider)
        return result
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Plan failed: %s\n%s", e, tb)
        return {"error": str(e)}
