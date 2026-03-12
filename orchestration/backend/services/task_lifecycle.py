#  Orchestration Engine - Task Lifecycle
#
#  Core task execution flow: dispatch, verify, checkpoint, context forwarding.
#  Extracted from executor.py for modularity.
#
#  Depends on: config.py, services/claude_agent.py, services/ollama_agent.py,
#              services/budget.py, services/progress.py, services/diagnostic_ingest.py,
#              services/model_router.py, services/knowledge_extractor.py,
#              tools/rag.py (_embed_query, RAGIndexCache)
#  Used by:    services/executor.py, routes/external.py

import asyncio
import json
import logging
import random
import time
import uuid

import anthropic
import httpx

from backend.config import (
    CHECKPOINT_ON_RETRY_EXHAUSTED,
    CONTEXT_FORWARD_MAX_CHARS,
    DIAGNOSTIC_RAG_ENABLED,
    KNOWLEDGE_EXTRACTION_ENABLED,
    VERIFICATION_ENABLED,
)
from backend.logging_config import set_task_id
from backend.models.enums import ModelTier, TaskStatus
from backend.services.claude_agent import run_claude_task
from backend.services.ollama_agent import run_ollama_task

logger = logging.getLogger("orchestration.executor")

# Transient errors that warrant automatic retry with backoff
_TRANSIENT_ERRORS = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
    httpx.ConnectError,
    httpx.ReadTimeout,
)

# Maximum verification feedback entries kept in context to prevent unbounded growth
_MAX_VERIFICATION_FEEDBACKS = 3

# Diagnostic RAG confidence threshold — only inject HIGH-confidence results
_DIAGNOSTIC_CONFIDENCE_THRESHOLD = 0.80


async def _search_diagnostic_rag(error_text: str, rag_cache, http_client) -> str | None:
    """Search diagnostic RAG for a known resolution to an error.

    Returns the matching chunk text if a high-confidence result exists,
    None otherwise. Never raises — returns None on any failure.
    """
    try:
        from backend.tools.rag import _embed_query

        idx = await rag_cache.get("diagnostic")
        if not idx or idx._state != "loaded" or idx.embeddings is None:
            return None

        import numpy as np

        query_vec = await _embed_query(error_text, http_client)
        if query_vec is None:
            return None

        similarities = idx.embeddings @ query_vec
        top_idx = int(np.argmax(similarities))
        top_score = float(similarities[top_idx])

        if top_score < _DIAGNOSTIC_CONFIDENCE_THRESHOLD:
            return None

        chunk_id = idx.chunk_ids[top_idx]
        rows = await asyncio.to_thread(
            idx.query_sync, "SELECT text, gotcha FROM chunks WHERE id = ?", (chunk_id,)
        )
        if not rows:
            return None

        text = rows[0]["text"]
        try:
            gotcha = rows[0]["gotcha"] or ""
        except (IndexError, KeyError):
            gotcha = ""

        result = f"[Diagnostic RAG match, score={top_score:.3f}]\n{text}"
        if gotcha:
            result += f"\n[CAUTION: {gotcha}]"
        return result

    except Exception as e:
        logger.debug("Diagnostic RAG search failed: %s", e)
        return None


async def _ingest_retry_success(task_row, output_text: str, db, ingester):
    """Capture a successful retry as a diagnostic resolution.

    Called when a task completes after retry_count > 0 — the last error
    becomes the error_pattern, the successful output becomes the resolution.
    """
    try:
        # Get the last error event for this task
        last_error = await db.fetchone(
            "SELECT message FROM task_events "
            "WHERE task_id = ? AND event_type IN ('task_retry', 'task_failed') "
            "ORDER BY timestamp DESC LIMIT 1",
            (task_row["id"],),
        )
        if not last_error or not last_error["message"]:
            return

        error_text = last_error["message"]
        resolution_text = (output_text or "")[:2000]
        if not resolution_text:
            return

        await ingester.ingest_resolution(
            error_text=error_text,
            resolution_text=f"Task '{task_row['title']}' succeeded after retry: {resolution_text}",
            error_context=f"Task type: {task_row.get('task_type', 'unknown')}, "
                          f"model: {task_row.get('model_tier', 'unknown')}",
            tags=["auto-captured", "retry-success"],
        )
    except Exception as e:
        logger.debug("Failed to ingest retry success: %s", e)


async def create_checkpoint(
    *, project_id, task_id, task_row, error_msg, db, progress,
):
    """Create a checkpoint for a task that exhausted retries.

    Sets the task to NEEDS_REVIEW and creates a structured checkpoint record
    with attempt history for the user to resolve.
    """
    checkpoint_id = uuid.uuid4().hex[:12]

    # Gather attempt history from task_events
    events = await db.fetchall(
        "SELECT message, timestamp FROM task_events "
        "WHERE task_id = ? AND event_type IN ('task_retry', 'task_failed') "
        "ORDER BY timestamp",
        (task_id,),
    )
    attempts = [
        {"message": e["message"], "timestamp": e["timestamp"]}
        for e in events
    ]

    await db.execute_write(
        "INSERT INTO checkpoints "
        "(id, project_id, task_id, checkpoint_type, summary, attempts_json, question, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            checkpoint_id, project_id, task_id, "retry_exhausted",
            f"Task '{task_row['title']}' failed after {task_row['max_retries']} attempts",
            json.dumps(attempts),
            "How should we proceed? Options: retry with modified approach, "
            "skip this task, or fail it.",
            time.time(),
        ),
    )

    await db.execute_write(
        "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
        (TaskStatus.NEEDS_REVIEW, error_msg, time.time(), task_id),
    )

    await progress.push_event(
        project_id, "checkpoint",
        f"Checkpoint: {task_row['title']} needs attention after {task_row['max_retries']} failed attempts",
        task_id=task_id, checkpoint_id=checkpoint_id,
    )


async def verify_task_output(
    *, task_row, output_text, project_id, task_id, db, client, budget, progress,
) -> bool:
    """Run output verification. Returns True if the task status was overridden."""
    from backend.services.verifier import verify_output
    from backend.models.enums import VerificationResult

    try:
        verification = await verify_output(
            task_title=task_row["title"],
            task_description=task_row["description"],
            output_text=output_text,
            client=client,
            budget=budget,
            project_id=project_id,
            task_id=task_id,
        )
    except Exception as e:
        # Verification failure should not block task completion
        logger.warning("Verification failed for task %s: %s", task_id, e)
        await db.execute_write(
            "UPDATE tasks SET verification_status = ?, verification_notes = ?, "
            "updated_at = ? WHERE id = ?",
            (VerificationResult.SKIPPED, f"Verification error: {e}",
             time.time(), task_id),
        )
        return False

    v_result = verification["result"]
    v_notes = verification["notes"]

    await db.execute_write(
        "UPDATE tasks SET verification_status = ?, verification_notes = ?, "
        "updated_at = ? WHERE id = ?",
        (v_result, v_notes, time.time(), task_id),
    )

    if v_result == VerificationResult.GAPS_FOUND:
        retry_count = task_row["retry_count"]
        max_retries = task_row["max_retries"]
        if retry_count < max_retries:
            # Auto-retry with verification feedback appended to context.
            # Sliding window: keep the most recent feedbacks up to the cap.
            ctx = json.loads(task_row["context_json"]) if task_row["context_json"] else []
            non_feedbacks = [e for e in ctx if e.get("type") != "verification_feedback"]
            feedbacks = [e for e in ctx if e.get("type") == "verification_feedback"]
            if len(feedbacks) >= _MAX_VERIFICATION_FEEDBACKS:
                feedbacks = feedbacks[-(_MAX_VERIFICATION_FEEDBACKS - 1):]
            feedbacks.append({
                "type": "verification_feedback",
                "content": f"Previous attempt had gaps: {v_notes}. Address these issues.",
            })
            ctx = non_feedbacks + feedbacks
            await db.execute_write(
                "UPDATE tasks SET status = ?, context_json = ?, "
                "retry_count = retry_count + 1, completed_at = NULL, updated_at = ? WHERE id = ?",
                (TaskStatus.PENDING, json.dumps(ctx), time.time(), task_id),
            )
            await progress.push_event(
                project_id, "task_verification_retry",
                f"{task_row['title']}: gaps found, retrying with feedback",
                task_id=task_id, verification_notes=v_notes,
            )
            return True

    if v_result == VerificationResult.HUMAN_NEEDED:
        await db.execute_write(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (TaskStatus.NEEDS_REVIEW, time.time(), task_id),
        )
        await progress.push_event(
            project_id, "task_needs_review",
            f"{task_row['title']}: requires human review",
            task_id=task_id, verification_notes=v_notes,
        )
        return True

    return False


async def forward_context(*, completed_task, output_text, db):
    """Inject completed task's output summary into dependent tasks' context."""
    deps = await db.fetchall(
        "SELECT task_id FROM task_deps WHERE depends_on = ?",
        (completed_task["id"],),
    )
    if not deps:
        return

    # Truncate output to configured max for context injection
    summary = (output_text or "")[:CONTEXT_FORWARD_MAX_CHARS]
    context_entry = {
        "type": "dependency_output",
        "source_task_id": completed_task["id"],
        "source_task_title": completed_task["title"],
        "content": summary,
    }

    for dep in deps:
        # Wrap each read-modify-write in a transaction to prevent
        # concurrent upstream completions from clobbering each other.
        async with db.transaction():
            dep_task = await db.fetchone(
                "SELECT context_json FROM tasks WHERE id = ?", (dep["task_id"],),
            )
            if dep_task:
                ctx = json.loads(dep_task["context_json"]) if dep_task["context_json"] else []
                ctx.append(context_entry)
                await db.execute_write(
                    "UPDATE tasks SET context_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(ctx), time.time(), dep["task_id"]),
                )


async def execute_task(
    *,
    task_row,
    est_cost: float = 0.0,
    db,
    budget,
    progress,
    tool_registry,
    http_client,
    client,
    semaphore,
    dispatched: set,
    retry_after: dict,
    rag_cache=None,
    diagnostic_ingester=None,
):
    """Execute a single task with semaphore-controlled concurrency.

    Args:
        task_row: Task database row.
        est_cost: Budget reservation estimate (0 for Ollama).
        db: Database instance.
        budget: BudgetManager instance.
        progress: ProgressManager instance.
        tool_registry: ToolRegistry for tool definitions.
        http_client: Shared httpx.AsyncClient (or None).
        client: anthropic.AsyncAnthropic instance.
        semaphore: asyncio.Semaphore for concurrency control.
        dispatched: Mutable set of currently dispatched task IDs.
        retry_after: Mutable dict of task_id → earliest retry timestamp.
        rag_cache: RAGIndexCache for diagnostic search (optional).
        diagnostic_ingester: DiagnosticIngester for feedback loop (optional).
    """
    task_id = task_row["id"]
    set_task_id(task_id)
    try:
        async with semaphore:
            project_id = task_row["project_id"]
            tier = ModelTier(task_row["model_tier"])

            # Mark as running
            now = time.time()
            await db.execute_write(
                "UPDATE tasks SET status = ?, started_at = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.RUNNING, now, now, task_id),
            )
            await progress.push_event(
                project_id, "task_start", task_row["title"], task_id=task_id
            )

            try:
                if tier == ModelTier.OLLAMA:
                    result = await run_ollama_task(
                        task_row=task_row, http_client=http_client, budget=budget,
                    )
                else:
                    result = await run_claude_task(
                        task_row=task_row, est_cost=est_cost, client=client,
                        tool_registry=tool_registry, budget=budget, progress=progress,
                        db=db,
                    )

                # Handle budget exhaustion — mark for review, don't complete
                retry_after.pop(task_id, None)
                if result.get("budget_exhausted"):
                    await db.execute_write(
                        "UPDATE tasks SET status = ?, output_text = ?, error = ?, "
                        "prompt_tokens = ?, completion_tokens = ?, cost_usd = ?, "
                        "model_used = ?, updated_at = ? WHERE id = ?",
                        (
                            TaskStatus.NEEDS_REVIEW, result["output"],
                            "Budget exhausted mid-execution (partial output)",
                            result["prompt_tokens"], result["completion_tokens"],
                            result["cost_usd"], result["model_used"],
                            time.time(), task_id,
                        ),
                    )
                    await progress.push_event(
                        project_id, "task_needs_review",
                        f"{task_row['title']}: budget exhausted, partial output needs review",
                        task_id=task_id,
                    )
                    return

                # Mark completed
                await db.execute_write(
                    "UPDATE tasks SET status = ?, output_text = ?, "
                    "prompt_tokens = ?, completion_tokens = ?, cost_usd = ?, "
                    "model_used = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                    (
                        TaskStatus.COMPLETED, result["output"],
                        result["prompt_tokens"], result["completion_tokens"],
                        result["cost_usd"], result["model_used"],
                        time.time(), time.time(), task_id,
                    ),
                )

                # Optional output verification (skip for Ollama — free tasks)
                # Run BEFORE forwarding context to prevent dependents from
                # receiving output that verification may reject.
                if VERIFICATION_ENABLED and tier != ModelTier.OLLAMA and client:
                    verification_overridden = await verify_task_output(
                        task_row=task_row, output_text=result["output"],
                        project_id=project_id, task_id=task_id,
                        db=db, client=client, budget=budget, progress=progress,
                    )
                    if verification_overridden:
                        return  # Task was reset to PENDING or NEEDS_REVIEW

                await progress.push_event(
                    project_id, "task_complete", task_row["title"],
                    task_id=task_id, cost_usd=result["cost_usd"],
                )

                # Forward output to dependent tasks' context (only after
                # verification passes — do not forward retried/reviewed output)
                await forward_context(
                    completed_task=task_row, output_text=result["output"], db=db,
                )

                # Learn from success-after-retry: capture the error→resolution pair
                if (DIAGNOSTIC_RAG_ENABLED and diagnostic_ingester
                        and task_row["retry_count"] > 0):
                    await _ingest_retry_success(
                        task_row, result["output"], db, diagnostic_ingester,
                    )

                # Extract reusable knowledge from task output
                if KNOWLEDGE_EXTRACTION_ENABLED and tier != ModelTier.OLLAMA and client:
                    from backend.services.knowledge_extractor import extract_knowledge
                    await extract_knowledge(
                        task_title=task_row["title"],
                        task_description=task_row["description"],
                        output_text=result["output"],
                        client=client,
                        budget=budget,
                        project_id=project_id,
                        task_id=task_id,
                        db=db,
                    )

            except _TRANSIENT_ERRORS as e:
                retry_count = task_row["retry_count"]
                max_retries = task_row["max_retries"]
                if retry_count < max_retries:
                    # Search diagnostic RAG for known resolutions before retry
                    diagnostic_ctx = None
                    if DIAGNOSTIC_RAG_ENABLED and rag_cache:
                        diagnostic_ctx = await _search_diagnostic_rag(
                            str(e), rag_cache, http_client,
                        )

                    # Schedule retry via retry_after instead of sleeping
                    # inside the semaphore. The tick loop will re-dispatch
                    # once the backoff period expires.
                    delay = min(5 * (2 ** retry_count) + random.uniform(0, 2), 120)
                    retry_after[task_id] = time.time() + delay

                    # Inject diagnostic suggestion into task context if found
                    if diagnostic_ctx:
                        task_ctx = await db.fetchone(
                            "SELECT context_json FROM tasks WHERE id = ?", (task_id,),
                        )
                        ctx = json.loads(task_ctx["context_json"]) if task_ctx and task_ctx["context_json"] else []
                        ctx.append({
                            "type": "diagnostic_suggestion",
                            "content": diagnostic_ctx,
                        })
                        await db.execute_write(
                            "UPDATE tasks SET status = ?, retry_count = retry_count + 1, "
                            "error = ?, context_json = ?, updated_at = ? WHERE id = ?",
                            (TaskStatus.PENDING, f"Transient error (retry {retry_count + 1}): {e}",
                             json.dumps(ctx), time.time(), task_id),
                        )
                    else:
                        await db.execute_write(
                            "UPDATE tasks SET status = ?, retry_count = retry_count + 1, "
                            "error = ?, updated_at = ? WHERE id = ?",
                            (TaskStatus.PENDING, f"Transient error (retry {retry_count + 1}): {e}",
                             time.time(), task_id),
                        )
                    await progress.push_event(
                        project_id, "task_retry",
                        f"{task_row['title']}: retrying in {delay:.0f}s ({e})",
                        task_id=task_id,
                    )
                else:
                    retry_after.pop(task_id, None)
                    error_msg = f"Max retries exceeded: {e}"

                    if CHECKPOINT_ON_RETRY_EXHAUSTED:
                        await create_checkpoint(
                            project_id=project_id, task_id=task_id,
                            task_row=task_row, error_msg=error_msg,
                            db=db, progress=progress,
                        )
                    else:
                        await db.execute_write(
                            "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                            (TaskStatus.FAILED, error_msg, time.time(), task_id),
                        )
                        await progress.push_event(
                            project_id, "task_failed", f"{task_row['title']}: {error_msg}",
                            task_id=task_id,
                        )

            except Exception as e:
                retry_after.pop(task_id, None)
                error_msg = str(e)
                await db.execute_write(
                    "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.FAILED, error_msg, time.time(), task_id),
                )
                await progress.push_event(
                    project_id, "task_failed", f"{task_row['title']}: {error_msg}",
                    task_id=task_id,
                )
    finally:
        set_task_id(None)
        dispatched.discard(task_id)
        if est_cost > 0:
            await budget.release_reservation(est_cost)
            await budget.release_reservation_project(task_row["project_id"], est_cost)


async def complete_task_external(
    *,
    task_id,
    task_row,
    project_id,
    output_text,
    model_used,
    prompt_tokens,
    completion_tokens,
    db,
    budget,
    progress,
):
    """Process an externally-submitted task result.

    Handles: cost recording, marking complete, context forwarding,
    and knowledge extraction. Verification is skipped for external tasks
    (the external executor is trusted for now).

    Returns dict with status and optional verification fields.
    """
    from backend.services.model_router import calculate_cost

    # Calculate cost from tokens
    cost_usd = calculate_cost(model_used, prompt_tokens, completion_tokens)

    # Record spend
    await budget.record_spend(
        cost_usd=cost_usd,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        provider="anthropic",
        model=model_used,
        project_id=project_id,
        task_id=task_id,
        purpose="external_execution",
    )

    # Mark task completed
    now = time.time()
    await db.execute_write(
        "UPDATE tasks SET status = ?, output_text = ?, model_used = ?, "
        "prompt_tokens = ?, completion_tokens = ?, cost_usd = ?, "
        "completed_at = ?, updated_at = ? WHERE id = ?",
        (
            TaskStatus.COMPLETED, output_text, model_used,
            prompt_tokens, completion_tokens, cost_usd,
            now, now, task_id,
        ),
    )

    await progress.push_event(
        project_id, "task_complete", task_row["title"],
        task_id=task_id, cost_usd=cost_usd,
    )

    # Forward context to dependent tasks
    await forward_context(
        completed_task=task_row, output_text=output_text, db=db,
    )

    # Extract knowledge (best-effort, non-blocking)
    if KNOWLEDGE_EXTRACTION_ENABLED:
        try:
            import anthropic as anthropic_mod
            client = anthropic_mod.AsyncAnthropic()
            from backend.services.knowledge_extractor import extract_knowledge
            await extract_knowledge(
                task_title=task_row["title"],
                task_description=task_row["description"],
                output_text=output_text,
                client=client,
                budget=budget,
                project_id=project_id,
                task_id=task_id,
                db=db,
            )
        except Exception as e:
            logger.warning("Knowledge extraction failed for external task %s: %s", task_id, e)

    return {"status": TaskStatus.COMPLETED}


async def verify_csharp_build(csproj_path: str) -> tuple[bool, str]:
    """Run dotnet build as a verification step for C# tasks.

    Returns (success, output). On failure, output contains compiler errors
    suitable for injection as retry feedback.
    """
    from backend.tools.dotnet_reflection import _run_subprocess

    code, stdout, stderr = await _run_subprocess(
        ["dotnet", "build", csproj_path, "-c", "Release", "--nologo", "-v", "q"],
        timeout=120,
    )
    if code == 0:
        return True, "Build succeeded"

    # Extract just the error lines for concise feedback
    output = stderr or stdout
    error_lines = [
        line for line in output.splitlines()
        if "error CS" in line or "error :" in line
    ]
    if error_lines:
        return False, "Build errors:\n" + "\n".join(error_lines[:20])
    return False, f"Build failed:\n{output[:2000]}"
