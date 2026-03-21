#  Orchestration Engine - LLM Router
#
#  Routes LLM calls through the LLM Gateway HTTP service (port 5210).
#  The gateway runs as the user and has CLI OAuth tokens.
#  Falls back to direct CLI/Ollama calls if gateway is unavailable.
#
#  Depends on: backend/config.py, backend/services/prompt_renderer.py
#  Used by:    planner.py, verifier.py, knowledge_extractor.py,
#              sentinel/reasoner.py

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import httpx

from backend.config import LLM_GATEWAY_URL, OLLAMA_URL, cfg

if TYPE_CHECKING:
    from backend.services.prompt_renderer import PromptSpec

logger = logging.getLogger("orchestration.llm_router")

_GATEWAY_URL = LLM_GATEWAY_URL


def _get_planning_providers() -> list[str]:
    return cfg("llm.planning_providers", ["gemini", "claude", "ollama"])


def _get_simple_providers() -> list[str]:
    return cfg("llm.simple_providers", ["gemini", "ollama", "claude"])


@dataclass
class LLMResponse:
    """Response from an LLM call."""

    text: str
    provider: str
    model: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


async def _call_gateway(provider: str, system_prompt: str, user_message: str,
                        model: Optional[str] = None) -> LLMResponse:
    """Call the LLM Gateway HTTP service."""
    payload = {
        "provider": provider,
        "system_prompt": system_prompt,
        "user_message": user_message,
    }
    if model:
        payload["model"] = model

    async with httpx.AsyncClient(timeout=330.0) as client:
        resp = await client.post(f"{_GATEWAY_URL}/v1/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()

    return LLMResponse(
        text=data["text"],
        provider=data["provider"],
        model=data.get("model"),
    )


# ---------------------------------------------------------------------------
# Fallback: direct CLI calls (used when gateway is down)
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


async def _call_cli_direct(provider: str, system_prompt: str, user_message: str,
                           model: Optional[str] = None) -> LLMResponse:
    """Direct CLI call — fallback when gateway is unavailable."""
    full_prompt = f"{system_prompt}\n\n---\n\n{user_message}"

    cli_names = {"claude": "claude", "codex": "codex", "gemini": "gemini"}
    binary = cli_names.get(provider)
    if not binary:
        raise ValueError(f"Unknown CLI provider: {provider}")

    resolved = _resolve_cmd(binary)
    if not resolved:
        raise FileNotFoundError(f"{provider} CLI ({binary}) not found on PATH")

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

    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=full_prompt.encode()), timeout=300,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        raise RuntimeError(f"{provider} CLI failed (exit {proc.returncode}): {stderr.decode().strip()}")

    return LLMResponse(text=stdout.decode().strip(), provider=provider, model=model)


async def _call_ollama_direct(system_prompt: str, user_message: str,
                              model: Optional[str] = None) -> LLMResponse:
    """Direct Ollama call — fallback when gateway is unavailable."""
    ollama_url = OLLAMA_URL
    ollama_model = model or os.environ.get("OLLAMA_MODEL", cfg("ollama.default_model", "qwen3.5:latest"))

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{ollama_url}/api/chat",
            json={"model": ollama_model, "messages": messages, "stream": False},
        )
        resp.raise_for_status()
        data = resp.json()

    text = data.get("message", {}).get("content", "")
    return LLMResponse(text=text.strip(), provider="ollama", model=ollama_model)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def call_llm(
    system_prompt: str = "",
    user_message: str = "",
    *,
    spec: PromptSpec | None = None,
    provider: Optional[str] = None,
    providers: Optional[list[str]] = None,
    model: Optional[str] = None,
    task_type: str = "planning",
) -> LLMResponse:
    """Route an LLM call through the gateway with fallback to direct calls.

    Tries the LLM Gateway first (has user CLI auth). If gateway is down,
    falls back to direct CLI/Ollama calls (may fail under NSSM).

    When ``spec`` is provided, the prompt is re-rendered for each provider
    in the fallback chain. This means a Gemini-optimized prompt automatically
    becomes a Claude-optimized prompt if Gemini fails and Claude is next.
    """
    if provider:
        chain = [provider]
    elif providers:
        chain = providers
    elif task_type == "simple":
        chain = _get_simple_providers()
    else:
        chain = _get_planning_providers()

    errors = []
    for p in chain:
        # Re-render per provider if spec is available
        if spec is not None:
            from backend.services.prompt_renderer import render_prompt
            rendered = render_prompt(spec, p)
            sys_prompt = rendered.system_prompt
            usr_msg = rendered.user_message
        else:
            sys_prompt = system_prompt
            usr_msg = user_message

        # Try gateway first
        try:
            logger.info("Calling %s via gateway for %s task", p, task_type)
            return await _call_gateway(p, sys_prompt, usr_msg, model)
        except httpx.ConnectError:
            logger.warning("LLM Gateway unavailable, falling back to direct call")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 503:
                # Provider not available on gateway — try next provider
                msg = f"{p} not available on gateway"
                logger.warning(msg)
                errors.append(msg)
                continue
            # Other HTTP errors (502 = CLI failed, 504 = timeout) — try next
            msg = f"{p} via gateway: {e.response.status_code} {e.response.text}"
            logger.warning(msg)
            errors.append(msg)
            continue
        except Exception as e:
            logger.warning("Gateway call failed for %s: %s", p, e)

        # Fallback to direct call
        try:
            logger.info("Calling %s directly for %s task", p, task_type)
            if p == "ollama":
                return await _call_ollama_direct(sys_prompt, usr_msg, model)
            else:
                return await _call_cli_direct(p, sys_prompt, usr_msg, model)
        except FileNotFoundError:
            msg = f"{p} CLI not found"
            logger.warning(msg)
            errors.append(msg)
        except asyncio.TimeoutError:
            msg = f"{p} timed out (300s)"
            logger.warning(msg)
            errors.append(msg)
        except Exception as e:
            msg = f"{p} failed: {e}"
            logger.warning(msg)
            errors.append(msg)

    raise RuntimeError(f"All providers failed: {'; '.join(errors)}")
