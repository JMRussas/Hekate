#  Orchestration Engine - Generic CLI Executor
#
#  Runs tasks via Gemini CLI or Codex CLI subprocesses. Both CLIs manage
#  their own tools (MCP servers, file I/O, shell) so no tool_registry is
#  needed — just a prompt and a working directory.
#
#  Unlike Claude Code, these CLIs don't emit stream-json events, so output
#  is collected as plain text after completion.
#
#  Depends on: config.py, services/cli_common.py, services/llm_router.py
#  Used by:    services/task_lifecycle.py

import asyncio
import logging

from backend.config import cfg
from backend.models.enums import ModelTier
from backend.services.cli_common import build_prompt_for_provider, is_process_crash, resolve_cwd
from backend.services.llm_router import _resolve_cmd
from backend.services.model_router import get_model_id

logger = logging.getLogger("orchestration.executor")

# Timeout for CLI tasks (seconds). Defaults to 10 minutes.
CLI_TIMEOUT = int(cfg("cli_executor.timeout_seconds", 600))

# Max retries for process crashes (access violations, OOM). Logical failures
# are NOT retried here — task_lifecycle handles those via retry_count.
CLI_CRASH_RETRIES = int(cfg("cli_executor.crash_retries", 2))


async def run_gemini_cli_task(
    *,
    task_row,
    db,
    budget,
    progress,
) -> dict:
    """Execute a task via the Gemini CLI."""
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
    """Execute a task via the Codex CLI."""
    return await _run_cli_task(
        provider="codex",
        task_row=task_row,
        db=db,
        budget=budget,
        progress=progress,
    )


def _build_cmd_args(provider: str) -> tuple[list[str], str]:
    """Build CLI command args for the given provider.

    Returns (cmd_args, model_used).
    """
    if provider == "gemini":
        cmd = _resolve_cmd("gemini")
        if not cmd:
            raise RuntimeError("Gemini CLI not found on PATH or in npm global bin")
        model = get_model_id(ModelTier.GEMINI_CLI)
        # --approval-mode yolo auto-approves all tool calls in headless mode.
        # -o text reduces output overhead (no rich formatting).
        cmd_args = [cmd, "-p", "", "--approval-mode", "yolo", "-o", "text"]
        if model:
            cmd_args.extend(["-m", model])
    elif provider == "codex":
        cmd = _resolve_cmd("codex")
        if not cmd:
            raise RuntimeError("Codex CLI not found on PATH or in npm global bin")
        model = get_model_id(ModelTier.CODEX_CLI)
        # --full-auto + --sandbox workspace-write is broken on Windows (v0.112.0) —
        # always resolves to read-only. Use bypass flag instead since the orchestrator
        # controls the working directory and we trust the agent output.
        cmd_args = [cmd, "exec", "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            cmd_args.extend(["--model", model])
    else:
        raise ValueError(f"Unknown CLI provider: {provider}")

    return cmd_args, model or f"{provider}-cli"


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
    Process crashes (access violations, OOM) are retried up to
    CLI_CRASH_RETRIES times before propagating the error.
    """
    task_id = task_row["id"]
    project_id = task_row["project_id"]

    prompt = build_prompt_for_provider(task_row, provider)
    cwd = await resolve_cwd(db, project_id)
    cmd_args, model_used = _build_cmd_args(provider)

    await progress.push_event(
        project_id, "task_output",
        f"Starting {provider} CLI execution",
        task_id=task_id,
    )

    last_error = None
    for attempt in range(1 + CLI_CRASH_RETRIES):
        if attempt > 0:
            # Back off before retry — give OS time to reclaim resources
            delay = 5 * attempt
            logger.info(
                "%s CLI process crashed (attempt %d/%d), retrying in %ds for task %s",
                provider, attempt, CLI_CRASH_RETRIES, delay, task_id,
            )
            await progress.push_event(
                project_id, "task_output",
                f"{provider} CLI crashed, retrying ({attempt}/{CLI_CRASH_RETRIES})...",
                task_id=task_id,
            )
            await asyncio.sleep(delay)

        stdout_text, stderr_text, returncode = await _exec_process(
            cmd_args, prompt, cwd, provider, task_id,
        )

        # Process crash — retry if we have attempts left
        if is_process_crash(returncode) and not stdout_text:
            last_error = (
                f"{provider} CLI crashed (exit {returncode}) "
                f"on attempt {attempt + 1}/{1 + CLI_CRASH_RETRIES}"
            )
            continue

        # Logical failure (non-zero exit, no output) — don't retry here
        if returncode != 0 and not stdout_text:
            raise RuntimeError(
                f"{provider} CLI failed (exit {returncode}): {stderr_text[:500]}"
            )

        # Silent failure — clean exit but empty output (common with codex_cli)
        if returncode == 0 and not stdout_text.strip():
            if attempt < CLI_CRASH_RETRIES:
                logger.warning(
                    "%s CLI returned empty output (exit 0) on attempt %d/%d for task %s, retrying",
                    provider, attempt + 1, 1 + CLI_CRASH_RETRIES, task_id,
                )
                last_error = (
                    f"{provider} CLI produced empty output (exit 0) "
                    f"on attempt {attempt + 1}/{1 + CLI_CRASH_RETRIES}"
                )
                continue
            raise RuntimeError(
                f"{provider} CLI produced empty output after {1 + CLI_CRASH_RETRIES} attempts. "
                f"stderr: {stderr_text[:500]}"
            )

        # Success — got output
        preview = stdout_text[:200] + "..." if len(stdout_text) > 200 else stdout_text
        await progress.push_event(
            project_id, "task_output", preview, task_id=task_id,
        )

        return {
            "output": stdout_text,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "model_used": model_used,
        }

    # All crash retries exhausted
    raise RuntimeError(last_error or f"{provider} CLI crashed after all retries")


async def _exec_process(
    cmd_args: list[str],
    prompt: str,
    cwd: str | None,
    provider: str,
    task_id: str,
) -> tuple[str, str, int]:
    """Run a CLI subprocess and return (stdout, stderr, returncode)."""
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
        return f"[{provider} CLI timed out after {CLI_TIMEOUT}s]", "", 0
    except Exception:
        proc.kill()
        await proc.wait()
        raise

    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    return stdout_text, stderr_text, proc.returncode
