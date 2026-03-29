"""Mimir handler — non-blocking verification, review, knowledge extraction.

MimirRunner manages background LLM verification:
  - handle_verify: receives worker_event:completed, launches background task, returns immediately
  - _verify_task: background coroutine that calls LLM verifier, writes results to relay
  - shutdown: gracefully waits for in-flight verifications

The pipeline never blocks on LLM verification. Results appear in the relay
table and get picked up by odin on the next tick.

Also handles (still synchronous, they're fast):
  - mimir_review: code quality review
  - mimir_handle_review_rejection: reset task on review rejection
  - mimir_handle_task_rejection: emit tick for re-dispatch
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import shutil
import time
from typing import Any

from gods.pipeline import Event, Emit
from gods import safe_json
from gods.providers.response_validator import validate_verdict, validate_review, extract_json

logger = logging.getLogger("gods.handlers.mimir")


# ---------------------------------------------------------------------------
# Output quality check (heuristic, no LLM)
# ---------------------------------------------------------------------------

_ERROR_PATTERNS = re.compile(
    r'(^Error:|^Traceback|^fatal:|command not found|FAILED|'
    r'ModuleNotFoundError|ImportError|SyntaxError|NameError)',
    re.MULTILINE | re.IGNORECASE,
)


def _check_output_quality(output: str | None) -> dict:
    """Fast heuristic check on output quality. No LLM call.

    Returns {passed: bool, reason: str, warning: str?}
    """
    if not output or not output.strip():
        return {"passed": False, "reason": "Output is empty"}

    stripped = output.strip()

    # Very short output is suspicious
    if len(stripped) < 10:
        return {
            "passed": False,
            "reason": "Output is suspiciously short",
            "warning": "Very short output may indicate incomplete execution",
        }

    # Check for error-only output
    lines = stripped.split("\n")
    error_lines = [l for l in lines if _ERROR_PATTERNS.search(l)]
    if len(error_lines) > len(lines) * 0.5 and len(lines) > 1:
        return {
            "passed": False,
            "reason": "Output appears to be mostly errors",
        }

    # Single line that's just an error
    if len(lines) <= 2 and error_lines:
        return {
            "passed": False,
            "reason": "Output is an error message",
        }

    return {"passed": True, "reason": "Output looks reasonable"}


# ---------------------------------------------------------------------------
# Agent-based verification — spawns a Claude agent with prometheus tools
# ---------------------------------------------------------------------------

_MCP_CONFIG_TEMPLATE = """\
{{
  "mcpServers": {{
    "prometheus": {{
      "type": "stdio",
      "command": "python",
      "args": ["{prometheus_script}"],
      "env": {{
        "HEKATE_ENGINE_URL": "http://localhost:5200"
      }}
    }}
  }}
}}
"""

_PROMETHEUS_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "prometheus_mcp.py"
)


async def _spawn_agent(cmd: list[str], env: dict) -> tuple[int, bytes, bytes]:
    """Spawn a subprocess and return (returncode, stdout, stderr).

    Extracted as a standalone coroutine so tests can monkeypatch it.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, b"", b"timeout"
    return proc.returncode, stdout, stderr


async def _call_verifier(
    *,
    task_title: str,
    task_description: str,
    output_text: str,
    task_id: str,
    gateway_url: str = "http://localhost:5210",
) -> dict:
    """Verify task output via LLM gateway and submit verdict to engine API.

    Uses the gateway for LLM judgment, then calls /verify directly to
    update the DB and emit relay events. The agent subprocess pattern was
    unreliable (Claude CLI didn't discover MCP tools fast enough).

    Returns {verdict: "passed"|"gaps_found"|"human_needed", confidence: float, feedback: str}.
    Also calls /verify API to emit relay events for wave progression.
    """
    import httpx

    # Get verdict from LLM gateway
    result = await _call_verifier_gateway(
        task_title=task_title,
        task_description=task_description,
        output_text=output_text,
        gateway_url=gateway_url,
    )

    verdict = result.get("verdict", "human_needed")
    feedback = result.get("feedback", "")
    confidence = result.get("confidence", 0.5)

    # Submit verdict to engine API — this emits relay events for wave progression
    engine_url = os.environ.get("HEKATE_ENGINE_URL", "http://localhost:5200")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{engine_url}/api/tasks/{task_id}/verify", json={
                "verdict": verdict,
                "feedback": feedback,
                "confidence": confidence,
            })
            if resp.status_code == 200:
                logger.info("Mimir: submitted %s verdict for task %s via /verify API", verdict, task_id[:8])
            else:
                logger.warning("Mimir: /verify returned %d for task %s", resp.status_code, task_id[:8])
    except Exception as e:
        logger.warning("Mimir: failed to call /verify for task %s: %s", task_id[:8], e)

    return result


async def _call_verifier_gateway(
    *,
    task_title: str,
    task_description: str,
    output_text: str,
    gateway_url: str = "http://localhost:5210",
) -> dict:
    """Fallback: call LLM gateway directly for verification."""
    import httpx

    prompt = (
        f"Verify that the following output satisfies the task requirements.\n\n"
        f"Task: {task_title}\n"
        f"Description: {task_description}\n\n"
        f"Output:\n{output_text[:5000]}\n\n"
        f"Respond with JSON: {{\"verdict\": \"passed|gaps_found|human_needed\", "
        f"\"confidence\": 0.0-1.0, \"feedback\": \"...\"}}"
    )

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(f"{gateway_url}/v1/chat", json={
            "provider": "claude",
            "system_prompt": "You are a code verification assistant. Always respond with valid JSON.",
            "user_message": prompt,
        })
        resp.raise_for_status()
        text = resp.json().get("text", "")

    result = validate_verdict(text)
    logger.debug("Mimir._call_verifier_gateway: validated verdict: %s", result)
    return result


# ---------------------------------------------------------------------------
# LLM-backed code review (mocked in tests)
# ---------------------------------------------------------------------------

async def _call_reviewer(
    *,
    task_title: str,
    output_text: str,
    gateway_url: str = "http://localhost:5210",
) -> dict:
    """Call LLM to review code quality.

    Returns {verdict: "approved"|"changes_requested", feedback: str}
    """
    import httpx

    prompt = (
        f"Review the code quality of this task output.\n\n"
        f"Task: {task_title}\n\n"
        f"Output:\n{output_text[:5000]}\n\n"
        f"Respond with JSON: {{\"verdict\": \"approved|changes_requested\", \"feedback\": \"...\"}}"
    )

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(f"{gateway_url}/v1/chat", json={
            "provider": "claude",
            "system_prompt": "You are a code review assistant. Always respond with valid JSON.",
            "user_message": prompt,
        })
        resp.raise_for_status()
        text = resp.json().get("text", "")

    return validate_review(text)


# ---------------------------------------------------------------------------
# Knowledge extraction (mocked in tests)
# ---------------------------------------------------------------------------

async def _call_knowledge_extractor(
    *,
    output_text: str,
    gateway_url: str = "http://localhost:5210",
) -> list[str]:
    """Extract reusable findings from task output.

    Returns list of finding strings.
    """
    import httpx

    prompt = (
        f"Extract reusable knowledge findings from this task output. "
        f"Return a JSON array of concise finding strings.\n\n"
        f"Output:\n{output_text[:5000]}"
    )

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(f"{gateway_url}/v1/chat", json={
            "provider": "claude",
            "system_prompt": "You are a knowledge extraction assistant. Always respond with valid JSON.",
            "user_message": prompt,
        })
        resp.raise_for_status()
        text = resp.json().get("text", "")

    result = extract_json(text)
    return result if isinstance(result, list) else []


async def _extract_knowledge(
    *,
    task_id: str,
    project_id: str,
    output_text: str,
    db: Any,
    gateway_url: str = "http://localhost:5210",
) -> list[str]:
    """Extract and store knowledge from task output."""
    if not output_text or not output_text.strip():
        return []

    findings = await _call_knowledge_extractor(
        output_text=output_text,
        gateway_url=gateway_url,
    )

    for finding in findings:
        await db.execute_write(
            "INSERT INTO project_knowledge (project_id, task_id, content, created_at) "
            "VALUES ($1, $2, $3, $4)",
            (project_id, task_id, finding, time.time()),
        )

    return findings


# ---------------------------------------------------------------------------
# MimirRunner — async verification with in-flight tracking
# ---------------------------------------------------------------------------

class MimirRunner:
    """Manages async LLM verification with in-flight tracking.

    Mirrors HermesRunner pattern: launch background task, return immediately,
    write results to the relay table for the pipeline to pick up.
    """

    def __init__(self, db, *, max_concurrent: int = 4):
        self.db = db
        self.max_concurrent = max_concurrent

        # In-flight tracking: task_id -> asyncio.Task
        self._tasks: dict[str, asyncio.Task] = {}

        # Deferred queue: events that arrived when slots were full.
        # Drained automatically as in-flight tasks complete.
        self._deferred: collections.deque[Event] = collections.deque()

    @property
    def in_flight(self) -> set[str]:
        """Set of currently in-flight verification task IDs."""
        return set(self._tasks.keys())

    # ------------------------------------------------------------------
    # Handle verify — launch background task, return immediately
    # ------------------------------------------------------------------

    async def handle_verify(self, event: Event, db=None) -> list[Emit] | None:
        """Handle worker_event: launch background verification, return immediately.

        Accepts optional db parameter for pipeline compatibility (ignored,
        uses self.db instead for background task access).
        """
        task_id = event.payload.get("task_id")
        project_id = event.payload.get("project_id")
        status = event.payload.get("status")

        # Only verify completed tasks
        if status != "completed":
            return None

        if not task_id:
            return [Emit("mimir_error", {"error": "No task_id"}, source="mimir")]

        # Dedup — already verifying?
        if task_id in self._tasks:
            logger.debug("Mimir: task %s already being verified, skipping", task_id[:8])
            return None

        # Concurrency check — queue internally so deferred tasks aren't lost
        if len(self._tasks) >= self.max_concurrent:
            logger.warning("Mimir: verification slots full (%d/%d), queuing task %s (%d already queued)",
                           len(self._tasks), self.max_concurrent, task_id[:8], len(self._deferred))
            self._deferred.append(event)
            return [Emit("verification_deferred", {
                "task_id": task_id,
                "project_id": project_id,
                "in_flight": len(self._tasks),
                "queued": len(self._deferred),
                "max_concurrent": self.max_concurrent,
            }, source="mimir")]

        # Fetch task for idempotency check and fast-path decisions
        row = await self.db.fetchone(
            "SELECT title, description, output_text, retry_count, max_retries, context_json, "
            "verification_status FROM tasks WHERE id = $1",
            (task_id,),
        )
        if not row:
            return [Emit("mimir_error", {
                "error": f"Task {task_id} not found",
            }, source="mimir")]

        # Idempotency guard -- if already verified, emit immediately (no background task needed)
        if row.get("verification_status") == "passed":
            logger.info("Task %s already verified, skipping", task_id[:8])
            return [Emit("task_verified", {
                "task_id": task_id,
                "project_id": project_id,
                "confidence": 1.0,
                "already_verified": True,
            }, source="mimir")]

        title = row["title"]
        description = row.get("description", "")
        output_text = row.get("output_text")
        retry_count = (row.get("retry_count") or 0)
        max_retries = (row.get("max_retries") or 3)

        # Check if task was "already done" — route to needs_review, not auto-pass.
        # The heuristic can't distinguish legitimate "already migrated" from
        # "task failed: already exists" — a human or LLM must decide.
        if output_text and any(phrase in output_text.lower() for phrase in [
            "already exists", "already done", "already in place", "already has",
            "already present", "already defined", "already implemented",
            "already installed", "nothing to do", "no changes needed", "file already",
        ]):
            logger.info("Mimir: task %s output has 'already done' phrase — routing to needs_review", task_id[:8])
            await self.db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                ("needs_review", time.time(), task_id),
            )
            await self._write_relay_event("needs_human_review", {
                "task_id": task_id,
                "project_id": project_id,
                "reason": "Output contains 'already done' phrase — needs human verification",
                "confidence": 0.3,
            })
            return [Emit("needs_human_review", {
                "task_id": task_id,
                "project_id": project_id,
                "reason": "already_done_phrase",
            }, source="mimir")]

        # Quick heuristic check -- synchronous, no LLM
        quality = _check_output_quality(output_text)
        if not quality["passed"]:
            # Heuristic failure -- handle synchronously (fast)
            return await self._handle_heuristic_failure(
                task_id=task_id,
                project_id=project_id,
                reason=quality["reason"],
                retry_count=retry_count,
                max_retries=max_retries,
            )

        # All fast checks passed -- launch background LLM verification
        bg_task = asyncio.create_task(
            self._verify_task(
                task_id=task_id,
                project_id=project_id,
                title=title,
                description=description,
                output_text=output_text or "",
                retry_count=retry_count,
                max_retries=max_retries,
            ),
            name=f"mimir-verify-{task_id}",
        )
        self._tasks[task_id] = bg_task

        # Cleanup callback — also drains the deferred queue
        bg_task.add_done_callback(lambda t: self._on_task_done(task_id))

        logger.info("Mimir: launched verification for %s (in-flight: %d/%d)",
                     task_id[:8], len(self._tasks), self.max_concurrent)

        return [Emit("verification_started", {
            "task_id": task_id,
            "project_id": project_id,
        }, source="mimir")]

    # ------------------------------------------------------------------
    # Done callback — cleanup + drain deferred queue
    # ------------------------------------------------------------------

    def _on_task_done(self, task_id: str):
        """Synchronous callback from asyncio.Task.add_done_callback.

        Removes the finished task and schedules deferred verifications.
        """
        self._tasks.pop(task_id, None)
        if self._deferred and len(self._tasks) < self.max_concurrent:
            loop = asyncio.get_event_loop()
            loop.create_task(self._drain_deferred())

    async def _drain_deferred(self):
        """Process queued events until slots are full or queue is empty.

        Calls handle_verify for each deferred event. If handle_verify
        launches a background task, slots fill up normally. If it returns
        synchronously (already verified, heuristic failure), we keep draining.
        """
        while self._deferred and len(self._tasks) < self.max_concurrent:
            event = self._deferred.popleft()
            task_id = event.payload.get("task_id", "?")
            project_id = event.payload.get("project_id", "")
            logger.info("Mimir: draining deferred verification for %s (%d queued remain)",
                        task_id[:8], len(self._deferred))
            # Write dequeued marker so replay_deferred_from_relay skips this on restart
            await self._write_relay_event("verification_dequeued", {
                "task_id": task_id,
                "project_id": project_id,
            })
            try:
                emits = await self.handle_verify(event)
                # Write any relay-worthy emits (verification_started, task_verified, etc.)
                if emits:
                    for emit in emits:
                        await self._write_relay_event(emit.event_type, emit.payload)
            except Exception as e:
                logger.error("Mimir: deferred verification failed for %s: %s", task_id[:8], e)

    async def replay_deferred_from_relay(self):
        """On startup, replay unprocessed verification_deferred events into _deferred.

        Finds relay events of type verification_deferred that have no corresponding
        verification_dequeued event. Adds them back to _deferred so they're picked
        up as soon as slots open.

        Idempotent: task_ids already in _deferred are not re-added.
        """
        # Find all deferred events
        deferred_rows = await self.db.fetchall(
            "SELECT id, payload FROM god_relay_events "
            "WHERE event_type = $1 ORDER BY id ASC",
            ("verification_deferred",),
        )
        if not deferred_rows:
            return

        # Find all already-dequeued task_ids
        dequeued_rows = await self.db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = $1",
            ("verification_dequeued",),
        )
        already_dequeued: set[str] = set()
        for row in dequeued_rows:
            try:
                payload = row.get("payload") or row.get(0, "{}")
                if isinstance(payload, str):
                    payload = json.loads(payload)
                tid = payload.get("task_id", "")
                if tid:
                    already_dequeued.add(tid)
            except Exception:
                pass

        # Already in the current queue
        already_queued = {e.payload.get("task_id") for e in self._deferred}

        replayed = 0
        for row in deferred_rows:
            try:
                payload_raw = row.get("payload") or row.get(1, "{}")
                if isinstance(payload_raw, str):
                    payload = json.loads(payload_raw)
                else:
                    payload = payload_raw or {}
                task_id = payload.get("task_id", "")
                project_id = payload.get("project_id", "")
                if not task_id:
                    continue
                if task_id in already_dequeued:
                    continue
                if task_id in already_queued:
                    continue
                # Reconstruct the worker_event that would have triggered verification
                from gods.pipeline import Event as _Event
                replay_event = _Event("worker_event", {
                    "task_id": task_id,
                    "project_id": project_id,
                    "status": "completed",
                }, source="mimir_replay")
                self._deferred.append(replay_event)
                already_queued.add(task_id)
                replayed += 1
            except Exception as e:
                logger.warning("Mimir: failed to replay deferred event: %s", e)

        if replayed:
            logger.info("Mimir: replayed %d unprocessed deferred verifications from relay", replayed)

    # ------------------------------------------------------------------
    # Heuristic failure (synchronous, no background task needed)
    # ------------------------------------------------------------------

    async def _handle_heuristic_failure(
        self,
        *,
        task_id: str,
        project_id: str,
        reason: str,
        retry_count: int,
        max_retries: int,
    ) -> list[Emit]:
        """Handle heuristic-detected failure synchronously."""
        if retry_count >= max_retries:
            await self.db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                ("needs_review", time.time(), task_id),
            )
            return [Emit("needs_human_review", {
                "task_id": task_id,
                "project_id": project_id,
                "reason": reason,
            }, source="mimir")]

        # Reset for retry
        fresh = await self.db.fetchone("SELECT context_json FROM tasks WHERE id = $1", (task_id,))
        fresh_ctx_str = fresh.get("context_json", "{}") if fresh else "{}"
        try:
            ctx = safe_json.loads_dict(fresh_ctx_str) if isinstance(fresh_ctx_str, str) else fresh_ctx_str
        except (json.JSONDecodeError, TypeError):
            ctx = {}
        if not isinstance(ctx, dict):
            ctx = {}
        ctx["verification_feedback"] = reason

        await self.db.execute_write(
            "UPDATE tasks SET status = $1, retry_count = $2, context_json = $3, updated_at = $4 WHERE id = $5",
            ("pending", retry_count + 1, json.dumps(ctx), time.time(), task_id),
        )
        return [Emit("task_rejected", {
            "task_id": task_id,
            "project_id": project_id,
            "feedback": reason,
        }, source="mimir")]

    # ------------------------------------------------------------------
    # Background verification -- runs LLM, writes results to relay
    # ------------------------------------------------------------------

    async def _verify_task(
        self,
        *,
        task_id: str,
        project_id: str,
        title: str,
        description: str,
        output_text: str,
        retry_count: int,
        max_retries: int,
    ):
        """Background coroutine that runs LLM verification and writes results to relay.

        This ALWAYS writes an event to the relay, even on internal errors.
        Verifications must never silently disappear.
        """
        try:
            result = await _call_verifier(
                task_title=title,
                task_description=description,
                output_text=output_text,
                task_id=task_id,
            )
            logger.info("Mimir: verifier returned type=%s value=%s",
                         type(result).__name__, str(result)[:200])
        except Exception as e:
            logger.error("Mimir: verifier failed for task %s: %s (%s)", task_id[:8], type(e).__name__, e)
            # Verifier unavailable → needs_review regardless of output length.
            # A task having output is not evidence it succeeded — route to human review.
            await self.db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                ("needs_review", time.time(), task_id),
            )
            await self._write_relay_event("needs_human_review", {
                "task_id": task_id,
                "project_id": project_id,
                "reason": f"Verification unavailable ({type(e).__name__}): {e}",
                "confidence": 0.3,
            })
            return

        # Agent submitted verdict directly via submit_verification tool —
        # read back from DB and emit the appropriate relay event
        if isinstance(result, dict) and result.get("verdict") == "_agent_submitted":
            fresh = await self.db.fetchone(
                "SELECT status, verification_status, verification_notes FROM tasks WHERE id = $1",
                (task_id,),
            )
            if fresh:
                vstatus = fresh.get("verification_status")
                notes = fresh.get("verification_notes") or ""
                task_status = fresh.get("status")
                if vstatus == "passed":
                    await self._write_relay_event("task_verified", {
                        "task_id": task_id,
                        "project_id": project_id,
                        "confidence": 1.0,
                    })
                elif task_status == "pending":
                    # gaps_found + retried — emit task_rejected so odin re-dispatches
                    await self._write_relay_event("task_rejected", {
                        "task_id": task_id,
                        "project_id": project_id,
                        "feedback": notes,
                    })
                else:
                    # needs_review (gaps_found maxed out, or human_needed)
                    await self._write_relay_event("needs_human_review", {
                        "task_id": task_id,
                        "project_id": project_id,
                        "reason": notes or "Agent flagged for human review",
                    })
            else:
                await self._write_relay_event("needs_human_review", {
                    "task_id": task_id,
                    "project_id": project_id,
                    "reason": "Agent submitted verdict but task not found",
                })
            return

        # Normalize result -- LLM might return array, string, or nested structure
        if isinstance(result, list) and len(result) > 0:
            result = result[0] if isinstance(result[0], dict) else {"verdict": "human_needed", "feedback": str(result)[:200]}
        elif isinstance(result, str):
            lower = result.lower()
            if any(w in lower for w in ["passed", "satisf", "correct", "done", "complet"]):
                result = {"verdict": "passed", "confidence": 0.7, "feedback": result[:200]}
            else:
                result = {"verdict": "human_needed", "confidence": 0.0, "feedback": result[:200]}
        elif not isinstance(result, dict):
            result = {"verdict": "human_needed", "confidence": 0.0, "feedback": str(result)[:200]}

        # Ensure required keys exist
        if "verdict" not in result:
            result["verdict"] = "human_needed"
        if "confidence" not in result:
            result["confidence"] = 0.0

        verdict = result.get("verdict", "human_needed")
        feedback = result.get("feedback", "")

        if verdict == "passed":
            # Extract knowledge from successful output
            try:
                await _extract_knowledge(
                    task_id=task_id,
                    project_id=project_id,
                    output_text=output_text,
                    db=self.db,
                )
            except Exception as ke:
                logger.warning("Knowledge extraction failed for task %s: %s", task_id[:8], ke)

            await self._write_relay_event("task_verified", {
                "task_id": task_id,
                "project_id": project_id,
                "confidence": result.get("confidence", 1.0),
            })

        elif verdict == "gaps_found":
            if retry_count >= max_retries:
                await self.db.execute_write(
                    "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                    ("needs_review", time.time(), task_id),
                )
                await self._write_relay_event("needs_human_review", {
                    "task_id": task_id,
                    "project_id": project_id,
                    "reason": f"Verification gaps after {retry_count} retries: {feedback}",
                })
                return

            # Reset for retry with feedback
            fresh = await self.db.fetchone("SELECT context_json FROM tasks WHERE id = $1", (task_id,))
            fresh_ctx_str = fresh.get("context_json", "{}") if fresh else "{}"
            try:
                ctx = safe_json.loads_dict(fresh_ctx_str) if isinstance(fresh_ctx_str, str) else fresh_ctx_str
            except (json.JSONDecodeError, TypeError):
                ctx = {}
            if not isinstance(ctx, dict):
                ctx = {}
            ctx["verification_feedback"] = feedback

            await self.db.execute_write(
                "UPDATE tasks SET status = $1, retry_count = $2, context_json = $3, updated_at = $4 WHERE id = $5",
                ("pending", retry_count + 1, json.dumps(ctx), time.time(), task_id),
            )
            await self._write_relay_event("task_rejected", {
                "task_id": task_id,
                "project_id": project_id,
                "feedback": feedback,
            })

        else:  # human_needed
            await self.db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                ("needs_review", time.time(), task_id),
            )
            await self._write_relay_event("needs_human_review", {
                "task_id": task_id,
                "project_id": project_id,
                "reason": feedback or "Verification requires human review",
            })

    # ------------------------------------------------------------------
    # Relay write -- direct insert into god_relay_events
    # ------------------------------------------------------------------

    async def _write_relay_event(self, event_type: str, payload: dict):
        """Write an event directly to the relay table."""
        try:
            await self.db.execute_write(
                "INSERT INTO god_relay_events "
                "(event_type, source, payload, severity, created_at) "
                "VALUES ($1, $2, $3, $4, $5)",
                (event_type, "mimir", json.dumps(payload), "info", time.time()),
            )
        except Exception as e:
            logger.error("Mimir: failed to write relay event: %s", e)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self, timeout: float = 30.0):
        """Gracefully shut down -- wait for in-flight verifications."""
        if self._deferred:
            logger.warning("Mimir: shutting down with %d deferred verifications still queued",
                           len(self._deferred))
            self._deferred.clear()

        if not self._tasks:
            return

        tasks = list(self._tasks.values())
        logger.info("Mimir: shutting down, waiting for %d in-flight verifications", len(tasks))

        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending, timeout=2.0)
        except Exception as e:
            logger.warning("Mimir: shutdown error: %s", e)

        self._tasks.clear()


# ---------------------------------------------------------------------------
# mimir_review -- code quality review (synchronous, fast enough)
# ---------------------------------------------------------------------------

async def mimir_review(event: Event, db) -> list[Emit] | None:
    """Review code quality of a verified task.

    Receives task_verified, calls LLM reviewer,
    emits review_passed or review_rejected.
    """
    task_id = event.payload.get("task_id")
    project_id = event.payload.get("project_id")

    if not task_id:
        return [Emit("mimir_error", {"error": "No task_id"}, source="mimir")]

    row = await db.fetchone(
        "SELECT title, output_text FROM tasks WHERE id = $1",
        (task_id,),
    )
    if not row:
        return [Emit("mimir_error", {"error": f"Task {task_id} not found"}, source="mimir")]

    title = row["title"]
    output_text = row.get("output_text")

    try:
        result = await _call_reviewer(
            task_title=title,
            output_text=output_text or "",
        )
    except Exception as e:
        logger.error("Mimir: reviewer failed for task %s: %s", task_id[:8], e)
        # Review failure is non-blocking -- pass through as approved
        return [Emit("review_passed", {
            "task_id": task_id,
            "project_id": project_id,
            "feedback": f"Review skipped: {e}",
        }, source="mimir")]

    if isinstance(result, list) and len(result) > 0:
        result = result[0] if isinstance(result[0], dict) else {"verdict": "approved", "feedback": str(result)[:200]}
    elif not isinstance(result, dict):
        result = {"verdict": "approved", "feedback": str(result)[:200]}

    verdict = result.get("verdict", "approved")
    feedback = result.get("feedback", "")

    # Review is advisory -- never rejects. Feedback stored for reference.
    # A human decides if feedback warrants reopening the task.
    if feedback:
        await db.execute_write(
            "UPDATE tasks SET verification_notes = $1, updated_at = $2 WHERE id = $3",
            (feedback[:500], time.time(), task_id),
        )

    return [Emit("review_passed", {
        "task_id": task_id,
        "project_id": project_id,
        "verdict": verdict,
        "feedback": feedback,
    }, source="mimir")]


# ---------------------------------------------------------------------------
# mimir_handle_review_rejection -- review_rejected -> reset task
# ---------------------------------------------------------------------------

async def mimir_handle_review_rejection(event: Event, db) -> list[Emit] | None:
    """Handle review_rejected -- reset task to pending with review feedback."""
    task_id = event.payload.get("task_id")
    project_id = event.payload.get("project_id")
    feedback = event.payload.get("feedback", "")

    if not task_id:
        return [Emit("mimir_error", {"error": "No task_id"}, source="mimir")]

    # Read existing context
    row = await db.fetchone(
        "SELECT context_json, retry_count FROM tasks WHERE id = $1", (task_id,))
    if not row:
        return [Emit("mimir_error", {"error": f"Task {task_id} not found"}, source="mimir")]

    existing_ctx = row.get("context_json", "{}")
    retry_count = (row.get("retry_count") or 0)

    try:
        ctx = safe_json.loads_dict(existing_ctx) if isinstance(existing_ctx, str) else (existing_ctx or {})
    except (json.JSONDecodeError, TypeError):
        ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}

    ctx["review_feedback"] = feedback

    await db.execute_write(
        "UPDATE tasks SET status = $1, retry_count = $2, context_json = $3, updated_at = $4 WHERE id = $5",
        ("pending", retry_count + 1, json.dumps(ctx), time.time(), task_id),
    )

    return [Emit("task_reset", {
        "task_id": task_id,
        "project_id": project_id,
        "reason": "review_rejected",
    }, source="mimir")]


# ---------------------------------------------------------------------------
# mimir_handle_task_rejection -- task_rejected -> emit tick for re-dispatch
# ---------------------------------------------------------------------------

async def mimir_handle_task_rejection(event: Event, db) -> list[Emit] | None:
    """Handle task_rejected -- emit tick so odin re-dispatches."""
    project_id = event.payload.get("project_id")

    if not project_id:
        return None

    return [Emit("tick", {
        "project_id": project_id,
    }, source="mimir")]


# ---------------------------------------------------------------------------
# Legacy alias for backwards compatibility
# ---------------------------------------------------------------------------

# Old tests may import mimir_verify directly. Provide a stub that
# creates a temporary runner. In production, use MimirRunner.handle_verify.
async def mimir_verify(event: Event, db) -> list[Emit] | None:
    """Legacy synchronous verify -- creates a one-shot MimirRunner.

    Prefer MimirRunner.handle_verify in production (registered via registration.py).
    This exists for backwards compatibility with tests that import mimir_verify.
    It awaits any background task so the caller gets the final result.
    """
    runner = MimirRunner(db=db, max_concurrent=4)
    emits = await runner.handle_verify(event, db)

    # Wait for background task to complete and read relay results
    if runner._tasks:
        await runner.shutdown(timeout=30.0)
        # Read the relay event that the background task wrote
        rows = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events "
            "WHERE source = 'mimir' ORDER BY id DESC LIMIT 1",
            params=(),
        )
        if rows:
            relay_type = rows[0]["event_type"]
            relay_payload = safe_json.loads_dict(rows[0]["payload"])
            return [Emit(relay_type, relay_payload, source="mimir")]

    return emits
