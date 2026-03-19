"""Async Hermes — non-blocking task execution.

HermesRunner manages background CLI subprocess execution:
  - handle_dispatch: receives dispatch_command, launches background task, returns immediately
  - _monitor_task: background coroutine that awaits subprocess, writes results to relay
  - cancel: kills a running task
  - shutdown: gracefully waits for in-flight tasks

The pipeline never blocks on CLI execution. Results appear in the relay
table and get picked up by mimir on the next tick.
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

logger = logging.getLogger("gods.handlers.hermes_async")

# TDD patterns (reused from hermes.py)
_TEST_FILE_PATTERN = re.compile(r'test[_/]|_test\.py|\.test\.|spec\.|\.spec\.', re.IGNORECASE)
_TEST_FAIL_PATTERN = re.compile(r'(\d+)\s+(failed|errors?|FAIL)', re.IGNORECASE)
_TEST_PASS_PATTERN = re.compile(r'(tests?\s+pass|all\s+pass|\d+\s+passed|✓|PASSED)', re.IGNORECASE)


class HermesRunner:
    """Manages async CLI task execution with in-flight tracking."""

    def __init__(
        self,
        db,
        *,
        max_concurrent: int = 4,
        default_timeout: int = 600,
    ):
        self.db = db
        self.max_concurrent = max_concurrent
        self.default_timeout = default_timeout

        # In-flight tracking: task_id → asyncio.Task
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def in_flight(self) -> set[str]:
        """Set of currently in-flight task IDs."""
        return set(self._tasks.keys())

    # ------------------------------------------------------------------
    # Handle dispatch — launch and return immediately
    # ------------------------------------------------------------------

    async def handle_dispatch(self, event: Event, db=None) -> list[Emit]:
        """Handle dispatch_command: launch background task, return immediately.

        Accepts optional db parameter for pipeline compatibility (ignored,
        uses self.db instead for background task access).
        """
        task_id = event.payload.get("task_id")
        project_id = event.payload.get("project_id")
        provider = event.payload.get("provider", "claude_code")

        if not task_id:
            return [Emit("hermes_error", {"error": "No task_id"}, source="hermes")]

        # Dedup — already running?
        if task_id in self._tasks:
            return [Emit("task_already_running", {
                "task_id": task_id,
                "project_id": project_id,
            }, source="hermes")]

        # Concurrency check
        if len(self._tasks) >= self.max_concurrent:
            return [Emit("slots_full", {
                "task_id": task_id,
                "project_id": project_id,
                "in_flight": len(self._tasks),
                "max_concurrent": self.max_concurrent,
            }, source="hermes")]

        # Fetch task
        row = await self.db.fetchone(
            "SELECT id, title, description, task_type, status, model_tier, context_json "
            "FROM tasks WHERE id = $1",
            (task_id,),
        )
        if not row:
            return [Emit("hermes_error", {
                "error": f"Task {task_id} not found",
                "task_id": task_id,
            }, source="hermes")]

        if isinstance(row, dict):
            status = row["status"]
            title = row["title"]
            description = row["description"]
            task_type = row["task_type"]
            context_json = row["context_json"]
        else:
            status = row[4]
            title = row[1]
            description = row[2]
            task_type = row[3]
            context_json = row[6]

        # Guard: only execute pending tasks
        if status != "pending":
            return [Emit("worker_event", {
                "task_id": task_id,
                "project_id": project_id,
                "status": "skipped",
                "reason": f"Task is {status}, expected pending",
            }, source="hermes")]

        # Set task → running
        await self.db.execute_write(
            "UPDATE tasks SET status = $1, started_at = $2, updated_at = $3 WHERE id = $4",
            ("running", time.time(), time.time(), task_id),
        )

        # Resolve working directory
        proj_row = await self.db.fetchone(
            "SELECT repo_path FROM projects WHERE id = $1", (project_id,))
        cwd = "."
        if proj_row:
            cwd = (proj_row[0] if isinstance(proj_row, (list, tuple))
                   else proj_row.get("repo_path", ".")) or "."

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
                    if ctx.get("review_feedback"):
                        prompt += f"\n\n# Review feedback:\n{ctx['review_feedback']}"
            except (json.JSONDecodeError, TypeError):
                pass

        # Launch background task
        bg_task = asyncio.create_task(
            self._monitor_task(
                task_id=task_id,
                project_id=project_id,
                provider=provider,
                prompt=prompt,
                cwd=cwd,
                task_type=task_type,
            ),
            name=f"hermes-{task_id}",
        )
        self._tasks[task_id] = bg_task

        # Set up cleanup callback
        bg_task.add_done_callback(lambda t: self._tasks.pop(task_id, None))

        logger.info("Hermes: launched %s via %s (in-flight: %d/%d)",
                     task_id[:8], provider, len(self._tasks), self.max_concurrent)

        return [Emit("task_running", {
            "task_id": task_id,
            "project_id": project_id,
            "provider": provider,
        }, source="hermes")]

    # ------------------------------------------------------------------
    # Background monitor — runs CLI, writes results to relay
    # ------------------------------------------------------------------

    async def _monitor_task(
        self,
        *,
        task_id: str,
        project_id: str,
        provider: str,
        prompt: str,
        cwd: str,
        task_type: str,
    ):
        """Background coroutine that runs CLI and writes results to relay.

        This ALWAYS writes a worker_event to the relay, even on internal errors.
        Tasks must never get stuck in 'running'.
        """
        try:
            result = await self._run_cli_background(
                provider=provider,
                prompt=prompt,
                cwd=cwd,
                task_id=task_id,
                project_id=project_id,
            )

            output = result.get("output", "")
            cost = result.get("cost_usd", 0.0)
            prompt_tokens = result.get("prompt_tokens", 0)
            completion_tokens = result.get("completion_tokens", 0)
            model_used = result.get("model_used", provider)
            narration = result.get("narration", [])

            # Write narration events to relay
            for n in narration:
                if n.get("type") in ("narration", "tool_call"):
                    await self._write_relay_event("narration", {
                        "task_id": task_id,
                        "project_id": project_id,
                        **n,
                    })

            # Check for empty output
            if not output or not output.strip():
                await self.db.execute_write(
                    "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                    ("failed", "Empty output from CLI", time.time(), task_id),
                )
                await self._write_relay_event("worker_event", {
                    "task_id": task_id,
                    "project_id": project_id,
                    "status": "failed",
                    "error": "Empty output from CLI executor",
                })
                return

            # TDD gate check
            tdd_warning = None
            if task_type in ("code", "integration", "refactor"):
                has_tests = bool(_TEST_FILE_PATTERN.search(output))
                if not has_tests:
                    tdd_warning = "No test files mentioned in output"

            # Store result in task row
            await self.db.execute_write(
                "UPDATE tasks SET status = $1, output_text = $2, cost_usd = $3, "
                "prompt_tokens = $4, completion_tokens = $5, model_used = $6, "
                "completed_at = $7, updated_at = $8 WHERE id = $9",
                ("completed", output, cost, prompt_tokens, completion_tokens,
                 model_used, time.time(), time.time(), task_id),
            )

            # Write completion event to relay
            payload = {
                "task_id": task_id,
                "project_id": project_id,
                "status": "completed",
                "cost_usd": cost,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "model_used": model_used,
            }
            if tdd_warning:
                payload["tdd_warning"] = tdd_warning

            await self._write_relay_event("worker_event", payload)

            logger.info("Hermes: task %s completed (cost=$%.4f)", task_id[:8], cost)

        except asyncio.TimeoutError as e:
            error_msg = f"Timeout: {e}" if str(e) else "CLI execution timed out"
            logger.error("Hermes: task %s timed out", task_id[:8])

            await self.db.execute_write(
                "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                ("failed", error_msg, time.time(), task_id),
            )
            await self._write_relay_event("worker_event", {
                "task_id": task_id,
                "project_id": project_id,
                "status": "failed",
                "error": error_msg,
                "timeout": True,
            })

        except asyncio.CancelledError:
            logger.info("Hermes: task %s cancelled", task_id[:8])
            # Shield DB writes from cancellation — these MUST complete
            try:
                await asyncio.shield(self.db.execute_write(
                    "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                    ("failed", "Task cancelled", time.time(), task_id),
                ))
                await asyncio.shield(self._write_relay_event("worker_event", {
                    "task_id": task_id,
                    "project_id": project_id,
                    "status": "failed",
                    "error": "Task cancelled",
                }))
            except (asyncio.CancelledError, Exception):
                pass
            return  # Don't re-raise — we handled it

        except Exception as e:
            error_msg = str(e)
            logger.error("Hermes: task %s failed: %s", task_id[:8], error_msg)

            await self.db.execute_write(
                "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                ("failed", error_msg, time.time(), task_id),
            )
            await self._write_relay_event("worker_event", {
                "task_id": task_id,
                "project_id": project_id,
                "status": "failed",
                "error": error_msg,
            })

    # ------------------------------------------------------------------
    # CLI execution (mocked in tests)
    # ------------------------------------------------------------------

    async def _run_cli_background(
        self,
        *,
        provider: str,
        prompt: str,
        cwd: str,
        task_id: str = "",
        project_id: str = "",
    ) -> dict:
        """Execute a CLI provider. Override/mock in tests.

        Returns: {output, cost_usd, prompt_tokens, completion_tokens, model_used, narration}
        """
        from gods.handlers.hermes import _build_cli_command, _parse_stream_event

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
            async with asyncio.timeout(self.default_timeout):
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
                            # Write narration to relay in real-time
                            if event.get("type") in ("narration", "tool_call"):
                                await self._write_relay_event("narration", {
                                    "task_id": task_id,
                                    "project_id": project_id,
                                    **event,
                                })
                        try:
                            import json as _json
                            data = _json.loads(decoded)
                            if data.get("type") == "result":
                                result_text = data.get("result", "")
                                if result_text:
                                    output_lines.append(result_text)
                        except (ValueError, KeyError):
                            output_lines.append(decoded)
                    else:
                        output_lines.append(decoded)
        except asyncio.TimeoutError:
            proc.kill()
            raise

        await proc.wait()
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace")

        if proc.returncode != 0 and not output_lines:
            raise RuntimeError(f"CLI exited {proc.returncode}: {stderr[:500]}")

        return {
            "output": "\n".join(output_lines),
            "cost_usd": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "model_used": provider,
            "narration": narration,
        }

    # ------------------------------------------------------------------
    # Relay write — direct insert into god_relay_events
    # ------------------------------------------------------------------

    async def _write_relay_event(self, event_type: str, payload: dict):
        """Write an event directly to the relay table."""
        try:
            await self.db.execute_write(
                "INSERT INTO god_relay_events "
                "(event_type, source, payload, severity, created_at) "
                "VALUES ($1, $2, $3, $4, $5)",
                (event_type, "hermes", json.dumps(payload), "info", time.time()),
            )
        except Exception as e:
            logger.error("Failed to write relay event: %s", e)

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def cancel(self, task_id: str):
        """Cancel a running task.

        Writes the failed status directly — don't rely on the CancelledError
        handler inside _monitor_task, since shielded DB writes inside a
        cancelled coroutine are unreliable.
        """
        task = self._tasks.get(task_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Write cleanup from cancel() itself — this is not cancelled
        try:
            await self.db.execute_write(
                "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                ("failed", "Task cancelled", time.time(), task_id),
            )
            await self._write_relay_event("worker_event", {
                "task_id": task_id,
                "status": "failed",
                "error": "Task cancelled",
            })
        except Exception as e:
            logger.warning("Hermes: cancel cleanup failed for %s: %s", task_id[:8], e)

        self._tasks.pop(task_id, None)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self, timeout: float = 30.0):
        """Gracefully shut down — wait for in-flight tasks."""
        if not self._tasks:
            return

        tasks = list(self._tasks.values())
        logger.info("Hermes: shutting down, waiting for %d in-flight tasks", len(tasks))

        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending, timeout=2.0)
        except Exception as e:
            logger.warning("Hermes: shutdown error: %s", e)

        self._tasks.clear()
