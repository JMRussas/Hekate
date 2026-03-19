"""Hermes handler — task execution via CLI providers with TDD and narration.

Receives dispatch_command events and:
  1. Sets task → running
  2. Resolves working directory
  3. Executes CLI with narration callbacks
  4. Captures output, cost, tokens
  5. Runs TDD gate (for code tasks)
  6. Emits worker_event (completed/failed)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from typing import Any

from gods.pipeline import Event, Emit

logger = logging.getLogger("gods.handlers.hermes")


# ---------------------------------------------------------------------------
# CLI command building
# ---------------------------------------------------------------------------

def _build_cli_command(
    provider: str,
    prompt: str,
    cwd: str,
    allowed_tools: str = "Edit,Write,Read,Glob,Grep,Bash(*)",
) -> tuple[list[str], str]:
    """Build CLI command args and stdin text for a provider.

    Returns (cmd_args, stdin_text).
    """
    if provider == "claude_code":
        claude_bin = shutil.which("claude") or "claude"
        cmd = [
            claude_bin, "-p",
            "--verbose",
            "--output-format", "stream-json",
            "--allowedTools", allowed_tools,
        ]
        return cmd, prompt

    elif provider == "gemini_cli":
        gemini_bin = shutil.which("gemini") or "gemini"
        cmd = [
            gemini_bin, "-p",
            "--approval-mode", "yolo",
            "-o", "text",
        ]
        return cmd, prompt

    elif provider == "codex_cli":
        codex_bin = shutil.which("codex") or "codex"
        cmd = [
            codex_bin, "exec",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        return cmd, prompt

    elif provider == "ollama":
        # Ollama uses HTTP, not CLI — handled separately
        raise ValueError("Ollama uses HTTP API, not CLI subprocess")

    else:
        raise ValueError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Stream event parsing (Claude Code stream-json)
# ---------------------------------------------------------------------------

def _parse_stream_event(line: str) -> dict | None:
    """Parse a single line of Claude Code stream-json output.

    Returns a narration-compatible event dict or None.
    """
    try:
        data = json.loads(line.strip())
    except (json.JSONDecodeError, ValueError):
        return None

    event_type = data.get("type", "")

    if event_type == "assistant":
        # Extract text content
        content = data.get("message", {}).get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        if texts:
            return {"type": "narration", "text": " ".join(texts)}

    elif event_type == "tool_use":
        return {
            "type": "tool_call",
            "tool": data.get("name", "unknown"),
            "args": data.get("input", {}),
        }

    elif event_type == "tool_result":
        return {
            "type": "tool_result",
            "tool": data.get("name", "unknown"),
            "output": str(data.get("output", ""))[:500],
        }

    elif event_type == "error":
        return {
            "type": "error",
            "text": data.get("error", {}).get("message", str(data)),
        }

    return None


# ---------------------------------------------------------------------------
# CLI execution (mocked in tests)
# ---------------------------------------------------------------------------

async def _run_cli(
    *,
    provider: str,
    prompt: str,
    cwd: str,
    timeout: int = 600,
    on_event: Any = None,
) -> dict:
    """Execute a CLI provider and return results.

    This is the real implementation — calls subprocess.
    Mocked in tests.

    Returns:
        {output, cost_usd, prompt_tokens, completion_tokens, model_used, narration}
    """
    import asyncio

    cmd, stdin_text = _build_cli_command(provider, prompt, cwd)
    narration: list[dict] = []

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        limit=10 * 1024 * 1024,
    )

    proc.stdin.write(stdin_text.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    output_lines: list[str] = []
    try:
        async with asyncio.timeout(timeout):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if not decoded:
                    continue

                if provider == "claude_code":
                    event = _parse_stream_event(decoded)
                    if event:
                        narration.append(event)
                        if on_event:
                            on_event(event)
                    # Collect text content for final output
                    try:
                        data = json.loads(decoded)
                        if data.get("type") == "result":
                            result_text = data.get("result", "")
                            if result_text:
                                output_lines.append(result_text)
                    except json.JSONDecodeError:
                        output_lines.append(decoded)
                else:
                    output_lines.append(decoded)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"CLI timed out after {timeout}s")

    await proc.wait()
    stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")

    if proc.returncode != 0 and not output_lines:
        raise RuntimeError(f"CLI exited {proc.returncode}: {stderr[:500]}")

    output = "\n".join(output_lines)

    return {
        "output": output,
        "cost_usd": 0.0,  # Parsed from stream in real implementation
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "model_used": provider,
        "narration": narration,
    }


# ---------------------------------------------------------------------------
# TDD gate
# ---------------------------------------------------------------------------

_TEST_FILE_PATTERN = re.compile(r'test[_/]|_test\.py|\.test\.|spec\.|\.spec\.', re.IGNORECASE)
_TEST_FAIL_PATTERN = re.compile(r'(\d+)\s+(failed|errors?|FAIL)', re.IGNORECASE)
_TEST_PASS_PATTERN = re.compile(r'(tests?\s+pass|all\s+pass|\d+\s+passed|✓|PASSED)', re.IGNORECASE)


async def hermes_tdd_gate(
    *,
    task_id: str,
    output_text: str,
    task_type: str,
    db: Any,
) -> dict:
    """Check if TDD was followed for code tasks.

    Returns {passed: bool, reason: str}.
    """
    # Non-code tasks skip TDD
    if task_type not in ("code", "integration", "refactor"):
        return {"passed": True, "reason": "TDD not required for non-code tasks"}

    if not output_text:
        return {"passed": False, "reason": "No output to evaluate"}

    # Check if test files were mentioned
    has_tests = bool(_TEST_FILE_PATTERN.search(output_text))
    if not has_tests:
        return {
            "passed": False,
            "reason": "No test files mentioned in output. TDD requires writing tests first.",
        }

    # Check for test failures
    has_failures = bool(_TEST_FAIL_PATTERN.search(output_text))
    has_passes = bool(_TEST_PASS_PATTERN.search(output_text))

    if has_failures and not has_passes:
        return {
            "passed": False,
            "reason": "Tests are failing. Implementation must make all tests pass.",
        }

    if has_failures and has_passes:
        # Mixed — some pass, some fail
        return {
            "passed": False,
            "reason": "Some tests are still failing. All tests must pass.",
        }

    return {"passed": True, "reason": "Tests present and passing"}


# ---------------------------------------------------------------------------
# hermes_execute — main handler
# ---------------------------------------------------------------------------

async def hermes_execute(event: Event, db) -> list[Emit] | None:
    """Execute a task via CLI provider.

    Receives dispatch_command, runs the CLI, captures output,
    emits worker_event (completed/failed).
    """
    task_id = event.payload.get("task_id")
    project_id = event.payload.get("project_id")
    provider = event.payload.get("provider", "claude_code")

    if not task_id:
        return [Emit("hermes_error", {"error": "No task_id"}, source="hermes")]

    # Fetch task
    row = await db.fetchone(
        "SELECT id, title, description, task_type, status, model_tier, context_json "
        "FROM tasks WHERE id = $1",
        (task_id,),
    )
    if not row:
        return [Emit("hermes_error", {
            "error": f"Task {task_id} not found",
            "task_id": task_id,
        }, source="hermes")]

    status = row["status"]
    title = row["title"]
    description = row.get("description", "")
    task_type = row.get("task_type", "code")
    context_json = row.get("context_json")

    # Guard: only execute pending or queued tasks
    if status not in ("pending", "queued"):
        logger.info("Hermes: task %s is %s, skipping", task_id[:8], status)
        return [Emit("worker_event", {
            "task_id": task_id,
            "project_id": project_id,
            "status": "skipped",
            "reason": f"Task is {status}, expected pending",
        }, source="hermes")]

    # Set task → running
    await db.execute_write(
        "UPDATE tasks SET status = $1, started_at = $2, updated_at = $3 WHERE id = $4",
        ("running", time.time(), time.time(), task_id),
    )

    # Resolve working directory
    proj_row = await db.fetchone(
        "SELECT repo_path FROM projects WHERE id = $1", (project_id,)
    )
    cwd = proj_row.get("repo_path", ".") if proj_row else "."

    # Build prompt
    prompt = f"# Task: {title}\n\n{description or ''}"
    if context_json and context_json != "{}":
        try:
            ctx = json.loads(context_json) if isinstance(context_json, str) else context_json
            if isinstance(ctx, dict):
                if ctx.get("verification_feedback"):
                    prompt += f"\n\n# Previous feedback:\n{ctx['verification_feedback']}"
                if ctx.get("prompt_guidance"):
                    prompt += f"\n\n# Guidance:\n{ctx['prompt_guidance']}"
        except (json.JSONDecodeError, TypeError):
            pass

    emits: list[Emit] = []

    try:
        result = await _run_cli(
            provider=provider,
            prompt=prompt,
            cwd=cwd,
        )

        output = result.get("output", "")
        cost = result.get("cost_usd", 0.0)
        prompt_tokens = result.get("prompt_tokens", 0)
        completion_tokens = result.get("completion_tokens", 0)
        model_used = result.get("model_used", provider)
        narration = result.get("narration", [])

        # Emit narration events
        for n in narration:
            if n.get("type") in ("narration", "tool_call"):
                emits.append(Emit("narration", {
                    "task_id": task_id,
                    "project_id": project_id,
                    **n,
                }, source="hermes"))

        # Check for empty output
        if not output or not output.strip():
            await db.execute_write(
                "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                ("failed", "Empty output from CLI", time.time(), task_id),
            )
            emits.append(Emit("worker_event", {
                "task_id": task_id,
                "project_id": project_id,
                "status": "failed",
                "error": "Empty output from CLI executor",
            }, source="hermes"))
            return emits

        # Run TDD gate for code tasks
        tdd_result = await hermes_tdd_gate(
            task_id=task_id,
            output_text=output,
            task_type=task_type,
            db=db,
        )
        tdd_warning = None if tdd_result["passed"] else tdd_result["reason"]

        # Store result
        await db.execute_write(
            "UPDATE tasks SET status = $1, output_text = $2, cost_usd = $3, "
            "prompt_tokens = $4, completion_tokens = $5, model_used = $6, "
            "completed_at = $7, updated_at = $8 WHERE id = $9",
            ("completed", output, cost, prompt_tokens, completion_tokens,
             model_used, time.time(), time.time(), task_id),
        )

        worker_payload = {
            "task_id": task_id,
            "project_id": project_id,
            "status": "completed",
            "cost_usd": cost,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model_used": model_used,
        }
        if tdd_warning:
            worker_payload["tdd_warning"] = tdd_warning

        emits.append(Emit("worker_event", worker_payload, source="hermes"))

        return emits

    except asyncio.TimeoutError as e:
        error_msg = f"Timeout: {e}" if str(e) else "CLI execution timed out"
        logger.error("Hermes: task %s timed out", task_id[:8])

        await db.execute_write(
            "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
            ("failed", error_msg, time.time(), task_id),
        )

        emits.append(Emit("worker_event", {
            "task_id": task_id,
            "project_id": project_id,
            "status": "failed",
            "error": error_msg,
            "timeout": True,
        }, source="hermes"))

        return emits

    except Exception as e:
        error_msg = str(e)
        logger.error("Hermes: task %s failed: %s", task_id[:8], error_msg)

        await db.execute_write(
            "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
            ("failed", error_msg, time.time(), task_id),
        )

        emits.append(Emit("worker_event", {
            "task_id": task_id,
            "project_id": project_id,
            "status": "failed",
            "error": error_msg,
        }, source="hermes"))

        return emits
