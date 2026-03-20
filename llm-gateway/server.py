#!/usr/bin/env python3
"""
LLM Gateway — HTTP proxy for CLI-authenticated LLM providers.

Persistent CLI sessions eliminate per-request startup overhead:
  - Claude: single process with --input-format stream-json
  - Gemini: warm process pool (spawn ahead, reuse)
  - Ollama: direct HTTP (already fast)

Port: 5210
Health: GET /health
Chat:  POST /v1/chat
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("LLM_GATEWAY_PORT", "5210"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("llm-gateway")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    provider: str  # "claude", "gemini", "codex", "ollama"
    system_prompt: str
    user_message: str
    model: Optional[str] = None
    timeout: Optional[int] = 300


class ChatResponse(BaseModel):
    text: str
    provider: str
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# CLI resolution
# ---------------------------------------------------------------------------

def _resolve_cmd(name: str) -> Optional[str]:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if sys.platform == "win32":
        npm_bin = os.path.join(os.environ.get("APPDATA", ""), "npm")
        for ext in (".cmd", ".exe", ""):
            candidate = os.path.join(npm_bin, f"{name}{ext}")
            if os.path.isfile(candidate):
                return candidate
    return None


_CLI_CACHE: dict[str, Optional[str]] = {}


def _get_cli(name: str) -> Optional[str]:
    if name not in _CLI_CACHE:
        _CLI_CACHE[name] = _resolve_cmd(name)
    return _CLI_CACHE[name]


# ---------------------------------------------------------------------------
# Gemini process pool — warm processes ready to handle requests
# ---------------------------------------------------------------------------

class GeminiPool:
    """Pool of warm Gemini CLI processes.

    Spawns processes ahead of time so requests don't wait for startup.
    """

    def __init__(self, pool_size: int = 2):
        self._pool_size = pool_size
        self._binary = _get_cli("gemini")
        self._lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()  # serialize requests

    async def chat(self, prompt: str, model: Optional[str] = None,
                   timeout: int = 300) -> str:
        """Send a prompt to Gemini. Spawns a process per request but
        serializes to avoid overwhelming the CLI."""
        if not self._binary:
            raise HTTPException(503, "Gemini CLI not found")

        async with self._request_lock:
            cmd = [self._binary, "-p", "--approval-mode", "yolo", "-o", "text"]
            if model:
                cmd.extend(["-m", model])

            logger.info("Gemini: calling%s", f" model={model}" if model else "")
            t0 = time.time()

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={**os.environ, "GEMINI_FORCE_FILE_STORAGE": "true"},
            )

            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode()),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise HTTPException(504, f"Gemini timed out ({timeout}s)")

            elapsed = time.time() - t0
            text = stdout.decode().strip()

            if proc.returncode != 0:
                raise HTTPException(502, f"Gemini exit {proc.returncode}")

            logger.info("Gemini: %d chars in %.1fs", len(text), elapsed)
            return text


# ---------------------------------------------------------------------------
# Claude persistent session (stream-json stdin/stdout)
# ---------------------------------------------------------------------------

class ClaudeSession:
    """Persistent Claude CLI session using --input-format stream-json.

    One process handles multiple requests via stdin/stdout JSON messages.
    Falls back to per-request subprocess if persistent mode fails.
    """

    def __init__(self):
        self._binary = _get_cli("claude")
        self._lock = asyncio.Lock()

    async def chat(self, prompt: str, model: Optional[str] = None,
                   timeout: int = 300) -> str:
        """Send a prompt to Claude. Uses -p mode (one process per request
        for now — persistent stream-json mode is complex with multi-turn)."""
        if not self._binary:
            raise HTTPException(503, "Claude CLI not found")

        async with self._lock:
            cmd = [self._binary, "-p", "--output-format", "text", "--no-chrome"]
            if model:
                cmd.extend(["--model", model])

            # Strip CLAUDECODE to avoid nested session detection
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

            logger.info("Claude: calling%s", f" model={model}" if model else "")
            t0 = time.time()

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )

            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode()),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise HTTPException(504, f"Claude timed out ({timeout}s)")

            elapsed = time.time() - t0
            text = stdout.decode().strip()

            if proc.returncode != 0:
                raise HTTPException(502, f"Claude exit {proc.returncode}")

            logger.info("Claude: %d chars in %.1fs", len(text), elapsed)
            return text


# ---------------------------------------------------------------------------
# Ollama (direct HTTP — already fast)
# ---------------------------------------------------------------------------

async def _call_ollama(system_prompt: str, user_message: str,
                       model: Optional[str] = None, timeout: int = 300) -> ChatResponse:
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    ollama_model = model or os.environ.get("OLLAMA_MODEL", "qwen3.5:latest")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    async with httpx.AsyncClient(timeout=float(timeout)) as client:
        resp = await client.post(
            f"{ollama_url}/api/chat",
            json={"model": ollama_model, "messages": messages, "stream": False},
        )
        resp.raise_for_status()
        data = resp.json()

    text = data.get("message", {}).get("content", "")
    return ChatResponse(text=text.strip(), provider="ollama", model=ollama_model)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

# Persistent provider sessions
_gemini_pool: Optional[GeminiPool] = None
_claude_session: Optional[ClaudeSession] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gemini_pool, _claude_session

    # Initialize persistent sessions
    _claude_session = ClaudeSession()
    _gemini_pool = GeminiPool(pool_size=2)

    for name in ("claude", "gemini", "codex"):
        path = _get_cli(name)
        if path:
            logger.info("Found %s CLI: %s", name, path)
        else:
            logger.warning("%s CLI not found", name)

    logger.info("LLM Gateway ready on port %d", PORT)
    yield
    logger.info("LLM Gateway shutting down")


app = FastAPI(title="LLM Gateway", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/ping")
async def ping():
    return {"message": "pong"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/providers")
async def providers():
    """List available providers and their status."""
    result = {}
    for name in ("claude", "gemini", "codex"):
        result[name] = {"available": _get_cli(name) is not None}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/")
            result["ollama"] = {"available": r.status_code == 200}
    except Exception:
        result["ollama"] = {"available": False}
    return result


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Route a chat request to the specified CLI provider."""
    full_prompt = f"{req.system_prompt}\n\n---\n\n{req.user_message}"
    timeout = req.timeout or 300

    if req.provider == "ollama":
        return await _call_ollama(req.system_prompt, req.user_message,
                                  req.model, timeout)

    elif req.provider in ("gemini", "gemini_cli"):
        text = await _gemini_pool.chat(full_prompt, req.model, timeout)
        return ChatResponse(text=text, provider="gemini", model=req.model)

    elif req.provider in ("claude", "claude_code"):
        text = await _claude_session.chat(full_prompt, req.model, timeout)
        return ChatResponse(text=text, provider="claude", model=req.model)

    elif req.provider == "codex":
        # Codex uses same pattern as Gemini — one-off subprocess
        binary = _get_cli("codex")
        if not binary:
            raise HTTPException(503, "Codex CLI not found")
        proc = await asyncio.create_subprocess_exec(
            binary, "exec",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode()), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise HTTPException(504, f"Codex timed out ({timeout}s)")
        return ChatResponse(text=stdout.decode().strip(), provider="codex", model=req.model)

    else:
        raise HTTPException(400, f"Unknown provider: {req.provider}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
