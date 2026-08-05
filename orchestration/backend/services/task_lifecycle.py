#  Orchestration Engine - Task Lifecycle
#
#  Core task execution flow: dispatch, verify, checkpoint, context forwarding,
#  telemetry feedback.
#  Extracted from executor.py for modularity.
#
#  Depends on: config.py, services/claude_agent.py, services/claude_code_executor.py,
#              services/ollama_agent.py,
#              services/budget.py, services/progress.py, services/diagnostic_ingest.py,
#              services/model_router.py, services/knowledge_extractor.py,
#              services/enrichment_service.py (lazy, pre-dispatch),
#              services/telemetry_feedback.py (push_execution_outcome),
#              tools/rag.py (_embed_query, RAGIndexCache)
#  Used by:    services/executor.py, routes/external.py

import asyncio
import glob
import json
import logging
import os
import random
import re
import time
import uuid

import anthropic
import httpx

from backend.config import (
    ANTHROPIC_API_KEY,
    CHECKPOINT_ON_RETRY_EXHAUSTED,
    CONTEXT_ENRICHMENT_ENABLED,
    CONTEXT_FORWARD_MAX_CHARS,
    DIAGNOSTIC_RAG_ENABLED,
    INTERROGATION_ENABLED,
    INTERROGATION_MODEL,
    KNOWLEDGE_EXTRACTION_ENABLED,
    REVIEW_AUTO_COMMIT,
    REVIEW_CYCLE_ENABLED,
    REVIEW_MAX_ITERATIONS,
    VERIFICATION_ENABLED,
)
from backend.logging_config import set_task_id
from backend.db.connection import parse_rowcount
from backend.models.enums import ModelTier, TaskStatus, VerificationResult
from backend.services.claude_agent import run_claude_task
from backend.services.claude_code_executor import run_claude_code_task
from backend.services.generic_cli_executor import run_gemini_cli_task, run_codex_cli_task
from backend.services.ollama_agent import run_ollama_task
from backend.services.telemetry_feedback import push_execution_outcome

logger = logging.getLogger("orchestration.executor")


async def _sync_status_to_context_store(
    *, db, task_title: str, plan_id: str, status: str, error: str | None = None,
):
    """Fire-and-forget: push a task status change to the context store node.

    Loads the node_mapping (title → node_id) from the plans table, finds the
    context store node for this task by title, and updates its attributes.
    Silently no-ops if the mapping doesn't exist or the context store is
    unreachable (circuit breaker open).
    """
    try:
        from backend.services.context_store_client import ContextStoreClient
        from backend.services.plan_sync import PlanSyncService
        cs = ContextStoreClient()

        # Circuit breaker check — skip early if context store is down
        if cs._is_circuit_open():
            return

        svc = PlanSyncService(context_client=cs, db=db)
        mapping = await svc.get_node_mapping(plan_id)
        node_id = mapping.get(task_title)
        if not node_id:
            return

        attrs: dict = {"status": status, "updated_at": time.time()}
        if error:
            attrs["error"] = error[:500]
        await cs.update_attributes(node_id, attrs)
    except Exception:
        # Never propagate — this is fire-and-forget
        logger.debug("Context store status sync failed for task %s", task_title, exc_info=True)

SOURCE_ROOT = os.environ.get("HEKATE_SOURCE", r"C:\Users\jruss\Documents\GitHub\Hekate")


async def _ensure_workspace(cwd: str, db, project_id: str):
    """Ensure a working directory exists and has CLI config files.

    Called before every task dispatch. Creates the directory if missing,
    copies .mcp.json and CLAUDE.md templates, and inits git if needed.
    """
    try:
        # Create directory if it doesn't exist
        if not os.path.isdir(cwd):
            os.makedirs(cwd, exist_ok=True)
            logger.info("Created workspace directory: %s", cwd)

        # Ensure git repo exists
        git_dir = os.path.join(cwd, ".git")
        if not os.path.isdir(git_dir):
            import subprocess
            subprocess.run(["git", "init"], cwd=cwd, capture_output=True, timeout=10)
            logger.info("Initialized git repo in %s", cwd)

        # Copy .mcp.json if missing (gives Claude CLI access to MCP tools)
        mcp_target = os.path.join(cwd, ".mcp.json")
        if not os.path.isfile(mcp_target):
            mcp_source = os.path.join(SOURCE_ROOT, "context-store", ".mcp.json")
            if os.path.isfile(mcp_source):
                import shutil
                shutil.copy2(mcp_source, mcp_target)
                logger.info("Copied .mcp.json to %s", cwd)

        # Copy CLAUDE.md if missing (gives CLI context about the project)
        claude_md_target = os.path.join(cwd, "CLAUDE.md")
        if not os.path.isfile(claude_md_target):
            # Check if project has a specific repo with its own CLAUDE.md
            # If not, use a minimal template
            _write_default_claude_md(claude_md_target, db, project_id)

    except Exception as e:
        logger.warning("Workspace setup failed for %s: %s", cwd, e)


def _write_default_claude_md(path: str, db, project_id: str):
    """Write a minimal CLAUDE.md for projects that don't have one."""
    try:
        with open(path, "w") as f:
            f.write("# Project Workspace\n\n")
            f.write("This workspace is managed by the Hekate orchestration engine.\n\n")
            f.write("## Available MCP Tools\n\n")
            f.write("The `.mcp.json` in this directory provides:\n")
            f.write("- `search_ideas` — search the knowledge store\n")
            f.write("- `get_node_details` — inspect a node in depth\n")
            f.write("- `list_threads` — list conversation threads\n")
            f.write("- `route_to_model` — ask another AI model a question\n")
        logger.info("Created default CLAUDE.md at %s", path)
    except Exception as e:
        logger.debug("Failed to write CLAUDE.md: %s", e)


# Transient errors that warrant automatic retry with backoff
_TRANSIENT_ERRORS = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
    httpx.ConnectError,
    httpx.ReadTimeout,
    asyncio.TimeoutError,
)

# Maximum verification feedback entries kept in context to prevent unbounded growth
_MAX_VERIFICATION_FEEDBACKS = 3

# Diagnostic RAG confidence threshold — only inject HIGH-confidence results
_DIAGNOSTIC_CONFIDENCE_THRESHOLD = 0.80


def _row_get(row, key: str, default=None):
    """Safe .get() for sqlite3.Row which doesn't support .get()."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


_MIGRATION_REVISION_RE = re.compile(r"^(\d{3})_")


async def validate_migration_files(
    *, db, project_id: str, task_id: str, output_text: str,
) -> list[str]:
    """Scan for new/modified migration files and validate conventions.

    Checks:
    - File name starts with NNN_ (three-digit revision).
    - ``revision`` variable inside the file matches that NNN.
    - ``down_revision`` matches the previous highest NNN in the versions dir.

    Returns a list of error strings (empty = all valid).
    Creates sentinel observations for each violation.
    """
    from backend.services.cli_common import resolve_cwd

    cwd = await resolve_cwd(db, project_id)
    if not cwd:
        return []

    versions_dir = os.path.join(cwd, "backend", "migrations", "versions")
    if not os.path.isdir(versions_dir):
        return []

    # Collect all NNN-prefixed migration files
    migration_files = sorted(glob.glob(os.path.join(versions_dir, "*.py")))
    revisions: dict[str, str] = {}  # NNN -> filepath
    for fpath in migration_files:
        fname = os.path.basename(fpath)
        m = _MIGRATION_REVISION_RE.match(fname)
        if m:
            revisions[m.group(1)] = fpath

    if not revisions:
        return []

    # Determine which files were created/modified by this task.
    # Heuristic: check output_text for mentions of migration version files,
    # and also check any file whose mtime is within the last 10 minutes.
    cutoff = time.time() - 600  # 10 minutes ago
    candidate_files: list[tuple[str, str]] = []  # (NNN, filepath)
    for nnn, fpath in revisions.items():
        fname = os.path.basename(fpath)
        recently_modified = False
        try:
            recently_modified = os.path.getmtime(fpath) > cutoff
        except OSError:
            pass
        if recently_modified or fname in (output_text or ""):
            candidate_files.append((nnn, fpath))

    if not candidate_files:
        return []

    errors: list[str] = []
    sorted_revisions = sorted(revisions.keys())

    for nnn, fpath in candidate_files:
        fname = os.path.basename(fpath)

        # Validate filename pattern
        if not _MIGRATION_REVISION_RE.match(fname):
            errors.append(f"Migration file '{fname}' does not follow NNN_description.py naming")
            continue

        # Read file and validate revision/down_revision variables
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            errors.append(f"Could not read migration file '{fname}': {e}")
            continue

        # Extract revision = 'NNN'
        rev_match = re.search(r"""^revision:\s*str\s*=\s*['"](\S+?)['"]""", content, re.MULTILINE)
        if not rev_match:
            rev_match = re.search(r"""^revision\s*=\s*['"](\S+?)['"]""", content, re.MULTILINE)
        if not rev_match:
            errors.append(f"Migration '{fname}': could not find revision variable")
            continue

        file_revision = rev_match.group(1)
        if file_revision != nnn:
            errors.append(
                f"Migration '{fname}': revision '{file_revision}' does not match "
                f"filename prefix '{nnn}'"
            )

        if not re.fullmatch(r"\d{3}", file_revision):
            errors.append(
                f"Migration '{fname}': revision '{file_revision}' is not a three-digit NNN string"
            )

        # Extract down_revision
        down_match = re.search(
            r"""^down_revision[^=]*=\s*['"](\S+?)['"]""", content, re.MULTILINE,
        )
        if not down_match:
            # down_revision = None is valid only for the first migration (001)
            if nnn != sorted_revisions[0]:
                errors.append(
                    f"Migration '{fname}': down_revision is None but this is not "
                    f"the first migration"
                )
            continue

        down_rev = down_match.group(1)
        # Find expected previous revision
        idx = sorted_revisions.index(nnn) if nnn in sorted_revisions else -1
        if idx > 0:
            expected_prev = sorted_revisions[idx - 1]
            if down_rev != expected_prev:
                errors.append(
                    f"Migration '{fname}': down_revision '{down_rev}' does not match "
                    f"expected previous revision '{expected_prev}'"
                )
        elif idx == 0:
            # First migration should have down_revision = None, but it has a value
            errors.append(
                f"Migration '{fname}': first migration should have down_revision = None, "
                f"got '{down_rev}'"
            )

    # Publish sentinel observations for any errors
    if errors:
        for err in errors:
            logger.error("Migration validation failed for task %s: %s", task_id, err)

        try:
            from backend.services.sentinel.models import SentinelObservation, Severity
            from backend.services.sentinel.context_client import SentinelContextClient

            observation = SentinelObservation(
                category="migration_validation",
                message=f"Migration validation failed: {len(errors)} issue(s) found",
                severity=Severity.WARNING,
                project_id=project_id,
                task_id=task_id,
                details={"errors": errors},
            )

            ctx_client = SentinelContextClient()
            await ctx_client.save_observation(observation, parent_id=project_id)
        except Exception:
            logger.debug(
                "Failed to persist migration validation observation for task %s",
                task_id, exc_info=True,
            )

    return errors


def _get_provider_from_tier(tier: ModelTier) -> str:
    """Map ModelTier to provider name for telemetry."""
    from backend.services.model_router import get_provider_for_tier
    return get_provider_for_tier(tier)


def _get_provider_from_model_name(model: str) -> str:
    """Infer provider from model name for external executions."""
    if not model:
        return "unknown"
    model_lower = model.lower()
    if "claude" in model_lower:
        return "anthropic"
    elif "gemini" in model_lower:
        return "gemini"
    elif "gpt" in model_lower or "openai" in model_lower:
        return "openai"
    else:
        return "unknown"


async def _push_telemetry(
    *,
    task_row,
    tier: ModelTier,
    project_id: str,
    task_id: str,
    status: str,
    verification_outcome: str | None = None,
    result: dict | None = None,
    http_client=None,
):
    """Push execution telemetry — shared by all completion/failure paths."""
    started_at = _row_get(task_row, "started_at") or time.time()
    duration = time.time() - started_at
    provider = _get_provider_from_tier(tier)
    await push_execution_outcome(
        task_id=task_id,
        project_id=project_id,
        task_title=task_row["title"],
        task_description=task_row["description"],
        task_type=task_row["task_type"],
        provider=provider,
        model=result["model_used"] if result else _row_get(task_row, "model_used", "unknown"),
        prompt_tokens=result["prompt_tokens"] if result else 0,
        completion_tokens=result["completion_tokens"] if result else 0,
        duration_seconds=duration,
        status=status,
        verification_outcome=verification_outcome,
        http_client=http_client,
    )


async def _run_review_cycle(
    *,
    task_row,
    task_id: str,
    project_id: str,
    result: dict,
    tier: ModelTier,
    db,
    budget,
    progress,
    client,
    tool_registry,
    http_client,
    semaphore,
    dispatched,
    retry_after,
) -> bool:
    """Run the code review cycle: review → iterate → commit.

    Returns True if the task was re-queued (review requested changes),
    False if the review passed (caller should proceed to completion).
    """
    from backend.services.code_reviewer import review_code, format_review_feedback
    from backend.services.cli_common import resolve_cwd

    cwd = await resolve_cwd(db, project_id)
    if not cwd:
        logger.debug("No repo_path for project %s, skipping review cycle", project_id)
        return False

    # Get git diff for review — scoped to task's affected_files only.
    # This prevents unrelated dirty files (e.g., .claude/settings.local.json)
    # from polluting the review with false security warnings.
    diff_text = None
    try:
        from backend.services.git_service import GitService
        git = GitService(db=db)
        status = await git.get_status(cwd)
        if status.strip():
            # Scope diff to affected_files if available
            ctx_entries = json.loads(task_row["context_json"] or "[]")
            affected = []
            for entry in ctx_entries:
                if entry.get("type") == "affected_files":
                    affected = entry.get("content", "").split(", ")
                    break
            if not affected:
                # Fallback: parse affected_files from task row (if column exists)
                affected_json = _row_get(task_row, "affected_files") or "[]"
                if isinstance(affected_json, str):
                    try:
                        affected = json.loads(affected_json)
                    except json.JSONDecodeError:
                        affected = []

            if affected and any(f.strip() for f in affected):
                # Diff only the task's files
                file_args = [f.strip() for f in affected if f.strip()]
                diff_text = await asyncio.to_thread(
                    git._run_git_sync, "diff", "--no-color", "--", *file_args, cwd=cwd,
                )
            else:
                # No affected_files — diff entire working tree
                diff_text = await git.get_diff_working(cwd)
    except Exception as e:
        logger.debug("Failed to get git diff for review: %s", e)

    # Get the review iteration count from context
    ctx = json.loads(task_row["context_json"] or "[]")
    review_iterations = sum(1 for c in ctx if c.get("type") == "review_feedback")

    prior_feedback = None
    if review_iterations > 0:
        feedbacks = [c for c in ctx if c.get("type") == "review_feedback"]
        if feedbacks:
            prior_feedback = feedbacks[-1].get("content", "")

    review = await review_code(
        task_title=task_row["title"],
        task_description=task_row["description"],
        output_text=result["output"],
        diff_text=diff_text,
        task_type=task_row["task_type"],
        iteration=review_iterations,
        prior_feedback=prior_feedback,
    )

    verdict = review["verdict"]
    summary = review.get("summary", "")

    await progress.push_event(
        project_id, "task_output",
        f"Code review: {verdict} — {summary}",
        task_id=task_id,
    )

    if verdict == "approved":
        # Self-interrogation: advisory second opinion on APPROVED verdict.
        # Logs concerns but never blocks.
        if INTERROGATION_ENABLED:
            await _interrogate_review_verdict(
                task_row=task_row,
                review_result=review,
                diff_text=diff_text,
                project_id=project_id,
                task_id=task_id,
                db=db,
                progress=progress,
            )

        # Auto-commit if enabled and there are changes
        if REVIEW_AUTO_COMMIT and diff_text:
            try:
                git = GitService(db=db)
                sha = await git.stage_and_commit(
                    cwd,
                    f"feat({task_row['title'][:40]}): task {task_id[:8]} — reviewed and approved",
                )
                if sha:
                    await progress.push_event(
                        project_id, "task_output",
                        f"Committed: {sha[:8]}",
                        task_id=task_id,
                    )
                    logger.info("Auto-committed task %s: %s", task_id, sha[:8])
            except Exception as e:
                logger.warning("Auto-commit failed for task %s: %s", task_id, e)
        return False  # Proceed to completion

    # Changes requested — check iteration limit
    if review_iterations >= REVIEW_MAX_ITERATIONS:
        logger.info(
            "Review iteration limit (%d) reached for task %s, sending to human review",
            REVIEW_MAX_ITERATIONS, task_id,
        )
        await db.execute_write(
            "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
            (
                TaskStatus.NEEDS_REVIEW,
                f"Code review found issues after {review_iterations} iterations: {summary}",
                time.time(), task_id,
            ),
        )
        if _row_get(task_row, "plan_id"):
            asyncio.ensure_future(_sync_status_to_context_store(
                db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
                status=TaskStatus.NEEDS_REVIEW,
                error=f"Review iteration limit: {summary}",
            ))
        await progress.push_event(
            project_id, "task_needs_review",
            f"{task_row['title']}: review iteration limit reached — {summary}",
            task_id=task_id,
        )
        return True

    # Append review feedback to context and re-queue for iteration
    feedback_text = format_review_feedback(review)
    non_feedbacks = [c for c in ctx if c.get("type") != "review_feedback"]
    feedbacks = [c for c in ctx if c.get("type") == "review_feedback"]
    feedbacks.append({
        "type": "review_feedback",
        "content": feedback_text,
        "review": {
            "verdict": review["verdict"],
            "issues": review.get("issues", []),
            "summary": review.get("summary", ""),
        },
    })

    updated_ctx = non_feedbacks + feedbacks

    await db.execute_write(
        "UPDATE tasks SET status = $1, context_json = $2, error = NULL, updated_at = $3 WHERE id = $4",
        (TaskStatus.PENDING, json.dumps(updated_ctx), time.time(), task_id),
    )

    await progress.push_event(
        project_id, "task_output",
        f"Review requested changes (iteration {review_iterations + 1}/{REVIEW_MAX_ITERATIONS}): {summary}",
        task_id=task_id,
    )

    logger.info(
        "Task %s sent back for iteration %d: %s",
        task_id, review_iterations + 1, summary,
    )
    return True


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
            "WHERE task_id = $1 AND event_type IN ('task_retry', 'task_failed') "
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
            error_context=f"Task type: {_row_get(task_row,'task_type', 'unknown')}, "
                          f"model: {_row_get(task_row,'model_tier', 'unknown')}",
            tags=["auto-captured", "retry-success"],
        )
    except Exception as e:
        logger.debug("Failed to ingest retry success: %s", e)


async def create_checkpoint(
    *, project_id, task_id, task_row, error_msg, db, progress,
    schema_json=None, checkpoint_type="retry_exhausted",
):
    """Create a checkpoint for a task that needs human review.

    Sets the task to NEEDS_REVIEW and creates a structured checkpoint record
    with attempt history for the user to resolve.
    """
    checkpoint_id = uuid.uuid4().hex[:12]

    # Gather attempt history from task_events
    events = await db.fetchall(
        "SELECT message, timestamp FROM task_events "
        "WHERE task_id = $1 AND event_type IN ('task_retry', 'task_failed') "
        "ORDER BY timestamp",
        (task_id,),
    )
    attempts = [
        {"message": e["message"], "timestamp": e["timestamp"]}
        for e in events
    ]

    await db.execute_write(
        "INSERT INTO checkpoints "
        "(id, project_id, task_id, checkpoint_type, summary, attempts_json, question, schema_json, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
        (
            checkpoint_id, project_id, task_id, checkpoint_type,
            f"Task '{task_row['title']}' failed after {task_row['max_retries']} attempts",
            json.dumps(attempts),
            "How should we proceed? Options: retry with modified approach, "
            "skip this task, or fail it.",
            schema_json,
            time.time(),
        ),
    )

    await db.execute_write(
        "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
        (TaskStatus.NEEDS_REVIEW, error_msg, time.time(), task_id),
    )
    if _row_get(task_row,"plan_id"):
        asyncio.ensure_future(_sync_status_to_context_store(
            db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
            status=TaskStatus.NEEDS_REVIEW, error=error_msg,
        ))

    await progress.push_event(
        project_id, "checkpoint",
        f"Checkpoint: {task_row['title']} needs attention after {task_row['max_retries']} failed attempts",
        task_id=task_id, checkpoint_id=checkpoint_id,
    )


async def create_reassessment_intervention(
    *,
    project_id: str,
    plan_id: str,
    wave: int,
    reassessment_context: dict,
    rationale: str,
    db,
    progress,
):
    """Create a supervised intervention proposal from a wave reassessment.

    This is called when the Athena Loop determines that the plan requires
    human intervention rather than autonomous replanning. It creates a
    checkpoint record and emits a frontend event.
    """
    intervention_id = uuid.uuid4().hex[:12]
    summary = f"Wave {wave} reassessment: escalation recommended"
    question = "The project plan may no longer be viable. Review the context and decide whether to replan, modify, or abort the project."

    await db.execute_write(
        "INSERT INTO checkpoints "
        "(id, project_id, checkpoint_type, summary, attempts_json, question, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        (
            intervention_id,
            project_id,
            "reassessment_escalation",
            summary,
            json.dumps(reassessment_context),
            question,
            time.time(),
        ),
    )

    # Also log to sentinel observations for unified history
    try:
        from backend.services.sentinel.models import SentinelObservation, Severity
        from backend.services.sentinel.context_client import SentinelContextClient

        observation = SentinelObservation(
            category="wave_reassessment",
            message=f"Escalation proposed for plan {plan_id} after wave {wave}",
            severity=Severity.CRITICAL,
            project_id=project_id,
            details={
                "wave": wave,
                "rationale": rationale,
                "context": reassessment_context,
                "intervention_id": intervention_id,
            },
        )
        ctx_client = SentinelContextClient()
        await ctx_client.save_observation(observation, parent_id=plan_id)
    except Exception:
        logger.debug("Failed to persist reassessment observation for plan %s", plan_id, exc_info=True)


    await progress.push_event(
        project_id,
        "intervention_proposal",
        summary,
        plan_id=plan_id,
        intervention_id=intervention_id,
        rationale=rationale,
    )
    logger.info("Created reassessment intervention %s for project %s wave %d", intervention_id, project_id, wave)


async def verify_task_output(
    *, task_row, output_text, project_id, task_id, db, budget, progress,
    retry_after: dict | None = None,
) -> bool:
    """Run output verification. Returns True if the task status was overridden."""
    from backend.services.verifier import verify_output
    from backend.models.enums import VerificationResult

    try:
        tools = json.loads(task_row["tools_json"]) if task_row["tools_json"] else []

        # Extract platform verification context from task context if present
        platform_ctx = None
        ctx_entries = json.loads(task_row["context_json"]) if task_row["context_json"] else []
        for entry in ctx_entries:
            if entry.get("type") == "platform_knowledge":
                platform_ctx = entry.get("content")
                break

        verification = await verify_output(
            task_title=task_row["title"],
            task_description=task_row["description"],
            output_text=output_text,
            task_type=task_row["task_type"],
            tools=tools,
            platform_context=platform_ctx,
            budget=budget,
            project_id=project_id,
            task_id=task_id,
        )
    except Exception as e:
        # Verification infrastructure failure — the task output is unverified.
        # Mark as NEEDS_REVIEW so dependents don't proceed on unverified output.
        # This prevents cascading hollow completions when the verifier is misconfigured.
        logger.warning("Verification failed for task %s: %s", task_id, e)
        await db.execute_write(
            "UPDATE tasks SET status = $1, verification_status = $2, verification_notes = $3, "
            "updated_at = $4 WHERE id = $5",
            (TaskStatus.NEEDS_REVIEW, VerificationResult.SKIPPED,
             f"Verification error: {e}", time.time(), task_id),
        )
        if _row_get(task_row,"plan_id"):
            asyncio.ensure_future(_sync_status_to_context_store(
                db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
                status=TaskStatus.NEEDS_REVIEW, error=f"Verification error: {e}",
            ))
        await progress.push_event(
            project_id, "task_needs_review",
            f"{task_row['title']}: verification infrastructure failed, blocking dependents",
            task_id=task_id,
        )
        return True

    v_result = verification["result"]
    v_notes = verification["notes"]

    await db.execute_write(
        "UPDATE tasks SET verification_status = $1, verification_notes = $2, "
        "updated_at = $3 WHERE id = $4",
        (v_result, v_notes, time.time(), task_id),
    )

    if v_result == VerificationResult.GAPS_FOUND:
        retry_count = task_row["retry_count"]
        max_retries = task_row["max_retries"]

        # Empty output with gaps is always a hard failure — never treat as a
        # soft pass.  Force retry if budget remains, otherwise mark FAILED so
        # the task blocks dependents and project completion.
        _output_empty = not output_text or not output_text.strip()
        if _output_empty:
            if retry_count < max_retries:
                if retry_after is not None:
                    retry_after.pop(task_id, None)
                ctx = json.loads(task_row["context_json"]) if task_row["context_json"] else []
                non_feedbacks = [e for e in ctx if e.get("type") != "verification_feedback"]
                feedbacks = [e for e in ctx if e.get("type") == "verification_feedback"]
                if len(feedbacks) >= _MAX_VERIFICATION_FEEDBACKS:
                    feedbacks = feedbacks[-(_MAX_VERIFICATION_FEEDBACKS - 1):]
                feedbacks.append({
                    "type": "verification_feedback",
                    "content": f"Previous attempt produced empty output with gaps: {v_notes}. "
                               "You MUST produce substantive output.",
                })
                ctx = non_feedbacks + feedbacks
                await db.execute_write(
                    "UPDATE tasks SET status = $1, context_json = $2, output_text = NULL, "
                    "retry_count = retry_count + 1, completed_at = NULL, updated_at = $3 WHERE id = $4",
                    (TaskStatus.PENDING, json.dumps(ctx), time.time(), task_id),
                )
                await progress.push_event(
                    project_id, "task_verification_retry",
                    f"{task_row['title']}: empty output with gaps, retrying",
                    task_id=task_id, verification_notes=v_notes,
                )
                return True
            else:
                logger.warning(
                    "Task %s has empty output with gaps and retries exhausted — marking FAILED",
                    task_id,
                )
                await db.execute_write(
                    "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                    (TaskStatus.FAILED,
                     f"Empty output after {retry_count} retries (gaps: {v_notes})",
                     time.time(), task_id),
                )
                if _row_get(task_row,"plan_id"):
                    asyncio.ensure_future(_sync_status_to_context_store(
                        db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
                        status=TaskStatus.FAILED,
                        error=f"Empty output after {retry_count} retries",
                    ))
                await progress.push_event(
                    project_id, "task_failed",
                    f"{task_row['title']}: empty output with gaps, retries exhausted",
                    task_id=task_id,
                )
                # File to fix queue for tracking
                try:
                    from backend.services.fix_queue import FixQueue
                    fq = FixQueue(db)
                    await fq.file_from_lifecycle(
                        task_id=task_id, project_id=project_id,
                        title=f"Empty output: {task_row['title'][:80]}",
                        description=f"Task produced empty output after {retry_count} retries. Gaps: {v_notes}",
                        error=f"Empty output after {retry_count} retries",
                        affected_component=_row_get(task_row,"model_tier") or _row_get(task_row,"model_used"),
                    )
                except Exception:
                    pass
                return True

        # Loop-breaker: if this output is identical to a previously rejected
        # output, retrying won't help — escalate to human review.
        import hashlib
        output_hash = hashlib.md5((output_text or "").encode()).hexdigest()[:16]
        ctx = json.loads(task_row["context_json"]) if task_row["context_json"] else []
        prev_feedbacks = [e for e in ctx if e.get("type") == "verification_feedback"]
        if prev_feedbacks and retry_count > 0:
            prev_hash = None
            for fb in reversed(prev_feedbacks):
                if "output_hash" in fb:
                    prev_hash = fb["output_hash"]
                    break
            if prev_hash and prev_hash == output_hash:
                logger.warning(
                    "Task %s produced identical output after retry, escalating to human review",
                    task_id,
                )
                await db.execute_write(
                    "UPDATE tasks SET status = $1, verification_notes = $2, updated_at = $3 WHERE id = $4",
                    (TaskStatus.NEEDS_REVIEW,
                     f"Identical output after retry — likely false positive: {v_notes}",
                     time.time(), task_id),
                )
                if _row_get(task_row,"plan_id"):
                    asyncio.ensure_future(_sync_status_to_context_store(
                        db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
                        status=TaskStatus.NEEDS_REVIEW,
                        error=f"Identical output after retry: {v_notes}",
                    ))
                await progress.push_event(
                    project_id, "task_needs_review",
                    f"{task_row['title']}: identical output on retry, likely false positive",
                    task_id=task_id,
                )
                return True

        if retry_count < max_retries:
            # Clear any lingering retry backoff so the task re-dispatches
            # immediately. Without this, a stale retry_after entry from a
            # prior transient error can block dispatch indefinitely.
            if retry_after is not None:
                retry_after.pop(task_id, None)

            # Auto-retry with verification feedback appended to context.
            # Sliding window: keep the most recent feedbacks up to the cap.
            non_feedbacks = [e for e in ctx if e.get("type") != "verification_feedback"]
            feedbacks = [e for e in ctx if e.get("type") == "verification_feedback"]
            if len(feedbacks) >= _MAX_VERIFICATION_FEEDBACKS:
                feedbacks = feedbacks[-(_MAX_VERIFICATION_FEEDBACKS - 1):]
            # Enrich retry feedback with historical recovery intelligence
            recovery_hint = ""
            try:
                from backend.services.learning.execution_learner import get_learner
                learner = get_learner()
                recommendation = learner.get_recovery_recommendation(v_notes or "")
                if recommendation == "diagnose_first":
                    recovery_hint = (
                        " RECOVERY STRATEGY: Read the relevant files first to understand "
                        "the current state before making changes (91% historical success rate)."
                    )
                elif recommendation == "reassign_model":
                    recovery_hint = (
                        " NOTE: This failure pattern has historically been unrecoverable "
                        "with retries. Focus on producing any working output."
                    )
            except Exception:
                pass

            feedbacks.append({
                "type": "verification_feedback",
                "content": f"Previous attempt had gaps: {v_notes}. Address these issues.{recovery_hint}",
                "output_hash": output_hash,
            })
            ctx = non_feedbacks + feedbacks
            await db.execute_write(
                "UPDATE tasks SET status = $1, context_json = $2, "
                "retry_count = retry_count + 1, completed_at = NULL, updated_at = $3 WHERE id = $4",
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
            "UPDATE tasks SET status = $1, updated_at = $2 WHERE id = $3",
            (TaskStatus.NEEDS_REVIEW, time.time(), task_id),
        )
        if _row_get(task_row,"plan_id"):
            asyncio.ensure_future(_sync_status_to_context_store(
                db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
                status=TaskStatus.NEEDS_REVIEW, error="Verification: human review needed",
            ))
        await progress.push_event(
            project_id, "task_needs_review",
            f"{task_row['title']}: requires human review",
            task_id=task_id, verification_notes=v_notes,
        )
        return True

    # Self-interrogation: when verification says PASSED, run the 6 questions
    # as an advisory second opinion. Logs concerns but never blocks.
    if v_result == VerificationResult.PASSED and INTERROGATION_ENABLED:
        await _interrogate_verification_verdict(
            task_row=task_row,
            output_text=output_text,
            v_notes=v_notes,
            project_id=project_id,
            task_id=task_id,
            db=db,
            progress=progress,
        )

    return False


async def _gather_interrogation_context(*, db, project_id: str, task_id: str) -> dict:
    """Gather rich context for self-interrogation from the DB.

    Returns a dict with keys: project_summary, requirements, decision_history,
    sibling_outcomes, affected_files, dependency_info, knowledge_findings.
    All are strings, truncated to reasonable lengths. Best-effort — returns
    empty strings on failure so interrogation can still run.
    """
    ctx: dict[str, str] = {}

    try:
        # Project summary + requirements
        proj = await db.fetchone(
            "SELECT title, requirements, status FROM projects WHERE id = $1",
            (project_id,),
        )
        if proj:
            ctx["project_summary"] = (
                f"Project: {proj['title']} (status: {proj['status']})"
            )
            ctx["requirements"] = (proj["requirements"] or "")[:1500]
    except Exception:
        pass

    try:
        # Sibling tasks in the same wave — what else is running/completed/failed
        task_wave = await db.fetchone(
            "SELECT wave FROM tasks WHERE id = $1", (task_id,),
        )
        if task_wave and task_wave["wave"] is not None:
            siblings = await db.fetchall(
                "SELECT title, status, task_type, error FROM tasks "
                "WHERE project_id = $1 AND wave = $2 AND id != $3 "
                "ORDER BY status",
                (project_id, task_wave["wave"], task_id),
            )
            if siblings:
                lines = []
                for s in siblings[:10]:
                    line = f"- {s['title']} [{s['status']}]"
                    if s["error"]:
                        line += f" error: {s['error'][:100]}"
                    lines.append(line)
                ctx["sibling_outcomes"] = (
                    f"Wave {task_wave['wave']} siblings:\n" + "\n".join(lines)
                )
    except Exception:
        pass

    try:
        # Downstream dependents — what breaks if this task is wrong
        deps = await db.fetchall(
            "SELECT t.title, t.status FROM task_deps td "
            "JOIN tasks t ON t.id = td.task_id "
            "WHERE td.depends_on = $1",
            (task_id,),
        )
        if deps:
            dep_lines = [f"- {d['title']} [{d['status']}]" for d in deps[:8]]
            ctx["dependency_info"] = (
                f"{len(deps)} downstream task(s) depend on this:\n"
                + "\n".join(dep_lines)
            )
    except Exception:
        pass

    try:
        # Affected files from task context
        task_ctx = await db.fetchone(
            "SELECT context_json FROM tasks WHERE id = $1", (task_id,),
        )
        if task_ctx and task_ctx["context_json"]:
            entries = json.loads(task_ctx["context_json"])
            for entry in entries:
                if entry.get("type") == "affected_files":
                    ctx["affected_files"] = f"Affected files: {entry.get('content', '')}"
                    break
    except Exception:
        pass

    try:
        # Recent sentinel decisions for this project — real precedent
        decisions = await db.fetchall(
            "SELECT message, details_json FROM sentinel_observations "
            "WHERE project_id = $1 AND category IN "
            "('intervention_result', 'intervention_proposal', 'interrogation_concern', 'wave_reassessment') "
            "ORDER BY created_at DESC LIMIT 5",
            (project_id,),
        )
        if decisions:
            lines = [d["message"][:150] for d in decisions]
            ctx["decision_history"] = (
                "Recent sentinel decisions:\n" + "\n".join(f"- {l}" for l in lines)
            )
    except Exception:
        pass

    try:
        # Project knowledge findings — what the system has learned
        findings = await db.fetchall(
            "SELECT content, category, rationale, alternatives_considered, "
            "confidence, source_task_title FROM project_knowledge "
            "WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5",
            (project_id,),
        )
        if findings:
            lines = []
            for f in findings:
                cat = f.get("category", "discovery")
                conf = f.get("confidence", "medium")
                line = f"[{cat}|{conf}] {f['content'][:200]}"
                if f.get("rationale"):
                    line += f"\n    WHY: {f['rationale'][:150]}"
                if f.get("alternatives_considered"):
                    line += f"\n    REJECTED: {f['alternatives_considered'][:100]}"
                if f.get("source_task_title"):
                    line += f"\n    (from: {f['source_task_title']})"
                lines.append(line)
            ctx["knowledge_findings"] = (
                "Historical Rationale — lessons from prior tasks:\n"
                + "\n".join(f"- {l}" for l in lines)
            )
    except Exception:
        pass

    return ctx


async def _interrogate_verification_verdict(
    *, task_row, output_text, v_notes, project_id, task_id, db, progress,
) -> None:
    """Run self-interrogation on a PASSED verification verdict.

    Advisory only — logs concerns as a sentinel observation and appends
    to verification_notes, but does NOT block the task. The verifier
    already said PASSED; the interrogation is a second opinion that
    gets recorded for audit, not a gate that creates stuck tasks.
    """
    from backend.services.sentinel.interrogator import (
        DecisionContext,
        InterrogationInput,
        SelfInterrogator,
    )

    try:
        is_coding = task_row["task_type"] in ("code", "game_code", "game_build_verify")
        ctx = await _gather_interrogation_context(
            db=db, project_id=project_id, task_id=task_id,
        )
        interrogator = SelfInterrogator(enabled=True, model=INTERROGATION_MODEL)
        inp = InterrogationInput(
            decision_context=DecisionContext.VERIFICATION_VERDICT,
            proposed_action="Accept verification PASSED verdict and mark task complete",
            reasoning=f"Verifier notes: {v_notes}",
            is_coding_task=is_coding,
            task_description=task_row["description"][:2000],
            task_output=(output_text or "")[:2000],
            project_summary=ctx.get("project_summary", ""),
            error_text=ctx.get("affected_files", ""),
            decision_history=ctx.get("decision_history", ""),
            world_state="\n".join(filter(None, [
                ctx.get("sibling_outcomes", ""),
                ctx.get("dependency_info", ""),
                ctx.get("knowledge_findings", ""),
                ctx.get("requirements", ""),
            ])),
        )
        result = await interrogator.interrogate(inp)

        if result is not None and not result.proceed:
            concern = (
                f"Self-interrogation concern (advisory): {result.escalation_trigger} "
                f"(confidence={result.overall_confidence:.2f})"
            )
            logger.info(
                "Self-interrogation flagged verification for task %s (trigger=%s) — "
                "advisory only, proceeding",
                task_id, result.escalation_trigger,
            )
            # Append concern to verification_notes for audit trail
            updated_notes = f"{v_notes} | {concern}" if v_notes else concern
            await db.execute_write(
                "UPDATE tasks SET verification_notes = $1, updated_at = $2 WHERE id = $3",
                (updated_notes, time.time(), task_id),
            )
            # Log as sentinel observation so it's visible in the dashboard
            await _log_interrogation_observation(
                db=db, project_id=project_id, task_id=task_id,
                context="verification_verdict", result=result,
            )
            await progress.push_event(
                project_id, "task_output",
                f"{task_row['title']}: interrogation concern (advisory): "
                f"{result.escalation_trigger}",
                task_id=task_id,
            )
    except Exception:
        logger.debug("Self-interrogation failed for verification of task %s, proceeding", task_id)


async def _interrogate_review_verdict(
    *, task_row, review_result, diff_text, project_id, task_id, db, progress,
) -> None:
    """Run self-interrogation on a code review APPROVED verdict.

    Advisory only — logs concerns but does NOT block the task. The code
    reviewer already approved; the interrogation records concerns for
    audit without creating stuck tasks.
    """
    from backend.services.sentinel.interrogator import (
        DecisionContext,
        InterrogationInput,
        SelfInterrogator,
    )

    try:
        ctx = await _gather_interrogation_context(
            db=db, project_id=project_id, task_id=task_id,
        )
        # Include review issues in reasoning so the LLM knows what was checked
        issues_text = ""
        for issue in review_result.get("issues", []):
            sev = issue.get("severity", "info")
            desc = issue.get("description", "")
            issues_text += f"\n- [{sev}] {issue.get('file', '')}: {desc}"

        interrogator = SelfInterrogator(enabled=True, model=INTERROGATION_MODEL)
        inp = InterrogationInput(
            decision_context=DecisionContext.CODE_REVIEW_VERDICT,
            proposed_action="Accept code review APPROVED verdict, auto-commit, and mark complete",
            reasoning=(
                f"Review summary: {review_result.get('summary', '')}"
                + (f"\nReview issues (all warnings, no blockers):{issues_text}" if issues_text else "")
            ),
            is_coding_task=True,
            task_description=task_row["description"][:2000],
            task_output=(diff_text or "")[:3000],
            project_summary=ctx.get("project_summary", ""),
            error_text=ctx.get("affected_files", ""),
            decision_history=ctx.get("decision_history", ""),
            world_state="\n".join(filter(None, [
                ctx.get("sibling_outcomes", ""),
                ctx.get("dependency_info", ""),
                ctx.get("knowledge_findings", ""),
                ctx.get("requirements", ""),
            ])),
        )
        result = await interrogator.interrogate(inp)

        if result is not None and not result.proceed:
            logger.info(
                "Self-interrogation flagged review for task %s (trigger=%s) — "
                "advisory only, proceeding",
                task_id, result.escalation_trigger,
            )
            await _log_interrogation_observation(
                db=db, project_id=project_id, task_id=task_id,
                context="code_review_verdict", result=result,
            )
            await progress.push_event(
                project_id, "task_output",
                f"{task_row['title']}: review interrogation concern (advisory): "
                f"{result.escalation_trigger}",
                task_id=task_id,
            )
    except Exception:
        logger.debug("Self-interrogation failed for review of task %s, proceeding", task_id)


async def _log_interrogation_observation(
    *, db, project_id: str, task_id: str, context: str, result,
) -> None:
    """Persist an interrogation concern as a sentinel observation for dashboard visibility."""
    try:
        answers_summary = "; ".join(
            f"{a.question}: {'OK' if a.confident else 'GAP — ' + a.answer}"
            for a in result.answers
            if not a.confident
        )
        await db.execute_write(
            "INSERT INTO sentinel_observations "
            "(id, project_id, task_id, category, severity, message, details_json, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING",
            (
                str(uuid.uuid4()),
                project_id,
                task_id,
                "interrogation_concern",
                "info",
                f"Self-interrogation concern during {context}: {result.escalation_trigger}",
                json.dumps({
                    "context": context,
                    "escalation_trigger": result.escalation_trigger,
                    "overall_confidence": result.overall_confidence,
                    "knowledge_gaps": answers_summary,
                }),
                time.time(),
            ),
        )
    except Exception:
        logger.debug("Failed to persist interrogation observation for task %s", task_id)


async def forward_context(*, completed_task, output_text, db):
    """Inject completed task's output summary into dependent tasks' context."""
    deps = await db.fetchall(
        "SELECT task_id FROM task_deps WHERE depends_on = $1",
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
                "SELECT context_json FROM tasks WHERE id = $1", (dep["task_id"],),
            )
            if dep_task:
                ctx = json.loads(dep_task["context_json"]) if dep_task["context_json"] else []
                ctx.append(context_entry)
                await db.execute_write(
                    "UPDATE tasks SET context_json = $1, updated_at = $2 WHERE id = $3",
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
                "UPDATE tasks SET status = $1, started_at = $2, updated_at = $3 WHERE id = $4",
                (TaskStatus.RUNNING, now, now, task_id),
            )
            asyncio.ensure_future(_sync_status_to_context_store(
                db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
                status=TaskStatus.RUNNING,
            ))
            await progress.push_event(
                project_id, "task_start", task_row["title"], task_id=task_id
            )
            await progress.push_event(
                project_id, "phase", "Phase: dispatching",
                task_id=task_id, phase="dispatching",
            )

            try:
                # --- Ensure working directory exists with CLI config ---
                from backend.services.cli_common import resolve_cwd
                _task_cwd = await resolve_cwd(db, project_id)
                if _task_cwd:
                    await _ensure_workspace(_task_cwd, db, project_id)

                # --- Context enrichment (pre-dispatch) ---
                # Query the context store for relevant knowledge and append the
                # XML block to the task description before handing off to the
                # agent.  Enrichment is best-effort: any failure (disabled,
                # unreachable store, empty result) leaves the description unchanged.
                _dispatch_row = task_row
                enrichment_tokens = 0
                enrichment_nodes = 0
                enrichment_latency_ms = 0.0

                # --- Project Knowledge Injection ---
                try:
                    knowledge_rows = await db.fetchall(
                        "SELECT category, content AS finding, rationale, alternatives_considered, "
                        "confidence, source_task_title "
                        "FROM project_knowledge WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5",
                        (project_id,)
                    )
                    if knowledge_rows:
                        knowledge_items = [dict(r) for r in knowledge_rows]
                        
                        current_context = json.loads(_row_get(_dispatch_row,"context_json") or "[]")
                        # Avoid duplicating on retries
                        if not any(c.get("type") == "project_knowledge" for c in current_context):
                            current_context.insert(0, {
                                "type": "project_knowledge",
                                "content": knowledge_items
                            })
                            # Need to create a new row object to modify it
                            _dispatch_row = dict(_dispatch_row)
                            _dispatch_row["context_json"] = json.dumps(current_context)
                except Exception as e:
                    logger.debug("Project knowledge injection failed for %s: %s", task_id, e)

                if CONTEXT_ENRICHMENT_ENABLED:
                    from backend.services.enrichment_service import EnrichmentService
                    _enrichment = EnrichmentService()
                    _enrichment_result = await _enrichment.enrich(task_row["description"])
                    if _enrichment_result:
                        _dispatch_row = dict(_dispatch_row)
                        _dispatch_row["description"] = (
                            task_row["description"]
                            + "\n\n"
                            + _enrichment_result.xml_block
                        )
                        enrichment_tokens = _enrichment_result.tokens_used
                        enrichment_nodes = _enrichment_result.node_count
                        enrichment_latency_ms = _enrichment_result.latency_ms

                        logger.info(
                            "task %s enriched: tokens=%d nodes=%d latency=%.1fms",
                            task_id,
                            enrichment_tokens,
                            enrichment_nodes,
                            enrichment_latency_ms,
                        )

                # Persist enrichment metadata only when enrichment actually ran
                # and returned results — avoids polluting usage_log with zero rows.
                if enrichment_nodes > 0:
                    await db.execute_write(
                        "INSERT INTO usage_log "
                        "(project_id, task_id, provider, model, prompt_tokens, "
                        "completion_tokens, cost_usd, purpose, timestamp, "
                        "context_tokens_injected, source_node_count, enrichment_latency_ms) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                        (
                            task_row["project_id"], task_id,
                            "context_store", "preview",
                            0, 0, 0.0, "context_enrichment",
                            time.time(),
                            enrichment_tokens,
                            enrichment_nodes,
                            enrichment_latency_ms,
                        ),
                    )

                await progress.push_event(
                    project_id, "phase", f"Phase: executing ({tier.value})",
                    task_id=task_id, phase="executing", model_tier=tier.value,
                )

                # Step tree execution — if task has a step tree, use
                # the tree runner instead of single-shot dispatch.
                _step_tree_raw = _row_get(_dispatch_row,"step_tree_json")
                if _step_tree_raw:
                    from backend.services.tree_runner import run_step_tree
                    _step_tree_data = (
                        json.loads(_step_tree_raw)
                        if isinstance(_step_tree_raw, str)
                        else _step_tree_raw
                    )
                    result = await run_step_tree(
                        step_tree=_step_tree_data,
                        task_row=_dispatch_row,
                        tier=tier,
                        db=db, budget=budget, progress=progress,
                        http_client=http_client,
                        tool_registry=tool_registry,
                    )
                elif tier == ModelTier.OLLAMA:
                    result = await run_ollama_task(
                        task_row=_dispatch_row, http_client=http_client, budget=budget,
                        tool_registry=tool_registry,
                    )
                elif tier == ModelTier.CLAUDE_CODE:
                    result = await run_claude_code_task(
                        task_row=_dispatch_row, db=db, budget=budget,
                        progress=progress,
                    )
                elif tier == ModelTier.GEMINI_CLI:
                    result = await run_gemini_cli_task(
                        task_row=_dispatch_row, db=db, budget=budget,
                        progress=progress,
                    )
                elif tier == ModelTier.CODEX_CLI:
                    result = await run_codex_cli_task(
                        task_row=_dispatch_row, db=db, budget=budget,
                        progress=progress,
                    )
                elif not client:
                    # No Anthropic API client — fall back to Claude Code CLI
                    # for haiku/sonnet tasks. CLI uses its own subscription auth.
                    # Pass the model tier name so CLI uses the correct model.
                    from backend.services.model_router import get_model_id
                    model_id = get_model_id(tier)
                    logger.info(
                        "No Anthropic client, routing %s task %s through Claude Code CLI (model=%s)",
                        tier.value, task_id, model_id,
                    )
                    result = await run_claude_code_task(
                        task_row=_dispatch_row, db=db, budget=budget,
                        progress=progress, model=tier.value,
                    )
                else:
                    result = await run_claude_task(
                        task_row=_dispatch_row, est_cost=est_cost, client=client,
                        tool_registry=tool_registry, budget=budget, progress=progress,
                        db=db,
                    )

                # Handle budget exhaustion — mark for review, don't complete
                retry_after.pop(task_id, None)
                if result.get("budget_exhausted"):
                    await db.execute_write(
                        "UPDATE tasks SET status = $1, output_text = $2, error = $3, "
                        "prompt_tokens = $4, completion_tokens = $5, cost_usd = $6, "
                        "model_used = $7, updated_at = $8 WHERE id = $9",
                        (
                            TaskStatus.NEEDS_REVIEW, result["output"],
                            "Budget exhausted mid-execution (partial output)",
                            result["prompt_tokens"], result["completion_tokens"],
                            result["cost_usd"], result["model_used"],
                            time.time(), task_id,
                        ),
                    )
                    asyncio.ensure_future(_sync_status_to_context_store(
                        db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
                        status=TaskStatus.NEEDS_REVIEW, error="Budget exhausted",
                    ))
                    await progress.push_event(
                        project_id, "task_needs_review",
                        f"{task_row['title']}: budget exhausted, partial output needs review",
                        task_id=task_id,
                    )

                    await _push_telemetry(
                        task_row=task_row, tier=tier, project_id=project_id,
                        task_id=task_id, status="failed",
                        verification_outcome="budget_exhausted",
                        result=result, http_client=http_client,
                    )
                    return

                # Mark completed
                await db.execute_write(
                    "UPDATE tasks SET status = $1, output_text = $2, "
                    "prompt_tokens = $3, completion_tokens = $4, cost_usd = $5, "
                    "model_used = $6, completed_at = $7, updated_at = $8 WHERE id = $9",
                    (
                        TaskStatus.COMPLETED, result["output"],
                        result["prompt_tokens"], result["completion_tokens"],
                        result["cost_usd"], result["model_used"],
                        time.time(), time.time(), task_id,
                    ),
                )
                asyncio.ensure_future(_sync_status_to_context_store(
                    db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
                    status=TaskStatus.COMPLETED,
                ))

                # Optional output verification (skip for Ollama — free tasks).
                # Verifier uses call_llm (CLI/Ollama), not the Anthropic SDK client.
                # Run BEFORE forwarding context to prevent dependents from
                # receiving output that verification may reject.
                if VERIFICATION_ENABLED and tier != ModelTier.OLLAMA:
                    await progress.push_event(
                        project_id, "phase", "Phase: verifying",
                        task_id=task_id, phase="verifying",
                    )
                    verification_overridden = await verify_task_output(
                        task_row=task_row, output_text=result["output"],
                        project_id=project_id, task_id=task_id,
                        db=db, budget=budget, progress=progress,
                        retry_after=retry_after,
                    )
                    if verification_overridden:
                        return  # Task was reset to PENDING or NEEDS_REVIEW

                # --- File tracking: stage declared files, detect orphans ---
                # Runs after output capture + verification but before review.
                # Never blocks task completion.
                try:
                    from backend.services.cli_common import resolve_cwd
                    from backend.services.git_service import GitService

                    _ft_cwd = await resolve_cwd(db, project_id)
                    if _ft_cwd:
                        _ft_git = GitService(db=db)

                        # Extract affected_files from context_json
                        _ft_ctx = json.loads(task_row["context_json"] or "[]")
                        _ft_affected: list[str] = []
                        for _ft_entry in _ft_ctx:
                            if _ft_entry.get("type") == "affected_files":
                                _ft_affected = _ft_entry.get("content", "").split(", ")
                                break
                        if not _ft_affected:
                            _ft_af_json = _row_get(task_row, "affected_files") or "[]"
                            if isinstance(_ft_af_json, str):
                                try:
                                    _ft_affected = json.loads(_ft_af_json)
                                except json.JSONDecodeError:
                                    _ft_affected = []

                        # Stage declared affected_files
                        _ft_affected = [f.strip() for f in _ft_affected if f.strip()]
                        if _ft_affected:
                            for _ft_file in _ft_affected:
                                try:
                                    await asyncio.to_thread(
                                        _ft_git._run_git_ok_sync,
                                        "add", "--", _ft_file, cwd=_ft_cwd,
                                    )
                                except Exception:
                                    logger.debug("Failed to stage %s", _ft_file)
                            logger.info(
                                "Staged %d declared file(s) for task %s",
                                len(_ft_affected), task_id,
                            )

                        # Detect orphaned files (untracked/modified but not declared)
                        _ft_status_ok, _ft_status = await asyncio.to_thread(
                            _ft_git._run_git_ok_sync,
                            "status", "--porcelain", cwd=_ft_cwd,
                        )
                        if _ft_status.strip():
                            _ft_orphans = [
                                line[3:] for line in _ft_status.split("\n")
                                if line.strip() and line[3:].strip() not in _ft_affected
                            ]
                            if _ft_orphans:
                                logger.warning(
                                    "Task %s left %d orphaned file(s): %s",
                                    task_id, len(_ft_orphans),
                                    ", ".join(_ft_orphans[:10]),
                                )
                                try:
                                    from backend.services.sentinel.models import (
                                        SentinelObservation, Severity,
                                    )
                                    from backend.services.sentinel.context_client import (
                                        SentinelContextClient,
                                    )
                                    _ft_obs = SentinelObservation(
                                        category="orphaned_files",
                                        message=(
                                            f"Task {task_id} created {len(_ft_orphans)} "
                                            f"file(s) not declared in affected_files"
                                        ),
                                        severity=Severity.WARNING,
                                        project_id=project_id,
                                        task_id=task_id,
                                        details={
                                            "orphaned_files": _ft_orphans[:50],
                                            "declared_files": _ft_affected,
                                        },
                                    )
                                    _ft_ctx_client = SentinelContextClient()
                                    try:
                                        await _ft_ctx_client.save_observation(
                                            _ft_obs, parent_id=project_id,
                                        )
                                    finally:
                                        await _ft_ctx_client.close()
                                except Exception:
                                    logger.debug(
                                        "Failed to persist orphaned_files observation",
                                        exc_info=True,
                                    )
                except Exception:
                    logger.debug(
                        "File tracking failed for task %s (non-blocking)",
                        task_id, exc_info=True,
                    )

                # --- Python syntax check on affected files ---
                # Catch broken string literals and other syntax errors before commit
                try:
                    if _ft_cwd and _ft_affected:
                        for _sc_file in _ft_affected:
                            if _sc_file.endswith(".py"):
                                _sc_path = os.path.join(_ft_cwd, _sc_file)
                                if os.path.isfile(_sc_path):
                                    try:
                                        with open(_sc_path, "r", encoding="utf-8") as _sc_f:
                                            compile(_sc_f.read(), _sc_file, "exec")
                                    except SyntaxError as _sc_err:
                                        logger.error(
                                            "Syntax error in executor output %s line %s: %s",
                                            _sc_file, _sc_err.lineno, _sc_err.msg,
                                        )
                except Exception:
                    logger.debug("Python syntax check failed (non-blocking)", exc_info=True)

                # --- Code review cycle (post-verification) ---
                # Reviews the git diff like a senior dev, iterates if needed,
                # then commits approved changes.
                # Check both global config AND per-project config_json.
                _review_enabled = REVIEW_CYCLE_ENABLED
                if not _review_enabled:
                    _proj_row = await db.fetchone(
                        "SELECT config_json FROM projects WHERE id = $1", (project_id,)
                    )
                    _project_cfg = json.loads(
                        _proj_row["config_json"] if _proj_row and _proj_row["config_json"] else "{}"
                    )
                    _review_enabled = _project_cfg.get("review_cycle", {}).get("enabled", False)
                if _review_enabled and tier not in (ModelTier.OLLAMA,):
                    review_blocked = await _run_review_cycle(
                        task_row=task_row,
                        task_id=task_id,
                        project_id=project_id,
                        result=result,
                        tier=tier,
                        db=db,
                        budget=budget,
                        progress=progress,
                        client=client,
                        tool_registry=tool_registry,
                        http_client=http_client,
                        semaphore=semaphore,
                        dispatched=dispatched,
                        retry_after=retry_after,
                    )
                    if review_blocked:
                        return  # Task re-queued for iteration or sent to review

                # --- Migration file validation (post-completion) ---
                # If the task created or modified Alembic migration files,
                # validate naming and revision chain conventions.
                try:
                    migration_errors = await validate_migration_files(
                        db=db, project_id=project_id,
                        task_id=task_id, output_text=result["output"],
                    )
                    if migration_errors:
                        await progress.push_event(
                            project_id, "migration_validation_warning",
                            f"{task_row['title']}: {len(migration_errors)} migration issue(s)",
                            task_id=task_id,
                        )
                except Exception:
                    logger.debug(
                        "Migration validation failed for task %s",
                        task_id, exc_info=True,
                    )

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

                # Push execution telemetry (success)
                verification_outcome_str = None
                if VERIFICATION_ENABLED and tier != ModelTier.OLLAMA:
                    task_fresh = await db.fetchone(
                        "SELECT verification_status FROM tasks WHERE id = $1", (task_id,)
                    )
                    if task_fresh and _row_get(task_fresh, "verification_status"):
                        verification_outcome_str = task_fresh["verification_status"]

                await _push_telemetry(
                    task_row=task_row, tier=tier, project_id=project_id,
                    task_id=task_id, status="completed",
                    verification_outcome=verification_outcome_str,
                    result=result, http_client=http_client,
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
                            "SELECT context_json FROM tasks WHERE id = $1", (task_id,),
                        )
                        ctx = json.loads(task_ctx["context_json"]) if task_ctx and task_ctx["context_json"] else []
                        ctx.append({
                            "type": "diagnostic_suggestion",
                            "content": diagnostic_ctx,
                        })
                        await db.execute_write(
                            "UPDATE tasks SET status = $1, retry_count = retry_count + 1, "
                            "error = $2, context_json = $3, updated_at = $4 WHERE id = $5",
                            (TaskStatus.PENDING, f"Transient error (retry {retry_count + 1}): {e}",
                             json.dumps(ctx), time.time(), task_id),
                        )
                    else:
                        await db.execute_write(
                            "UPDATE tasks SET status = $1, retry_count = retry_count + 1, "
                            "error = $2, updated_at = $3 WHERE id = $4",
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
                            "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                            (TaskStatus.FAILED, error_msg, time.time(), task_id),
                        )
                        await progress.push_event(
                            project_id, "task_failed", f"{task_row['title']}: {error_msg}",
                            task_id=task_id,
                        )

                    asyncio.ensure_future(_sync_status_to_context_store(
                        db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
                        status=TaskStatus.FAILED, error=error_msg,
                    ))
                    await _push_telemetry(
                        task_row=task_row, tier=tier, project_id=project_id,
                        task_id=task_id, status="failed",
                        verification_outcome="max_retries_exceeded",
                        http_client=http_client,
                    )

            except asyncio.CancelledError:
                retry_after.pop(task_id, None)
                await db.execute_write(
                    "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                    (TaskStatus.PENDING, "Cancelled by shutdown", time.time(), task_id),
                )
                await progress.push_event(
                    project_id, "task_cancelled",
                    f"{task_row['title']}: cancelled, will retry on restart",
                    task_id=task_id,
                )
                raise  # Re-raise — swallowing breaks asyncio cancellation protocol

            except Exception as e:
                retry_after.pop(task_id, None)
                error_msg = str(e)
                await db.execute_write(
                    "UPDATE tasks SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                    (TaskStatus.FAILED, error_msg, time.time(), task_id),
                )
                asyncio.ensure_future(_sync_status_to_context_store(
                    db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
                    status=TaskStatus.FAILED, error=error_msg,
                ))
                await progress.push_event(
                    project_id, "task_failed", f"{task_row['title']}: {error_msg}",
                    task_id=task_id,
                )

                await _push_telemetry(
                    task_row=task_row, tier=tier, project_id=project_id,
                    task_id=task_id, status="failed",
                    verification_outcome="exception",
                    http_client=http_client,
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
        "UPDATE tasks SET status = $1, output_text = $2, model_used = $3, "
        "prompt_tokens = $4, completion_tokens = $5, cost_usd = $6, "
        "completed_at = $7, updated_at = $8 WHERE id = $9",
        (
            TaskStatus.COMPLETED, output_text, model_used,
            prompt_tokens, completion_tokens, cost_usd,
            now, now, task_id,
        ),
    )

    # Sync status to context store (fire-and-forget)
    if _row_get(task_row,"plan_id"):
        asyncio.ensure_future(_sync_status_to_context_store(
            db=db, task_title=task_row["title"], plan_id=task_row["plan_id"],
            status=TaskStatus.COMPLETED,
        ))

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
            if ANTHROPIC_API_KEY:
                import anthropic as anthropic_mod
                ext_client = anthropic_mod.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
            else:
                ext_client = None
            from backend.services.knowledge_extractor import extract_knowledge
            await extract_knowledge(
                task_title=task_row["title"],
                task_description=task_row["description"],
                output_text=output_text,
                client=ext_client,
                budget=budget,
                project_id=project_id,
                task_id=task_id,
                db=db,
            )
        except Exception as e:
            logger.warning("Knowledge extraction failed for external task %s: %s", task_id, e)

    # Push telemetry for external task completion
    started_at = _row_get(task_row,"started_at") or now
    duration = now - started_at
    provider = _get_provider_from_model_name(model_used)
    await push_execution_outcome(
        task_id=task_id,
        project_id=project_id,
        task_title=task_row["title"],
        task_description=task_row["description"],
        task_type=_row_get(task_row,"task_type", "unknown"),
        provider=provider,
        model=model_used,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        duration_seconds=duration,
        status="completed",
        verification_outcome=None,  # External tasks skip verification
    )

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


# ---------------------------------------------------------------------------
# Wave Reassessment (Athena Loop)
# ---------------------------------------------------------------------------

from backend.models.schemas import (
    KnowledgeFinding,
    SentinelObservationSummary,
    TaskOutcomeSummary,
    WaveReassessmentContext,
)

_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "needs_review")


async def collect_wave_reassessment_context(
    *, db, project_id: str, wave_number: int
) -> WaveReassessmentContext | None:
    """Collect task outcomes, knowledge, and observations when all wave tasks are terminal.

    Returns None if the plan is missing or any task in the wave is still non-terminal.
    Gathers:
      - Per-task outcomes with cost (from usage_log), model_tier, duration
      - Knowledge findings from project_knowledge
      - Sentinel observations for the project
      - Original plan JSON
      - Aggregate wave cost
    """

    # ---------------------------------------------------------------
    # 0. Verify all tasks in the wave are terminal
    # ---------------------------------------------------------------
    non_terminal_row = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM tasks "
        "WHERE project_id = $1 AND wave = $2 AND status NOT IN ($3, $4, $5, $6)",
        (project_id, wave_number, *_TERMINAL_STATUSES),
    )
    if non_terminal_row and non_terminal_row["cnt"] > 0:
        logger.debug(
            "Wave %d has %d non-terminal tasks, skipping reassessment collection",
            wave_number, non_terminal_row["cnt"],
        )
        return None

    # ---------------------------------------------------------------
    # 1. Task outcomes with cost data from usage_log
    #    NOTE: u.cost_usd is fully qualified to avoid ambiguity when
    #    JOINing usage_log (which has cost_usd) with other tables.
    # ---------------------------------------------------------------
    task_rows = await db.fetchall(
        "SELECT t.id, t.title, t.status, t.output_text, t.error, "
        "t.model_tier, t.started_at, t.completed_at, "
        "COALESCE(SUM(u.cost_usd), 0) as task_cost "
        "FROM tasks t "
        "LEFT JOIN usage_log u ON u.task_id = t.id "
        "WHERE t.project_id = $1 AND t.wave = $2 "
        "GROUP BY t.id",
        (project_id, wave_number),
    )

    if not task_rows:
        logger.debug("No tasks found for wave %d in project %s", wave_number, project_id)
        return None

    task_outcomes = []
    wave_cost = 0.0
    for row in task_rows:
        summary = (row["output_text"] or "")[:500]
        cost = row["task_cost"] or 0.0
        wave_cost += cost

        duration = None
        if row["started_at"] and row["completed_at"]:
            duration = round(row["completed_at"] - row["started_at"], 2)

        task_outcomes.append(
            TaskOutcomeSummary(
                task_id=row["id"],
                title=row["title"],
                status=row["status"],
                output_summary=summary,
                error=row["error"],
                model_tier=row["model_tier"],
                cost_usd=round(cost, 6),
                duration_seconds=duration,
            )
        )

    # ---------------------------------------------------------------
    # 2. Knowledge findings for the project
    # ---------------------------------------------------------------
    knowledge_rows = await db.fetchall(
        "SELECT id, content, category, confidence, rationale, source_task_title "
        "FROM project_knowledge "
        "WHERE project_id = $1 ORDER BY created_at DESC",
        (project_id,),
    )
    knowledge_findings = [
        KnowledgeFinding(
            id=row["id"] or "",
            content=row["content"],
            category=row["category"] or "discovery",
            confidence=row["confidence"] or "medium",
            rationale=row["rationale"] or "",
            source_task_title=row["source_task_title"] or "",
        )
        for row in knowledge_rows
    ]

    # ---------------------------------------------------------------
    # 3. Sentinel observations for the project
    #    Table columns: id, project_id, rule, severity, summary,
    #    details_json, reasoning, timestamp
    # ---------------------------------------------------------------
    observation_rows = await db.fetchall(
        "SELECT id, rule, severity, summary, details_json "
        "FROM sentinel_observations "
        "WHERE project_id = $1 ORDER BY timestamp DESC",
        (project_id,),
    )
    sentinel_observations = []
    for row in observation_rows:
        try:
            details = json.loads(row["details_json"]) if row["details_json"] else {}
        except (json.JSONDecodeError, TypeError):
            details = {}
        sentinel_observations.append(
            SentinelObservationSummary(
                id=row["id"] or "",
                category=row["rule"],
                severity=row["severity"],
                message=row["summary"],
                details=details,
            )
        )

    # ---------------------------------------------------------------
    # 4. Original plan
    # ---------------------------------------------------------------
    plan_row = await db.fetchone(
        "SELECT plan_json FROM plans WHERE project_id = $1 ORDER BY version DESC LIMIT 1",
        (project_id,),
    )
    if not plan_row:
        return None

    original_plan = json.loads(plan_row["plan_json"])

    return WaveReassessmentContext(
        project_id=project_id,
        wave_number=wave_number,
        task_outcomes=task_outcomes,
        knowledge_findings=knowledge_findings,
        sentinel_observations=sentinel_observations,
        original_plan=original_plan,
        wave_cost_usd=round(wave_cost, 6),
        all_tasks_terminal=True,
    )


async def execute_replan(
    *,
    db,
    budget,
    plan_sync,
    project_id: str,
    completed_wave: int,
    rationale: str,
    suggested_changes: list[str],
    wave_context_json: str | None = None,
    observation_ids: list[str] | None = None,
    finding_ids: list[str] | None = None,
) -> dict:
    """Cancel pending future-wave tasks, generate a new plan, decompose it, and record the revision.

    Called when wave reassessment returns replan_remaining.

    Args:
        db: Database instance.
        budget: BudgetManager instance.
        plan_sync: PlanSyncService instance for context store revision tracking.
        project_id: The project being replanned.
        completed_wave: The wave number that just finished.
        rationale: LLM rationale for why replanning is needed.
        suggested_changes: High-level changes suggested by the reassessment.
        wave_context_json: Serialized WaveReassessmentContext (appended to requirements).
        observation_ids: Context store observation IDs that triggered this replan.
        finding_ids: Context store finding IDs that informed this replan.

    Returns:
        Dict with replan summary (cancelled_count, new_plan_id, new_tasks_created, revision_node_id).
    """
    from backend.services.planner import PlannerService
    from backend.services.decomposer import DecomposerService

    logger.info(
        "Executing replan for project %s after wave %d: %s",
        project_id, completed_wave, rationale,
    )

    # 1. Cancel all pending/blocked/queued tasks in waves after the completed wave.
    #    Include QUEUED because those tasks haven't started executing yet.
    now = time.time()
    cancelled_status = await db.execute_write(
        "UPDATE tasks SET status = $1, updated_at = $2 "
        "WHERE project_id = $3 AND wave > $4 AND status IN ($5, $6, $7)",
        (
            TaskStatus.CANCELLED, now,
            project_id, completed_wave,
            TaskStatus.PENDING, TaskStatus.BLOCKED, TaskStatus.QUEUED,
        ),
    )
    cancelled_count = parse_rowcount(cancelled_status)
    logger.info(
        "Cancelled %s pending/blocked/queued tasks in waves > %d for project %s",
        cancelled_count, completed_wave, project_id,
    )

    # 2. Get the current plan ID (for revision tracking)
    old_plan_row = await db.fetchone(
        "SELECT id FROM plans WHERE project_id = $1 ORDER BY version DESC LIMIT 1",
        (project_id,),
    )
    old_plan_id = old_plan_row["id"] if old_plan_row else None

    # 3. Augment project requirements with wave findings for the replanning call.
    #    We temporarily update the project requirements to include the wave context,
    #    then restore after plan generation.
    project_row = await db.fetchone(
        "SELECT requirements FROM projects WHERE id = $1", (project_id,),
    )
    original_requirements = project_row["requirements"] if project_row else ""

    replan_addendum = (
        f"\n\n--- WAVE {completed_wave} REASSESSMENT ---\n"
        f"Rationale for replanning: {rationale}\n"
    )
    if suggested_changes:
        replan_addendum += "Suggested changes:\n"
        for change in suggested_changes:
            replan_addendum += f"  - {change}\n"
    if wave_context_json:
        replan_addendum += f"\nWave context (task outcomes, knowledge, observations):\n{wave_context_json}\n"

    augmented_requirements = original_requirements + replan_addendum

    await db.execute_write(
        "UPDATE projects SET requirements = $1, updated_at = $2 WHERE id = $3",
        (augmented_requirements, now, project_id),
    )

    try:
        # 4. Generate a new plan
        planner = PlannerService(db=db, budget=budget)
        plan_result = await planner.generate(project_id)
        new_plan_id = plan_result["plan_id"]
        logger.info(
            "New plan generated for project %s: plan_id=%s, version=%d",
            project_id, new_plan_id, plan_result["version"],
        )

        # 5. Auto-approve the new plan (system-triggered replan, no human gate)
        from backend.models.enums import PlanStatus as _PlanStatus
        await db.execute_write(
            "UPDATE plans SET status = $1 WHERE id = $2",
            (_PlanStatus.APPROVED, new_plan_id),
        )

        # 6. Decompose the new plan into tasks
        decomposer = DecomposerService(db=db)
        decompose_result = await decomposer.decompose(project_id, new_plan_id)
        logger.info(
            "New plan decomposed for project %s: %d tasks, %d waves",
            project_id, decompose_result["tasks_created"],
            decompose_result.get("total_waves", 0),
        )
    finally:
        # 7. Restore original requirements (the addendum was temporary context)
        await db.execute_write(
            "UPDATE projects SET requirements = $1, updated_at = $2 WHERE id = $3",
            (original_requirements, time.time(), project_id),
        )

    # 8. Set project back to EXECUTING so the executor picks up the new tasks.
    #    decomposer.decompose() sets status to READY (awaiting human approval),
    #    but auto-replan is system-triggered — no human gate needed.
    from backend.models.enums import ProjectStatus
    await db.execute_write(
        "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
        (ProjectStatus.EXECUTING, time.time(), project_id),
    )

    # 9. Sync the new plan's node tree to the context store so the new tasks
    #    get context store nodes (required for status update propagation).
    if plan_sync:
        project_row_full = await db.fetchone(
            "SELECT name, repo_path FROM projects WHERE id = $1", (project_id,),
        )
        if project_row_full:
            await plan_sync.sync_plan(
                project_name=project_row_full["name"],
                repo_path=project_row_full["repo_path"],
                plan_data=plan_result["plan"],
                plan_id=new_plan_id,
                project_id=project_id,
            )

    # 10. Record the revision in the context store
    revision_node_id = None
    if plan_sync and old_plan_id:
        delta = {
            "cancelled_tasks_in_waves_after": completed_wave,
            "cancelled_count": cancelled_count,
            "new_plan_id": new_plan_id,
            "new_tasks_created": decompose_result["tasks_created"],
            "suggested_changes": suggested_changes,
        }
        revision_node_id = await plan_sync.sync_revision(
            old_plan_id,
            wave_number=completed_wave,
            outcome="replan_remaining",
            rationale=rationale,
            delta=delta,
            observation_ids=observation_ids,
            finding_ids=finding_ids,
        )
        if revision_node_id:
            logger.info(
                "Revision node created in context store: %s", revision_node_id,
            )

    return {
        "cancelled_count": cancelled_count,
        "new_plan_id": new_plan_id,
        "new_tasks_created": decompose_result["tasks_created"],
        "new_total_waves": decompose_result.get("total_waves", 0),
        "revision_node_id": revision_node_id,
    }
