#  Orchestration Engine - Claude Code CLI Executor
#
#  Runs a single task via the Claude Code CLI subprocess with stream-json
#  output. Claude Code manages its own tools (MCP servers, file I/O, bash)
#  so no tool_registry is needed — just a prompt and a working directory.
#  Cost is $0 on unlimited subscription plans.
#
#  Depends on: config.py, services/model_router.py, services/progress.py,
#              services/llm_router.py (_resolve_cmd)
#  Used by:    services/task_lifecycle.py

import asyncio
import json
import logging
import os
import time

from backend.config import cfg
from backend.services.llm_router import _resolve_cmd

logger = logging.getLogger("orchestration.executor")

# Timeout for Claude Code CLI (seconds). Defaults to 10 minutes.
CLAUDE_CODE_TIMEOUT = int(cfg("claude_code.timeout_seconds", 600))

# Tools that Claude Code is allowed to use without user approval in headless mode.
# Without this, -p mode requires interactive approval for writes — making it useless
# for code generation tasks.
CLAUDE_CODE_ALLOWED_TOOLS = cfg(
    "claude_code.allowed_tools",
    "Edit,Write,Read,Glob,Grep,Bash(git *),Bash(dotnet *),Bash(npm *),Bash(python *)",
)

# Max prompt length before switching to stdin pipe (Windows cmd line limit)
_MAX_CMD_PROMPT_LEN = 4000


async def run_claude_code_task(
    *,
    task_row,
    db,
    budget,
    progress,
) -> dict:
    """Execute a task via the Claude Code CLI.

    Shells out to ``claude -p "prompt" --output-format stream-json``.
    Claude Code handles its own tools (MCP servers, file operations, bash).
    Streams progress events back through the ProgressManager SSE pipeline.

    Args:
        task_row: Task database row.
        db: Database instance (for looking up project repo_path).
        budget: BudgetManager instance.
        progress: ProgressManager instance.

    Returns:
        dict with keys: output, prompt_tokens, completion_tokens,
        cost_usd, model_used.
    """
    task_id = task_row["id"]
    project_id = task_row["project_id"]

    # Build the prompt from task description + context
    prompt = _build_prompt(task_row)

    # Resolve working directory from project repo_path
    cwd = await _resolve_cwd(db, project_id)

    # Resolve the claude CLI command
    claude_cmd = _resolve_cmd("claude")

    # Build command args
    # --allowedTools grants write access in headless mode. Without it, Claude Code
    # in -p mode defaults to read-only and asks for interactive approval — which
    # silently fails in a subprocess, producing plans instead of code.
    cmd_args = [
        claude_cmd, "-p",
        "--verbose",
        "--output-format", "stream-json",
        "--allowedTools", CLAUDE_CODE_ALLOWED_TOOLS,
    ]

    # Strip Claude session env vars so the subprocess doesn't detect a nested session
    clean_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("CLAUDE") and k != "CLAUDECODE"}

    # Launch subprocess — pipe prompt via stdin to avoid Windows cmd length limits
    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=clean_env,
    )

    text_parts: list[str] = []
    total_cost_usd = 0.0
    model_used = "claude-code-cli"
    total_prompt_tokens = 0
    total_completion_tokens = 0

    try:
        # Send prompt via stdin (avoids cmd line length limits)
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        # Stream stdout line by line, parsing stream-json events
        async def read_stream():
            nonlocal total_cost_usd, model_used
            nonlocal total_prompt_tokens, total_completion_tokens

            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=CLAUDE_CODE_TIMEOUT,
                )
                if not line:
                    break

                line_text = line.decode("utf-8", errors="replace").strip()
                if not line_text:
                    continue

                try:
                    event = json.loads(line_text)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON line from claude: %s", line_text[:200])
                    continue

                await _handle_stream_event(
                    event, task_id, project_id, progress, text_parts,
                )

                # Extract usage from result event
                if event.get("type") == "result":
                    result_data = event.get("result", "")
                    if isinstance(result_data, str):
                        text_parts.append(result_data)

                    total_cost_usd = event.get("total_cost", 0.0) or 0.0
                    model_used = event.get("model", "claude-code-cli")

                    usage = event.get("usage", {})
                    total_prompt_tokens = usage.get("input_tokens", 0)
                    total_completion_tokens = usage.get("output_tokens", 0)

        await read_stream()
        await proc.wait()

    except asyncio.TimeoutError:
        logger.warning("Claude Code timed out after %ds for task %s", CLAUDE_CODE_TIMEOUT, task_id)
        proc.kill()
        await proc.wait()
        text_parts.append(f"\n[Claude Code timed out after {CLAUDE_CODE_TIMEOUT}s]")

    except Exception:
        proc.kill()
        await proc.wait()
        raise

    stderr_text = ""
    if proc.stderr:
        stderr_bytes = await proc.stderr.read()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0 and not text_parts:
        raise RuntimeError(
            f"Claude Code CLI failed (exit {proc.returncode}): {stderr_text[:500]}"
        )

    # Record spend (CLI is $0 on subscription, but track for accounting)
    if total_cost_usd > 0:
        await budget.record_spend(
            cost_usd=total_cost_usd,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            provider="claude_code_cli",
            model=model_used,
            purpose="execution",
            project_id=project_id,
            task_id=task_id,
        )

    output = "\n".join(text_parts).strip()

    return {
        "output": output,
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "cost_usd": total_cost_usd,
        "model_used": model_used,
    }


def _build_prompt(task_row) -> str:
    """Build the full prompt from task description and context."""
    parts = []

    # System prompt (sqlite3.Row doesn't support .get(), use [] with fallback)
    system_prompt = task_row["system_prompt"] or ""
    if system_prompt:
        parts.append(system_prompt)

    # Context from dependencies
    context_json = task_row["context_json"] or "[]"
    context = json.loads(context_json) if isinstance(context_json, str) else context_json
    for ctx in context:
        ctx_type = ctx.get("type", "context")
        content = ctx.get("content", "")
        if content:
            parts.append(f"<{ctx_type}>\n{content}\n</{ctx_type}>")

    # Task description (always last — this is the main instruction)
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


async def _handle_stream_event(
    event: dict,
    task_id: str,
    project_id: str,
    progress,
    text_parts: list[str],
):
    """Process a single stream-json event from Claude Code.

    Maps Claude Code stream events to Nobody progress events for the dashboard.
    """
    event_type = event.get("type", "")

    if event_type == "assistant":
        # Assistant message — extract text content
        message = event.get("message", {})
        content_blocks = message.get("content", [])
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)
                    # Push a truncated preview as progress
                    preview = text[:200] + "..." if len(text) > 200 else text
                    await progress.push_event(
                        project_id, "task_output", preview, task_id=task_id,
                    )

    elif event_type == "tool_use":
        # Claude Code is calling a tool (file edit, bash, MCP, etc.)
        tool_name = event.get("name", event.get("tool", "unknown"))
        await progress.push_event(
            project_id, "tool_call", f"Using {tool_name}",
            task_id=task_id, tool=tool_name,
        )

    elif event_type == "tool_result":
        # Tool completed — don't push full output (could be huge)
        pass

    elif event_type == "error":
        error_msg = event.get("error", {})
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message", str(error_msg))
        logger.warning("Claude Code error event for task %s: %s", task_id, error_msg)
        await progress.push_event(
            project_id, "task_error", str(error_msg)[:500],
            task_id=task_id,
        )
