#  Orchestration Engine - LLM Router
#
#  Routes LLM calls through CLI providers (subscription billing) instead of
#  the Anthropic API. Supports Claude, Gemini, Codex CLIs and Ollama HTTP.
#
#  Depends on: backend/config.py
#  Used by:    planner.py

import asyncio
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Optional

import httpx

from backend.config import cfg

logger = logging.getLogger("orchestration.llm_router")

# Provider preference order for planning (complex reasoning tasks)
_PLANNING_PROVIDERS = ["gemini", "claude", "codex"]

# Provider preference order for simple tasks (verification, extraction)
_SIMPLE_PROVIDERS = ["gemini", "ollama", "codex"]


@dataclass
class LLMResponse:
    """Response from an LLM call."""

    text: str
    provider: str
    model: Optional[str] = None
    # CLI providers don't expose token counts — cost is $0 on subscription
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


def _resolve_cmd(name: str) -> Optional[str]:
    """Resolve a CLI command name to full path (handles .cmd on Windows).

    Falls back to the npm global prefix bin directory when the command
    isn't on PATH — common in Git Bash / MSYS2 environments on Windows
    where the npm global bin isn't inherited.

    Returns None if the command is not found anywhere.
    """
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if sys.platform == "win32":
        # npm global bin often missing from Git Bash PATH
        npm_bin = os.path.join(os.environ.get("APPDATA", ""), "npm")
        for ext in (".cmd", ".exe", ""):
            candidate = os.path.join(npm_bin, f"{name}{ext}")
            if os.path.isfile(candidate):
                return candidate
    return None


async def _call_cli(provider: str, system_prompt: str, user_message: str,
                    model: Optional[str] = None) -> LLMResponse:
    """Call a CLI provider with system prompt and user message.

    Pipes the prompt via stdin to avoid Windows command line length limits.
    All CLIs support reading prompts from stdin.
    """
    # CLI providers don't support separate system/user roles — flatten into one prompt.
    # TODO: Claude CLI supports --system-prompt flag; use it when available to preserve
    # role separation. Gemini and Codex CLIs have no equivalent yet.
    full_prompt = f"{system_prompt}\n\n---\n\n{user_message}"

    cli_names = {"claude": "claude", "codex": "codex", "gemini": "gemini"}
    binary = cli_names.get(provider)
    if not binary:
        raise ValueError(f"Unknown CLI provider: {provider}")

    resolved = _resolve_cmd(binary)
    if not resolved:
        raise FileNotFoundError(f"{provider} CLI ({binary}) not found on PATH or in npm global bin")

    if provider == "claude":
        # Claude: -p is --print (non-interactive mode), reads prompt from stdin
        cmd_args = [resolved, "-p", "--output-format", "text"]
    elif provider == "codex":
        cmd_args = [resolved, "exec"]
        if model:
            cmd_args.extend(["--model", model])
    elif provider == "gemini":
        # Gemini: -p "" triggers non-interactive mode, stdin is prepended to prompt
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
    stdout_text = stdout.decode().strip()
    stderr_text = stderr.decode().strip()

    if proc.returncode != 0:
        raise RuntimeError(f"{provider} CLI failed (exit {proc.returncode}): {stderr_text}")

    return LLMResponse(text=stdout_text, provider=provider, model=model)


async def _call_ollama(system_prompt: str, user_message: str,
                       model: Optional[str] = None) -> LLMResponse:
    """Call Ollama HTTP API."""
    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
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


async def call_llm(
    system_prompt: str,
    user_message: str,
    *,
    provider: Optional[str] = None,
    providers: Optional[list[str]] = None,
    model: Optional[str] = None,
    task_type: str = "planning",
) -> LLMResponse:
    """Route an LLM call through CLI providers with fallback.

    Args:
        system_prompt: System instructions for the LLM.
        user_message: The user/task content.
        provider: Explicit single provider to use (no fallback).
        providers: Ordered list of providers to try (with fallback).
        model: Optional model override for the provider.
        task_type: "planning" or "simple" — determines default provider order.

    Returns:
        LLMResponse with the text output.

    Raises:
        RuntimeError if all providers fail.
    """
    if provider:
        chain = [provider]
    elif providers:
        chain = providers
    elif task_type == "simple":
        chain = list(_SIMPLE_PROVIDERS)
    else:
        chain = list(_PLANNING_PROVIDERS)

    errors = []
    for p in chain:
        try:
            logger.info("Calling %s for %s task", p, task_type)
            if p == "ollama":
                return await _call_ollama(system_prompt, user_message, model)
            else:
                return await _call_cli(p, system_prompt, user_message, model)
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
