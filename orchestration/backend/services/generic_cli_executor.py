#  Orchestration Engine - Generic CLI Executor
#
#  Runs tasks via Gemini CLI or Codex CLI subprocesses. Both CLIs manage
#  their own tools (MCP servers, file I/O, shell) so no tool_registry is
#  needed — just a prompt and a working directory.
#
#  Unlike Claude Code, these CLIs don't emit stream-json events, so output
#  is collected as plain text after completion.
#
#  Depends on: config.py, services/llm_router.py (_resolve_cmd)
#  Used by:    services/task_lifecycle.py

import asyncio
import json
import logging
import os
import time

from backend.config import cfg
from backend.services.llm_router import _resolve_cmd
from backend.services.model_router import get_model_id
from backend.models.enums import ModelTier

logger = logging.getLogger("orchestration.executor")

# Timeout for CLI tasks (seconds). Defaults to 10 minutes.
CLI_TIMEOUT = int(cfg("cli_executor.timeout_seconds", 600))

# Gemini allowed tools — passed as --allowed-tools array entries
GEMINI_ALLOWED_TOOLS = cfg(
    "gemini_cli.allowed_tools",
    "Edit,Write,Read,Glob,Grep,"
    "Bash(git *),Bash(dotnet build *),Bash(dotnet test *),Bash(dotnet publish *),"
    "Bash(dotnet run *),Bash(npm *),Bash(python *),Bash(curl *),Bash(ls *),Bash(find *),"
    "mcp__hecate__*,mcp__ollama__*",
)

# Codex allowed tools — not passed as CLI flags (Codex uses --full-auto + sandbox),
# but kept for config parity and potential future use
CODEX_ALLOWED_TOOLS = cfg(
    "codex_cli.allowed_tools",
    "Edit,Write,Read,Glob,Grep,"
    "Bash(git *),Bash(dotnet build *),Bash(dotnet test *),Bash(dotnet publish *),"
    "Bash(dotnet run *),Bash(npm *),Bash(python *),Bash(curl *),Bash(ls *),Bash(find *),"
    "mcp__hecate__*,mcp__ollama__*",
)


async def run_gemini_cli_task(
    *,
    task_row,
    db,
    budget,
    progress,
) -> dict:
    """Execute a task via the Gemini CLI.

    Shells out to ``gemini -p "" < prompt``.
    Gemini CLI handles its own tools (MCP servers, file operations, shell).

    Returns:
        dict with keys: output, prompt_tokens, completion_tokens,
        cost_usd, model_used.
    """
    return await _run_cli_task(
        provider="gemini",
        task_row=task_row,
        db=db,
        budget=budget,
        progress=progress,
    )


async def run_codex_cli_task(
    *,
    task_row,
    db,
    budget,
    progress,
) -> dict:
    """Execute a task via the Codex CLI.

    Shells out to ``codex exec < prompt``.
    Codex CLI handles its own tools (file operations, shell, web search).

    Returns:
        dict with keys: output, prompt_tokens, completion_tokens,
        cost_usd, model_used.
    """
    return await _run_cli_task(
        provider="codex",
        task_row=task_row,
        db=db,
        budget=budget,
        progress=progress,
    )


async def _run_cli_task(
    *,
    provider: str,
    task_row,
    db,
    budget,
    progress,
) -> dict:
    """Generic CLI task execution for gemini and codex.

    Both CLIs read prompts from stdin and write results to stdout.
    No stream-json — output collected after completion.
    """
    task_id = task_row["id"]
    project_id = task_row["project_id"]

    prompt = _build_prompt(task_row)
    cwd = await _resolve_cwd(db, project_id)

    if provider == "gemini":
        cmd = _resolve_cmd("gemini")
        if not cmd:
            raise RuntimeError("Gemini CLI not found on PATH or in npm global bin")
        model = get_model_id(ModelTier.GEMINI_CLI)
        # --approval-mode yolo auto-approves all tool calls in headless mode.
        # --allowed-tools specifies which tools are available (array flag).
        cmd_args = [cmd, "-p", "", "--approval-mode", "yolo"]
        if model:
            cmd_args.extend(["-m", model])
        # Pass allowed tools as individual --allowed-tools entries
        for tool in GEMINI_ALLOWED_TOOLS.split(","):
            tool = tool.strip()
            if tool:
                cmd_args.extend(["--allowed-tools", tool])
    elif provider == "codex":
        cmd = _resolve_cmd("codex")
        if not cmd:
            raise RuntimeError("Codex CLI not found on PATH or in npm global bin")
        model = get_model_id(ModelTier.CODEX_CLI)
        # --full-auto enables workspace writes + auto-approval (no interactive prompts)
        # --sandbox workspace-write allows file modifications in the working directory
        cmd_args = [cmd, "exec", "--full-auto", "--sandbox", "workspace-write"]
        if model:
            cmd_args.extend(["--model", model])
    else:
        raise ValueError(f"Unknown CLI provider: {provider}")

    model_used = model or f"{provider}-cli"

    await progress.push_event(
        project_id, "task_output",
        f"Starting {provider} CLI execution",
        task_id=task_id,
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=CLI_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "%s CLI timed out after %ds for task %s",
            provider, CLI_TIMEOUT, task_id,
        )
        proc.kill()
        await proc.wait()
        return {
            "output": f"[{provider} CLI timed out after {CLI_TIMEOUT}s]",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "model_used": model_used,
        }
    except Exception:
        proc.kill()
        await proc.wait()
        raise

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0 and not stdout_text:
        raise RuntimeError(
            f"{provider} CLI failed (exit {proc.returncode}): {stderr_text[:500]}"
        )

    # Push completion event
    preview = stdout_text[:200] + "..." if len(stdout_text) > 200 else stdout_text
    await progress.push_event(
        project_id, "task_output", preview, task_id=task_id,
    )

    # CLI is $0 on subscription — track for accounting only
    return {
        "output": stdout_text,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "model_used": model_used,
    }


def _build_prompt(task_row) -> str:
    """Build the full prompt from task description and context."""
    parts = []

    system_prompt = task_row["system_prompt"] or ""
    if system_prompt:
        parts.append(system_prompt)

    context_json = task_row["context_json"] or "[]"
    context = json.loads(context_json) if isinstance(context_json, str) else context_json
    for ctx in context:
        ctx_type = ctx.get("type", "context")
        content = ctx.get("content", "")
        if content:
            parts.append(f"<{ctx_type}>\n{content}\n</{ctx_type}>")

    parts.append(task_row["description"])
    return "\n\n".join(parts)


async def _resolve_cwd(db, project_id: str) -> str | None:
    """Look up the project's repo_path for use as working directory."""
    try:
        row = await db.fetchone(
            "SELECT repo_path FROM projects WHERE id = ?",
            (project_id,),
        )
        if row and row["repo_path"]:
            return row["repo_path"]
    except Exception as e:
        logger.debug("Failed to resolve repo_path for project %s: %s", project_id, e)
    return None
