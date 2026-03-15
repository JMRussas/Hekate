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
from backend.services.cli_common import build_prompt, resolve_cwd
from backend.services.llm_router import _resolve_cmd

logger = logging.getLogger("orchestration.executor")

# Timeout for Claude Code CLI (seconds). Defaults to 10 minutes.
CLAUDE_CODE_TIMEOUT = int(cfg("claude_code.timeout_seconds", 600))

# Tools that Claude Code is allowed to use without user approval in headless mode.
# Without this, -p mode requires interactive approval for writes — making it useless
# for code generation tasks.
CLAUDE_CODE_ALLOWED_TOOLS = cfg(
    "claude_code.allowed_tools",
    "Edit,Write,Read,Glob,Grep,"
    "Bash(git *),Bash(dotnet build *),Bash(dotnet test *),Bash(dotnet publish *),"
    "Bash(dotnet run *),Bash(npm *),Bash(python *),Bash(curl *),Bash(ls *),Bash(find *),"
    "mcp__hekate__*,mcp__ollama__*",
)


async def run_claude_code_task(
    *,
    task_row,
    db,
    budget,
    progress,
    model: str | None = None,
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
        model: Optional model override (e.g. "sonnet", "haiku", "opus").

    Returns:
        dict with keys: output, prompt_tokens, completion_tokens,
        cost_usd, model_used.
    """
    task_id = task_row["id"]
    project_id = task_row["project_id"]

    # Build the prompt from task description + context
    prompt = build_prompt(task_row)

    # Resolve working directory from project repo_path
    cwd = await resolve_cwd(db, project_id)

    # Resolve the claude CLI command
    claude_cmd = _resolve_cmd("claude")
    if not claude_cmd:
        raise RuntimeError("Claude Code CLI not found on PATH or in npm global bin")

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
    if model:
        cmd_args.extend(["--model", model])

    # Strip only the env vars that cause Claude Code to detect a nested session.
    # Keep CLAUDE_API_KEY, ANTHROPIC_API_KEY, etc. — the subprocess needs those.
    _NESTED_SESSION_VARS = {
        "CLAUDECODE", "CLAUDE_CODE_ENTRY_POINT", "CLAUDE_CODE_PARENT_SESSION_ID",
    }
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in _NESTED_SESSION_VARS}

    # Launch subprocess — pipe prompt via stdin to avoid Windows cmd length limits.
    # limit=10MB prevents "Separator is found, but chunk is longer than limit"
    # when Claude Code emits large stream-json lines (e.g., tool_result with
    # full file contents).
    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=clean_env,
        limit=10 * 1024 * 1024,  # 10 MB line buffer
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

        # Stream stdout line by line, parsing stream-json events.
        # Timeout wraps the entire stream read — not individual lines —
        # so a task that trickles output can't dodge the limit.
        async def read_stream():
            nonlocal total_cost_usd, model_used
            nonlocal total_prompt_tokens, total_completion_tokens

            while True:
                line = await proc.stdout.readline()
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

        await asyncio.wait_for(read_stream(), timeout=CLAUDE_CODE_TIMEOUT)
        await proc.wait()

    except asyncio.TimeoutError:
        logger.warning("Claude Code timed out after %ds for task %s", CLAUDE_CODE_TIMEOUT, task_id)
        proc.kill()
        await proc.wait()
        raise  # Let task_lifecycle handle as transient error → auto-retry

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

    # Always record spend for audit trail — even $0 on subscription billing.
    # This ensures every execution appears in usage_log for traceability.
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


async def _handle_stream_event(
    event: dict,
    task_id: str,
    project_id: str,
    progress,
    text_parts: list[str],
):
    """Process a single stream-json event from Claude Code.

    Maps Claude Code stream events to Hekate progress events for the dashboard.
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
