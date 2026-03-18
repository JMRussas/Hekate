#!/usr/bin/env python3
"""
LLM Gateway — HTTP proxy for CLI-authenticated LLM providers.

Runs as the user (not LocalSystem) so it has access to CLI OAuth tokens.
NSSM services call this via HTTP instead of spawning CLIs directly.

Port: 5210
Health: GET /health
Chat:  POST /v1/chat
"""

import asyncio
import logging
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

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
# CLI resolution (handles .cmd on Windows)
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


# Cache resolved paths at startup
_CLI_CACHE: dict[str, Optional[str]] = {}


def _get_cli(name: str) -> Optional[str]:
    if name not in _CLI_CACHE:
        _CLI_CACHE[name] = _resolve_cmd(name)
    return _CLI_CACHE[name]


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

async def _call_cli(provider: str, system_prompt: str, user_message: str,
                    model: Optional[str] = None, timeout: int = 300) -> ChatResponse:
    full_prompt = f"{system_prompt}\n\n---\n\n{user_message}"

    cli_map = {"claude": "claude", "gemini": "gemini", "codex": "codex"}
    binary = cli_map.get(provider)
    if not binary:
        raise HTTPException(400, f"Unknown CLI provider: {provider}")

    resolved = _get_cli(binary)
    if not resolved:
        raise HTTPException(503, f"{provider} CLI not found on PATH")

    if provider == "claude":
        cmd_args = [resolved, "-p", "--output-format", "text"]
    elif provider == "codex":
        cmd_args = [resolved, "exec"]
        if model:
            cmd_args.extend(["--model", model])
    elif provider == "gemini":
        cmd_args = [resolved, "-p", ""]
        if model:
            cmd_args.extend(["-m", model])

    logger.info("Calling %s CLI%s", provider, f" model={model}" if model else "")

    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=full_prompt.encode()), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(504, f"{provider} timed out ({timeout}s)")

    stdout_text = stdout.decode().strip()
    stderr_text = stderr.decode().strip()

    if proc.returncode != 0:
        logger.error("%s exit %d: %s", provider, proc.returncode, stderr_text)
        raise HTTPException(502, f"{provider} CLI failed (exit {proc.returncode}): {stderr_text}")

    return ChatResponse(text=stdout_text, provider=provider, model=model)


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

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log available CLIs at startup
    for name in ("claude", "gemini", "codex"):
        path = _get_cli(name)
        if path:
            logger.info("Found %s CLI: %s", name, path)
        else:
            logger.warning("%s CLI not found", name)
    yield

app = FastAPI(title="LLM Gateway", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/providers")
async def providers():
    """List available providers and their status."""
    result = {}
    for name in ("claude", "gemini", "codex"):
        result[name] = {"available": _get_cli(name) is not None}
    # Check Ollama
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
    if req.provider == "ollama":
        return await _call_ollama(req.system_prompt, req.user_message, req.model, req.timeout or 300)
    else:
        return await _call_cli(req.provider, req.system_prompt, req.user_message, req.model, req.timeout or 300)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
