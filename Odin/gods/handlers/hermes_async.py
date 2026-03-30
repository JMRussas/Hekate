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
import time
from typing import Any, TYPE_CHECKING

from gods.pipeline import Event, Emit, idem_key, is_duplicate_key_error
from gods import safe_json
from gods.task_states import transition_task
from gods.task_definition import load_task_definition

if TYPE_CHECKING:
    from gods.providers.base import ProviderRegistry

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
        registry: ProviderRegistry | None = None,
        max_concurrent: int = 4,
        default_timeout: int = 600,
        heartbeat_interval: float = 30.0,
    ):
        self.db = db
        self.registry = registry
        self.max_concurrent = max_concurrent
        self.default_timeout = default_timeout
        self.heartbeat_interval = heartbeat_interval

        # In-flight tracking: task_id → asyncio.Task
        self._tasks: dict[str, asyncio.Task] = {}
        # Subprocess tracking: task_id → Process (for kill on cancel)
        self._processes: dict[str, Any] = {}
        # Heartbeat tasks: task_id → asyncio.Task
        self._heartbeats: dict[str, asyncio.Task] = {}
        # Serializes the concurrency check + task registration to prevent
        # two concurrent dispatch_command events from both seeing len < max
        self._dispatch_lock = asyncio.Lock()
        # Deferred dispatch queue — tasks queued when slots are full,
        # drained when a slot opens (in _on_task_done callback)
        self._deferred: list[tuple[Event, Any]] = []
        # Consecutive best-effort relay write failures — escalates to critical at threshold
        self._relay_failure_count = 0
        self._relay_failure_threshold = 3

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

        # Serialized check-and-register: prevents two concurrent dispatches from
        # both seeing len < max_concurrent and both launching (race condition).
        async with self._dispatch_lock:
            # Dedup — already running?
            if task_id in self._tasks:
                return [Emit("task_already_running", {
                    "task_id": task_id,
                    "project_id": project_id,
                }, source="hermes")]

            # Concurrency check — queue if full, drain when slot opens
            if len(self._tasks) >= self.max_concurrent:
                self._deferred.append((event, db))
                logger.info("Hermes: queued %s (deferred: %d, in-flight: %d/%d)",
                            task_id[:8], len(self._deferred),
                            len(self._tasks), self.max_concurrent)
                return None

            # Fetch task
            row = await self.db.fetchone(
                "SELECT id, title, description, task_type, status, model_tier, context_json, retry_count "
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
            retry_count = row.get("retry_count", 0) or 0

            # Load TaskDefinition for per-task timeout
            td = load_task_definition(context_json)
            task_timeout = td.timeout_seconds or self.default_timeout

            # Guard: only execute pending/queued tasks
            if status not in ("pending", "queued"):
                logger.debug("Hermes: task %s is %s, skipping (normal dedup)", task_id[:8], status)
                return None

            # Set task → running
            await transition_task(self.db, task_id, "running",
                extra_fields={"started_at": time.time()},
                source="hermes.dispatch")

            # Resolve working directory and additional repos
            proj_row = await self.db.fetchone(
                "SELECT repo_path, additional_repos FROM projects WHERE id = $1", (project_id,))
            cwd = proj_row.get("repo_path", ".") if proj_row else "."

            # Determine add_dirs: task-level repo_paths override project-level additional_repos
            add_dirs = None
            task_row_full = await self.db.fetchone(
                "SELECT repo_paths FROM tasks WHERE id = $1", (task_id,))
            task_repo_paths = task_row_full.get("repo_paths") if task_row_full else None
            if task_repo_paths:
                try:
                    add_dirs = json.loads(task_repo_paths)
                except (json.JSONDecodeError, TypeError):
                    pass
            if not add_dirs and proj_row:
                proj_additional = proj_row.get("additional_repos")
                if proj_additional:
                    try:
                        add_dirs = json.loads(proj_additional)
                    except (json.JSONDecodeError, TypeError):
                        pass

            # Build prompt — give the model full context so it can reason about the task
            prompt = f"# Task: {title}\n\n"
            prompt += f"## Requirements\n{description or 'No description provided.'}\n\n"
            prompt += f"## Instructions\n"
            prompt += f"1. Read the relevant code before making changes\n"
            prompt += f"2. Implement the requirements\n"
            prompt += f"3. After implementing, review your own work:\n"
            prompt += f"   - Any gaps? Anything the task asked for that you missed?\n"
            prompt += f"   - Does your change fit the current codebase patterns?\n"
            prompt += f"   - Did you handle edge cases?\n"
            prompt += f"4. Fix any issues you find before finishing\n"

            if context_json and context_json != "{}":
                try:
                    ctx = safe_json.loads_dict(context_json) if isinstance(context_json, str) else context_json
                    if isinstance(ctx, dict):
                        if ctx.get("verification_feedback"):
                            prompt += f"\n## Previous Verification Feedback\n{ctx['verification_feedback']}\n"
                            prompt += f"This task was previously attempted and the verifier found issues. Address them.\n"
                        if ctx.get("prompt_guidance"):
                            prompt += f"\n## Guidance\n{ctx['prompt_guidance']}\n"
                        if ctx.get("review_feedback"):
                            prompt += f"\n## Review Feedback\n{ctx['review_feedback']}\n"
                except (json.JSONDecodeError, TypeError):
                    pass

            # Launch background task — registered inside the lock so the slot
            # count is updated before any concurrent dispatch can re-check.
            bg_task = asyncio.create_task(
                self._monitor_task(
                    task_id=task_id,
                    project_id=project_id,
                    provider=provider,
                    prompt=prompt,
                    cwd=cwd,
                    task_type=task_type,
                    add_dirs=add_dirs,
                    retry_count=retry_count,
                    timeout=task_timeout,
                ),
                name=f"hermes-{task_id}",
            )
            self._tasks[task_id] = bg_task

        # Set up cleanup callback outside the lock (add_done_callback is synchronous)
        bg_task.add_done_callback(lambda t: self._on_task_done(task_id))

        logger.info("Hermes: launched %s via %s (in-flight: %d/%d)",
                     task_id[:8], provider, len(self._tasks), self.max_concurrent)

        return [Emit("task_running", {
            "task_id": task_id,
            "project_id": project_id,
            "provider": provider,
        }, source="hermes")]

    # ------------------------------------------------------------------
    # Deferred queue drain — called when a slot opens
    # ------------------------------------------------------------------

    def _on_task_done(self, task_id: str):
        """Cleanup callback when a task finishes. Drains deferred queue."""
        self._tasks.pop(task_id, None)
        if self._deferred:
            asyncio.get_event_loop().create_task(self._drain_deferred())

    async def _drain_deferred(self):
        """Process queued dispatch events now that a slot is available."""
        while self._deferred and len(self._tasks) < self.max_concurrent:
            event, db = self._deferred.pop(0)
            task_id = event.payload.get("task_id", "?")[:8]
            logger.info("Hermes: draining deferred %s (remaining: %d)", task_id, len(self._deferred))
            await self.handle_dispatch(event, db)

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
        add_dirs: list[str] | None = None,
        retry_count: int = 0,
        timeout: int | None = None,
    ):
        """Background coroutine that runs CLI and writes results to relay.

        This ALWAYS writes a worker_event to the relay, even on internal errors.
        Tasks must never get stuck in 'running'.
        """
        effective_timeout = timeout or self.default_timeout

        # Start heartbeat
        hb_task = asyncio.create_task(
            self._heartbeat_loop(task_id, project_id, provider),
            name=f"heartbeat-{task_id}",
        )
        self._heartbeats[task_id] = hb_task

        try:
            t0 = time.time()
            result = await asyncio.wait_for(
                self._run_cli_background(
                    provider=provider,
                    prompt=prompt,
                    cwd=cwd,
                    task_id=task_id,
                    project_id=project_id,
                    add_dirs=add_dirs,
                    timeout=effective_timeout,
                ),
                timeout=effective_timeout * 1.2,
            )
            elapsed = time.time() - t0

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
                logger.warning("Hermes: task %s produced empty output", task_id[:8])
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

            # Write relay event FIRST — if this fails, leave task as "running"
            # so it gets detected as stuck and retried. This prevents split-brain
            # where DB says "completed" but Mimir never sees the relay event.
            affected_files = result.get("affected_files", [])
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
            if affected_files:
                payload["affected_files"] = affected_files

            await self._write_relay_event_strict(
                "worker_event", payload,
                idempotency_key=idem_key("hermes_complete", task_id, str(retry_count)),
            )

            # Relay write succeeded — now safe to mark task completed in DB
            await self.db.execute_write(
                "UPDATE tasks SET status = $1, output_text = $2, cost_usd = $3, "
                "prompt_tokens = $4, completion_tokens = $5, model_used = $6, "
                "completed_at = $7, updated_at = $8 WHERE id = $9",
                ("completed", output, cost, prompt_tokens, completion_tokens,
                 model_used, time.time(), time.time(), task_id),
            )

            logger.info("Hermes: task %s completed in %.1fs (cost=$%.4f)", task_id[:8], elapsed, cost)

        except asyncio.TimeoutError as e:
            error_msg = f"Timeout: {e}" if str(e) else "CLI execution timed out"
            logger.error("Hermes: task %s timed out", task_id[:8])

            # Kill the orphaned subprocess
            await self._kill_process(task_id)

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
            }, idempotency_key=idem_key("hermes_failed", task_id, str(retry_count)))

        except asyncio.CancelledError:
            logger.info("Hermes: task %s cancelled", task_id[:8])
            # Kill the orphaned subprocess
            await self._kill_process(task_id)
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
                }, idempotency_key=idem_key("hermes_cancelled", task_id, str(retry_count))))
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

        finally:
            # Always stop heartbeat and clean up process ref
            self._stop_heartbeat(task_id)
            self._processes.pop(task_id, None)

    # ------------------------------------------------------------------
    # Process cleanup
    # ------------------------------------------------------------------

    async def _kill_process(self, task_id: str):
        """Terminate and kill the subprocess for a task, if tracked."""
        proc = self._processes.get(task_id)
        if proc is None:
            return
        try:
            proc.terminate()
            # Give it 2s to exit gracefully, then force-kill
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            logger.info("Hermes: killed subprocess for task %s (pid=%s)", task_id[:8], proc.pid)
        except (ProcessLookupError, OSError) as e:
            logger.debug("Hermes: process already gone for task %s: %s", task_id[:8], e)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, task_id: str, project_id: str, provider: str):
        """Emit periodic heartbeat events while a task is running.

        Also updates updated_at on the task row so odin_tick's stuck-task
        detection doesn't reset legitimately long-running tasks. A task is
        only considered stuck if it has had NO heartbeat for > threshold seconds.
        """
        start = time.time()
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                now = time.time()
                uptime = now - start
                # Update updated_at so the stuck-task watchdog sees a live signal
                await self.db.execute_write(
                    "UPDATE tasks SET updated_at = $1 WHERE id = $2",
                    (now, task_id),
                )
                await self._write_relay_event("heartbeat", {
                    "task_id": task_id,
                    "project_id": project_id,
                    "provider": provider,
                    "uptime_s": round(uptime, 1),
                })
        except asyncio.CancelledError:
            pass

    def _stop_heartbeat(self, task_id: str):
        """Cancel heartbeat for a task."""
        hb = self._heartbeats.pop(task_id, None)
        if hb and not hb.done():
            hb.cancel()

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
        add_dirs: list[str] | None = None,
        timeout: int | None = None,
    ) -> dict:
        """Execute a CLI provider via the provider registry.

        Falls back to legacy inline execution if no registry or provider
        is not registered (e.g. ollama).

        Returns: {output, cost_usd, prompt_tokens, completion_tokens, model_used, narration}
        """
        # Try provider registry first
        cli_provider = self.registry.get(provider) if self.registry else None

        # Apply per-task add_dirs if the provider supports with_config
        if cli_provider is not None and add_dirs:
            if hasattr(cli_provider, 'with_config'):
                cli_provider = cli_provider.with_config(add_dirs=add_dirs)
                logger.info("Hermes: task %s using add_dirs: %s", task_id[:8], add_dirs)

        if cli_provider is not None:
            # Stream narration in real-time: parse each stream-json line and
            # write tool_use / assistant events to the relay as they arrive.
            async def _on_line(line: str):
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    return
                event_type = data.get("type")
                if event_type == "tool_use":
                    await self._write_relay_event("narration", {
                        "task_id": task_id,
                        "project_id": project_id,
                        "type": "tool_call",
                        "tool": data.get("tool", ""),
                        "input": str(data.get("input", ""))[:200],
                    })
                elif event_type == "assistant":
                    msg = data.get("message", {})
                    for content in msg.get("content", []):
                        if content.get("type") == "text" and content.get("text"):
                            await self._write_relay_event("narration", {
                                "task_id": task_id,
                                "project_id": project_id,
                                "type": "assistant",
                                "text": content["text"][:300],
                            })

            result = await cli_provider.execute(
                prompt=prompt,
                cwd=cwd,
                timeout=timeout or self.default_timeout,
                on_process=lambda proc: self._processes.__setitem__(task_id, proc),
                on_line=_on_line,
            )
            return {
                "output": result.output,
                "cost_usd": result.cost_usd,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "model_used": result.model or provider,
                "narration": result.narration,
                "affected_files": result.affected_files,
            }

        # Fallback: unknown provider — raise so caller writes a failed event
        raise RuntimeError(f"No provider registered for '{provider}'")

    # ------------------------------------------------------------------
    # Relay write — direct insert into god_relay_events
    # ------------------------------------------------------------------

    async def _write_relay_event(
        self, event_type: str, payload: dict, *, idempotency_key: str | None = None,
    ):
        """Write an event directly to the relay table (best-effort, swallows errors).

        Tracks consecutive failures and escalates to critical after threshold.
        """
        try:
            if idempotency_key:
                await self.db.execute_write(
                    "INSERT INTO god_relay_events "
                    "(event_type, source, payload, severity, idempotency_key, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    (event_type, "hermes", json.dumps(payload), "info",
                     idempotency_key, time.time()),
                )
            else:
                await self.db.execute_write(
                    "INSERT INTO god_relay_events "
                    "(event_type, source, payload, severity, created_at) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    (event_type, "hermes", json.dumps(payload), "info", time.time()),
                )
            self._relay_failure_count = 0  # reset on success
        except Exception as e:
            if idempotency_key and is_duplicate_key_error(e):
                logger.debug("Hermes relay dedup: key %s already exists", idempotency_key[:16])
                return
            self._relay_failure_count += 1
            logger.error("Failed to write relay event: %s", e)
            if self._relay_failure_count >= self._relay_failure_threshold:
                logger.critical(
                    "Hermes: %d consecutive relay write failures — "
                    "god_relay_events table may be unavailable. "
                    "Pipeline events are being dropped.",
                    self._relay_failure_count,
                )

    async def _write_relay_event_strict(
        self, event_type: str, payload: dict, *, idempotency_key: str | None = None,
    ):
        """Write an event to the relay table — raises on failure.

        Used for completion events where the relay write MUST succeed before
        we update the task DB row, preventing split-brain.
        """
        if idempotency_key:
            try:
                await self.db.execute_write(
                    "INSERT INTO god_relay_events "
                    "(event_type, source, payload, severity, idempotency_key, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6)",
                    (event_type, "hermes", json.dumps(payload), "info",
                     idempotency_key, time.time()),
                )
            except Exception as e:
                if is_duplicate_key_error(e):
                    # Dedup on replay — task DB update still runs after this,
                    # which is correct: setting status=completed is idempotent.
                    logger.info("Hermes relay dedup (strict): key %s already exists", idempotency_key[:16])
                    return
                raise
        else:
            await self.db.execute_write(
                "INSERT INTO god_relay_events "
                "(event_type, source, payload, severity, created_at) "
                "VALUES ($1, $2, $3, $4, $5)",
                (event_type, "hermes", json.dumps(payload), "info", time.time()),
            )

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def cancel(self, task_id: str):
        """Cancel a running task.

        Kills the subprocess (if tracked), cancels the asyncio.Task,
        then writes failed status directly from cancel() (not the handler).
        """
        # Kill subprocess first
        proc = self._processes.pop(task_id, None)
        if proc is not None:
            try:
                proc.terminate()
                # Wait briefly for graceful exit
                await asyncio.sleep(0.5)
                if proc.returncode is None:
                    proc.kill()
            except (ProcessLookupError, OSError):
                pass

        # Stop heartbeat
        self._stop_heartbeat(task_id)

        # Cancel asyncio.Task
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
