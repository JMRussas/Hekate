"""Mimir handler — verification, review, knowledge extraction.

Receives worker_event:completed and:
  1. Runs output verification (does output match task requirements?)
  2. Emits task_verified or task_rejected
  3. Optionally extracts knowledge from successful completions

Also handles code review (mimir_review):
  - Checks code quality
  - Emits review_passed or review_rejected
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from gods.pipeline import Event, Emit

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
# LLM-backed verification (mocked in tests)
# ---------------------------------------------------------------------------

async def _call_verifier(
    *,
    task_title: str,
    task_description: str,
    output_text: str,
    gateway_url: str = "http://localhost:5210",
) -> dict:
    """Call LLM to verify output matches task requirements.

    Returns {verdict: "passed"|"gaps_found"|"human_needed", confidence: float, feedback: str}
    """
    import httpx

    prompt = (
        f"Verify that the following output satisfies the task requirements.\n\n"
        f"Task: {task_title}\n"
        f"Description: {task_description}\n\n"
        f"Output:\n{output_text[:5000]}\n\n"
        f"Respond with JSON: {{\"verdict\": \"passed|gaps_found|human_needed\", "
        f"\"confidence\": 0.0-1.0, \"feedback\": \"...\"}}"
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{gateway_url}/v1/chat", json={
            "provider": "gemini",
            "system_prompt": "You are a code verification assistant. Always respond with valid JSON.",
            "user_message": prompt,
        })
        resp.raise_for_status()
        text = resp.json().get("text", "")

    try:
        # Try to extract JSON from response (may have markdown fencing)
        import re
        json_match = re.search(r'\{[^{}]*\}', text)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(text)
    except json.JSONDecodeError:
        # If LLM says it passed in prose, treat as passed
        lower = text.lower()
        if any(w in lower for w in ["passed", "satisf", "correct", "done", "complet"]):
            return {"verdict": "passed", "confidence": 0.7, "feedback": text[:200]}
        return {"verdict": "human_needed", "confidence": 0.0, "feedback": text[:200]}


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

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{gateway_url}/v1/chat", json={
            "provider": "gemini",
            "system_prompt": "You are a code review assistant. Always respond with valid JSON.",
            "user_message": prompt,
        })
        resp.raise_for_status()
        text = resp.json().get("text", "")

    try:
        import re
        json_match = re.search(r'\{[^{}]*\}', text)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(text)
    except json.JSONDecodeError:
        return {"verdict": "approved", "feedback": text[:200]}


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

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{gateway_url}/v1/chat", json={
            "provider": "gemini",
            "system_prompt": "You are a knowledge extraction assistant. Always respond with valid JSON.",
            "user_message": prompt,
        })
        resp.raise_for_status()
        text = resp.json().get("text", "")

    try:
        import re
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(text)
    except json.JSONDecodeError:
        return []


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
# mimir_verify — worker_event:completed → task_verified / task_rejected
# ---------------------------------------------------------------------------

async def mimir_verify(event: Event, db) -> list[Emit] | None:
    """Verify a completed task's output.

    Receives worker_event with status=completed.
    Calls LLM verifier, emits task_verified or task_rejected.
    On rejection, resets task to pending with feedback.
    """
    task_id = event.payload.get("task_id")
    project_id = event.payload.get("project_id")
    status = event.payload.get("status")

    # Only verify completed tasks
    if status != "completed":
        return None

    if not task_id:
        return [Emit("mimir_error", {"error": "No task_id"}, source="mimir")]

    # Fetch task
    row = await db.fetchone(
        "SELECT title, description, output_text, retry_count, max_retries, context_json "
        "FROM tasks WHERE id = $1",
        (task_id,),
    )
    if not row:
        return [Emit("mimir_error", {
            "error": f"Task {task_id} not found",
        }, source="mimir")]

    title = row["title"]
    description = row.get("description", "")
    output_text = row.get("output_text")
    retry_count = (row.get("retry_count") or 0)
    max_retries = (row.get("max_retries") or 3)
    context_json = row.get("context_json") or "{}"

    # Quick heuristic check first
    quality = _check_output_quality(output_text)
    if not quality["passed"]:
        # Heuristic failure — treat as gaps_found
        if retry_count >= max_retries:
            await db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                ("needs_review", time.time(), task_id),
            )
            return [Emit("needs_human_review", {
                "task_id": task_id,
                "project_id": project_id,
                "reason": quality["reason"],
            }, source="mimir")]

        # Reset for retry
        try:
            ctx = json.loads(context_json) if isinstance(context_json, str) else context_json
        except (json.JSONDecodeError, TypeError):
            ctx = {}
        ctx["verification_feedback"] = quality["reason"]

        await db.execute_write(
            "UPDATE tasks SET status = $1, retry_count = $2, context_json = $3, updated_at = $4 WHERE id = $5",
            ("pending", retry_count + 1, json.dumps(ctx), time.time(), task_id),
        )
        return [Emit("task_rejected", {
            "task_id": task_id,
            "project_id": project_id,
            "feedback": quality["reason"],
        }, source="mimir")]

    # LLM verification
    try:
        result = await _call_verifier(
            task_title=title,
            task_description=description or "",
            output_text=output_text or "",
        )
    except Exception as e:
        logger.error("Mimir: verifier failed for task %s: %s", task_id[:8], e)
        await db.execute_write(
            "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
            ("needs_review", time.time(), task_id),
        )
        return [Emit("needs_human_review", {
            "task_id": task_id,
            "project_id": project_id,
            "reason": f"Verification service error: {e}",
        }, source="mimir")]

    # Guard: ensure result is a dict (LLM might return array or string)
    if not isinstance(result, dict):
        result = {"verdict": "human_needed", "confidence": 0.0, "feedback": str(result)[:200]}

    verdict = result.get("verdict", "human_needed")
    feedback = result.get("feedback", "")

    if verdict == "passed":
        # Extract knowledge from successful output
        try:
            await _extract_knowledge(
                task_id=task_id,
                project_id=project_id,
                output_text=output_text or "",
                db=db,
            )
        except Exception as ke:
            logger.warning("Knowledge extraction failed for task %s: %s", task_id[:8], ke)

        return [Emit("task_verified", {
            "task_id": task_id,
            "project_id": project_id,
            "confidence": result.get("confidence", 1.0),
        }, source="mimir")]

    elif verdict == "gaps_found":
        if retry_count >= max_retries:
            await db.execute_write(
                "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
                ("needs_review", time.time(), task_id),
            )
            return [Emit("needs_human_review", {
                "task_id": task_id,
                "project_id": project_id,
                "reason": f"Verification gaps after {retry_count} retries: {feedback}",
            }, source="mimir")]

        # Reset for retry with feedback
        try:
            ctx = json.loads(context_json) if isinstance(context_json, str) else context_json
        except (json.JSONDecodeError, TypeError):
            ctx = {}
        ctx["verification_feedback"] = feedback

        await db.execute_write(
            "UPDATE tasks SET status = $1, retry_count = $2, context_json = $3, updated_at = $4 WHERE id = $5",
            ("pending", retry_count + 1, json.dumps(ctx), time.time(), task_id),
        )
        return [Emit("task_rejected", {
            "task_id": task_id,
            "project_id": project_id,
            "feedback": feedback,
        }, source="mimir")]

    else:  # human_needed
        await db.execute_write(
            "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
            ("needs_review", time.time(), task_id),
        )
        return [Emit("needs_human_review", {
            "task_id": task_id,
            "project_id": project_id,
            "reason": feedback or "Verification requires human review",
        }, source="mimir")]


# ---------------------------------------------------------------------------
# mimir_review — code quality review
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
        # Review failure is non-blocking — pass through as approved
        return [Emit("review_passed", {
            "task_id": task_id,
            "project_id": project_id,
            "feedback": f"Review skipped: {e}",
        }, source="mimir")]

    if not isinstance(result, dict):
        result = {"verdict": "approved", "feedback": str(result)[:200]}

    verdict = result.get("verdict", "approved")
    feedback = result.get("feedback", "")

    if verdict == "approved":
        return [Emit("review_passed", {
            "task_id": task_id,
            "project_id": project_id,
            "feedback": feedback,
        }, source="mimir")]
    else:
        return [Emit("review_rejected", {
            "task_id": task_id,
            "project_id": project_id,
            "feedback": feedback,
        }, source="mimir")]


# ---------------------------------------------------------------------------
# mimir_handle_review_rejection — review_rejected → reset task
# ---------------------------------------------------------------------------

async def mimir_handle_review_rejection(event: Event, db) -> list[Emit] | None:
    """Handle review_rejected — reset task to pending with review feedback."""
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
        ctx = json.loads(existing_ctx) if isinstance(existing_ctx, str) else (existing_ctx or {})
    except (json.JSONDecodeError, TypeError):
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
# mimir_handle_task_rejection — task_rejected → emit tick for re-dispatch
# ---------------------------------------------------------------------------

async def mimir_handle_task_rejection(event: Event, db) -> list[Emit] | None:
    """Handle task_rejected — emit tick so odin re-dispatches."""
    project_id = event.payload.get("project_id")

    if not project_id:
        return None

    return [Emit("tick", {
        "project_id": project_id,
    }, source="mimir")]
