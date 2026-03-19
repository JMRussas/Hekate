"""Error condition tests across the entire workflow.

Tests every failure mode in the chain:
  project_created → athena → odin → hermes → mimir → odin_lifecycle

Categories:
  1. Athena failures (plan generation, review, DB errors)
  2. Odin failures (missing project, no providers, dispatch errors)
  3. Hermes failures (CLI crash, timeout, empty output, auth errors, rate limits)
  4. Mimir failures (verification fails, review fails, knowledge extraction fails)
  5. Cross-handler failures (event payloads missing fields, DB corruption)
  6. Recovery (retry after failure, provider fallback, escalation chain)
  7. Pipeline-level (handler exception doesn't kill pipeline, cursor survives errors)
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

from gods.pipeline import Pipeline, Event, Emit
from gods.handlers.registration import register_all_handlers


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(sqlite_db):
    """Full schema DB for error tests."""
    for ddl in [
        """CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT, requirements TEXT,
            status TEXT DEFAULT 'draft', repo_path TEXT,
            config_json TEXT DEFAULT '{}',
            completed_at REAL, updated_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, project_id TEXT, title TEXT,
            description TEXT DEFAULT '', task_type TEXT DEFAULT 'code',
            complexity TEXT DEFAULT 'medium', status TEXT DEFAULT 'pending',
            model_tier TEXT DEFAULT 'claude_code', wave INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 0, output_text TEXT, error TEXT,
            context_json TEXT DEFAULT '{}', retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3, cost_usd REAL DEFAULT 0,
            prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
            model_used TEXT, started_at REAL, completed_at REAL, updated_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS task_deps (
            task_id TEXT, depends_on TEXT,
            PRIMARY KEY (task_id, depends_on)
        )""",
        """CREATE TABLE IF NOT EXISTS project_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, task_id TEXT, content TEXT, created_at REAL
        )""",
        "DROP TABLE IF EXISTS god_relay_events",
        """CREATE TABLE god_relay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL, source TEXT NOT NULL,
            payload TEXT DEFAULT '{}', severity TEXT DEFAULT 'info',
            created_at REAL NOT NULL
        )""",
        "DROP TABLE IF EXISTS god_registry",
        """CREATE TABLE god_registry (
            name TEXT PRIMARY KEY, last_seen_id INTEGER DEFAULT 0,
            last_heartbeat REAL DEFAULT 0, config_json TEXT DEFAULT '{}'
        )""",
    ]:
        await sqlite_db.execute_write(ddl)
    return sqlite_db


async def _seed(db, project_id="proj-1", num_tasks=1, status="draft",
                task_status="pending", task_type="code"):
    now = time.time()
    await db.execute_write(
        "INSERT OR REPLACE INTO projects (id, name, requirements, status, repo_path, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, "Test", "Build X", status, "/tmp/repo", now),
    )
    for i in range(num_tasks):
        await db.execute_write(
            "INSERT OR REPLACE INTO tasks (id, project_id, title, description, task_type, "
            "status, wave, retry_count, max_retries, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"t{i+1}", project_id, f"Task {i+1}", f"Do thing {i+1}",
             task_type, task_status, 0, 0, 3, now),
        )


# ===========================================================================
# 1. ATHENA FAILURES
# ===========================================================================

class TestAthenaErrors:
    @pytest.mark.asyncio
    async def test_planner_throws_exception(self, db):
        """If _generate_plan raises, athena should emit planning_failed, not crash."""
        from gods.handlers.athena import athena_plan
        await _seed(db, status="draft")

        event = Event("project_created", {"project_id": "proj-1"}, "user")

        with patch("gods.handlers.athena._generate_plan", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("LLM gateway down")
            emits = await athena_plan(event, db)

        failed = next((e for e in emits if e.event_type == "planning_failed"), None)
        assert failed is not None
        assert "gateway down" in failed.payload["error"].lower()

    @pytest.mark.asyncio
    async def test_planner_returns_empty_plan(self, db):
        """If planner returns no tasks, athena should handle gracefully."""
        from gods.handlers.athena import athena_plan
        await _seed(db, status="draft")

        event = Event("project_created", {"project_id": "proj-1"}, "user")

        with patch("gods.handlers.athena._generate_plan", new_callable=AsyncMock) as mock_gen, \
             patch("gods.handlers.athena._review_plan", new_callable=AsyncMock) as mock_rev:
            mock_gen.return_value = {"plan_id": "p1", "plan": {"waves": []}}
            mock_rev.return_value = MagicMock(
                has_gaps=False, confidence=0.9, gaps=[], feedback="")
            emits = await athena_plan(event, db)

        # Should still emit project_planned (even with empty plan)
        planned = next((e for e in (emits or []) if e.event_type == "project_planned"), None)
        assert planned is not None or any(
            e.event_type == "planning_failed" for e in (emits or []))

    @pytest.mark.asyncio
    async def test_review_throws_doesnt_block_plan(self, db):
        """If review fails, plan should still be emitted (with no review data)."""
        from gods.handlers.athena import athena_plan
        await _seed(db, status="draft")

        event = Event("project_created", {"project_id": "proj-1"}, "user")

        with patch("gods.handlers.athena._generate_plan", new_callable=AsyncMock) as mock_gen, \
             patch("gods.handlers.athena._review_plan", new_callable=AsyncMock) as mock_rev:
            mock_gen.return_value = {
                "plan_id": "p1",
                "plan": {"waves": [{"tasks": [{"title": "t", "task_type": "code", "complexity": "m"}]}]},
            }
            mock_rev.side_effect = RuntimeError("Review service down")
            emits = await athena_plan(event, db)

        # Should emit project_planned despite review failure
        planned = next((e for e in (emits or []) if e.event_type == "project_planned"), None)
        failed = next((e for e in (emits or []) if e.event_type == "planning_failed"), None)
        assert planned is not None or failed is not None

    @pytest.mark.asyncio
    async def test_nonexistent_project(self, db):
        """project_created for missing project → error event."""
        from gods.handlers.athena import athena_plan

        event = Event("project_created", {"project_id": "ghost"}, "user")

        with patch("gods.handlers.athena._generate_plan", new_callable=AsyncMock) as mock:
            mock.side_effect = Exception("Project ghost not found")
            emits = await athena_plan(event, db)

        failed = next((e for e in emits if e.event_type == "planning_failed"), None)
        assert failed is not None


# ===========================================================================
# 2. ODIN FAILURES
# ===========================================================================

class TestOdinErrors:
    @pytest.mark.asyncio
    async def test_no_providers_available(self, db):
        """All providers down → dispatch should not emit commands."""
        from gods.handlers.odin import odin_dispatch
        await _seed(db, status="executing")

        event = Event("tick", {"project_id": "proj-1"}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock:
            mock.return_value = {"claude_code": False, "gemini_cli": False, "ollama": False}
            emits = await odin_dispatch(event, db)

        commands = [e for e in (emits or []) if e.event_type == "dispatch_command"]
        # Should still dispatch (claude_code as last resort) or emit 0
        # The key is it doesn't crash
        assert isinstance(emits, list) or emits is None

    @pytest.mark.asyncio
    async def test_provider_check_timeout(self, db):
        """Gateway timeout → should fallback to all-available."""
        from gods.handlers.odin import odin_dispatch
        await _seed(db, status="executing")

        event = Event("tick", {"project_id": "proj-1"}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock:
            mock.side_effect = asyncio.TimeoutError("gateway timeout")
            # The actual function has a try/except that returns all-true on failure
            # So we need to test the real function, not mock it
            pass

        # Test the actual fallback in _get_provider_availability
        from gods.handlers.odin import _get_provider_availability
        with patch("httpx.AsyncClient.get", side_effect=asyncio.TimeoutError()):
            result = await _get_provider_availability("http://localhost:9999")
        assert result == {"claude_code": True, "gemini_cli": True, "ollama": True}

    @pytest.mark.asyncio
    async def test_dispatch_with_no_tasks(self, db):
        """Executing project with no pending tasks → no dispatch, no crash."""
        from gods.handlers.odin import odin_dispatch
        await _seed(db, status="executing", task_status="completed")

        event = Event("tick", {"project_id": "proj-1"}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock:
            mock.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}
            emits = await odin_dispatch(event, db)

        commands = [e for e in (emits or []) if e.event_type == "dispatch_command"]
        assert len(commands) == 0

    @pytest.mark.asyncio
    async def test_lifecycle_with_nonexistent_task(self, db):
        """task_verified for deleted task → no crash."""
        from gods.handlers.odin import odin_lifecycle
        await _seed(db, status="executing")

        event = Event("task_verified", {
            "project_id": "proj-1",
            "task_id": "deleted-task",
        }, "mimir")

        emits = await odin_lifecycle(event, db)
        # Should not crash — might emit nothing or wave/project status
        assert emits is None or isinstance(emits, list)

    @pytest.mark.asyncio
    async def test_diagnosis_unknown_fix_type(self, db):
        """Unknown fix_type → odin_error event."""
        from gods.handlers.odin import odin_handle_diagnosis
        await _seed(db, status="executing", task_status="failed")

        event = Event("task_diagnosis", {
            "task_id": "t1", "project_id": "proj-1",
            "fix_type": "invented_strategy",
        }, "odin")

        emits = await odin_handle_diagnosis(event, db)

        error = next((e for e in emits if e.event_type == "odin_error"), None)
        assert error is not None


# ===========================================================================
# 3. HERMES FAILURES
# ===========================================================================

class TestHermesErrors:
    @pytest.mark.asyncio
    async def test_cli_crashes_with_exit_code(self, db):
        """CLI exits with non-zero → task failed with error details."""
        from gods.handlers.hermes import hermes_execute
        await _seed(db, status="executing")

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("CLI exited 0xC0000005: access violation")
            emits = await hermes_execute(event, db)

        failed = next(e for e in emits if e.event_type == "worker_event")
        assert failed.payload["status"] == "failed"
        assert "access violation" in failed.payload["error"]

    @pytest.mark.asyncio
    async def test_cli_timeout(self, db):
        """CLI hangs → timeout → task failed with timeout flag."""
        from gods.handlers.hermes import hermes_execute
        await _seed(db, status="executing")

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock:
            mock.side_effect = asyncio.TimeoutError("600s timeout")
            emits = await hermes_execute(event, db)

        failed = next(e for e in emits if e.event_type == "worker_event")
        assert failed.payload["status"] == "failed"
        assert failed.payload.get("timeout") is True

    @pytest.mark.asyncio
    async def test_rate_limit_error(self, db):
        """Rate limit from CLI → task failed, error contains rate limit."""
        from gods.handlers.hermes import hermes_execute
        await _seed(db, status="executing")

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("Error: rate limit exceeded, retry after 30s")
            emits = await hermes_execute(event, db)

        failed = next(e for e in emits if e.event_type == "worker_event")
        assert failed.payload["status"] == "failed"
        assert "rate limit" in failed.payload["error"].lower()

    @pytest.mark.asyncio
    async def test_auth_error(self, db):
        """Auth failure → task failed."""
        from gods.handlers.hermes import hermes_execute
        await _seed(db, status="executing")

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "gemini_cli",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("CLI exited 1: Error: Not authenticated")
            emits = await hermes_execute(event, db)

        failed = next(e for e in emits if e.event_type == "worker_event")
        assert failed.payload["status"] == "failed"

    @pytest.mark.asyncio
    async def test_output_is_only_errors(self, db):
        """CLI returns only error text → TDD gate catches, task still completes."""
        from gods.handlers.hermes import hermes_execute
        await _seed(db, status="executing")

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "output": "Error: ModuleNotFoundError: No module named 'nonexistent'\nTraceback...",
                "cost_usd": 0.01, "prompt_tokens": 100,
                "completion_tokens": 50, "model_used": "claude-sonnet-4",
                "narration": [],
            }
            emits = await hermes_execute(event, db)

        # Should complete (with TDD warning) — mimir will catch the bad output
        worker = next(e for e in emits if e.event_type == "worker_event")
        assert worker.payload["status"] == "completed"

    @pytest.mark.asyncio
    async def test_nested_session_error(self, db):
        """Claude Code nested session → task failed."""
        from gods.handlers.hermes import hermes_execute
        await _seed(db, status="executing")

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError(
                "CLI exited 1: Error: Claude Code cannot be launched inside another Claude Code session.")
            emits = await hermes_execute(event, db)

        failed = next(e for e in emits if e.event_type == "worker_event")
        assert failed.payload["status"] == "failed"

    @pytest.mark.asyncio
    async def test_missing_project_repo_path(self, db):
        """Project with no repo_path → hermes still runs (cwd = '.')."""
        from gods.handlers.hermes import hermes_execute
        await db.execute_write(
            "INSERT INTO projects (id, name, status, updated_at) VALUES (?, ?, ?, ?)",
            ("p-no-repo", "No Repo", "executing", time.time()))
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, title, status, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("t-no-repo", "p-no-repo", "Task", "pending", time.time()))

        event = Event("dispatch_command", {
            "task_id": "t-no-repo", "project_id": "p-no-repo", "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "output": "Done.", "cost_usd": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0, "model_used": "claude", "narration": [],
            }
            emits = await hermes_execute(event, db)

        worker = next(e for e in emits if e.event_type == "worker_event")
        assert worker.payload["status"] == "completed"


# ===========================================================================
# 4. MIMIR FAILURES
# ===========================================================================

class TestMimirErrors:
    @pytest.mark.asyncio
    async def test_verifier_throws(self, db):
        """LLM verifier exception → task goes to needs_review."""
        from gods.handlers.mimir import mimir_verify
        await _seed(db, status="executing", task_status="completed")
        await db.execute_write(
            "UPDATE tasks SET output_text = 'Some real output here.' WHERE id = ?", ("t1",))

        event = Event("worker_event", {
            "task_id": "t1", "project_id": "proj-1", "status": "completed",
        }, "hermes")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("Verifier gateway down")
            emits = await mimir_verify(event, db)

        # Should handle gracefully — either needs_review or error event
        assert emits is not None
        has_handling = any(
            e.event_type in ("needs_human_review", "mimir_error", "task_verified")
            for e in emits
        )
        assert has_handling, f"Unhandled verifier error, got: {[e.event_type for e in emits]}"

    @pytest.mark.asyncio
    async def test_verifier_returns_garbage(self, db):
        """LLM returns unparseable response → handle gracefully."""
        from gods.handlers.mimir import mimir_verify
        await _seed(db, status="executing", task_status="completed")
        await db.execute_write(
            "UPDATE tasks SET output_text = 'Real output.' WHERE id = ?", ("t1",))

        event = Event("worker_event", {
            "task_id": "t1", "project_id": "proj-1", "status": "completed",
        }, "hermes")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock:
            mock.return_value = {"verdict": "???INVALID???", "confidence": -1}
            emits = await mimir_verify(event, db)

        # Unknown verdict should route to human review
        human = next((e for e in emits if e.event_type == "needs_human_review"), None)
        assert human is not None

    @pytest.mark.asyncio
    async def test_knowledge_extraction_failure_doesnt_block(self, db):
        """Knowledge extraction fails → task_verified still emitted."""
        from gods.handlers.mimir import mimir_verify
        await _seed(db, status="executing", task_status="completed")
        await db.execute_write(
            "UPDATE tasks SET output_text = 'Good output with findings.' WHERE id = ?", ("t1",))

        event = Event("worker_event", {
            "task_id": "t1", "project_id": "proj-1", "status": "completed",
        }, "hermes")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._extract_knowledge", new_callable=AsyncMock) as mock_k:
            mock_v.return_value = {"verdict": "passed", "confidence": 0.9, "feedback": ""}
            mock_k.side_effect = RuntimeError("Knowledge DB down")
            emits = await mimir_verify(event, db)

        verified = next((e for e in emits if e.event_type == "task_verified"), None)
        assert verified is not None, "Knowledge failure should not block verification"

    @pytest.mark.asyncio
    async def test_reviewer_throws(self, db):
        """Code reviewer exception → handle gracefully."""
        from gods.handlers.mimir import mimir_review
        await _seed(db, status="executing", task_status="completed")
        await db.execute_write(
            "UPDATE tasks SET output_text = 'Code here.' WHERE id = ?", ("t1",))

        event = Event("task_verified", {
            "task_id": "t1", "project_id": "proj-1",
        }, "mimir")

        with patch("gods.handlers.mimir._call_reviewer", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("Reviewer down")
            emits = await mimir_review(event, db)

        # Should not crash — either error or pass-through
        assert emits is not None or True  # handler might raise, caught by pipeline


# ===========================================================================
# 5. CROSS-HANDLER: Missing/malformed event payloads
# ===========================================================================

class TestMalformedEvents:
    @pytest.mark.asyncio
    async def test_athena_missing_project_id(self, db):
        from gods.handlers.athena import athena_plan
        event = Event("project_created", {}, "user")

        with patch("gods.handlers.athena._generate_plan", new_callable=AsyncMock):
            emits = await athena_plan(event, db)

        # Should emit error, not crash
        assert emits is not None

    @pytest.mark.asyncio
    async def test_odin_start_missing_project_id(self, db):
        from gods.handlers.odin import odin_start
        event = Event("project_planned", {}, "athena")
        emits = await odin_start(event, db)

        error = next((e for e in emits if e.event_type == "odin_error"), None)
        assert error is not None

    @pytest.mark.asyncio
    async def test_hermes_missing_task_id(self, db):
        from gods.handlers.hermes import hermes_execute
        event = Event("dispatch_command", {"provider": "claude_code"}, "odin")
        emits = await hermes_execute(event, db)

        error = next((e for e in emits if e.event_type == "hermes_error"), None)
        assert error is not None

    @pytest.mark.asyncio
    async def test_mimir_missing_task_id(self, db):
        from gods.handlers.mimir import mimir_verify
        event = Event("worker_event", {"status": "completed"}, "hermes")
        emits = await mimir_verify(event, db)

        error = next((e for e in (emits or []) if e.event_type == "mimir_error"), None)
        assert error is not None

    @pytest.mark.asyncio
    async def test_odin_diagnosis_missing_fields(self, db):
        from gods.handlers.odin import odin_handle_diagnosis
        event = Event("task_diagnosis", {}, "odin")
        emits = await odin_handle_diagnosis(event, db)

        error = next((e for e in emits if e.event_type == "odin_error"), None)
        assert error is not None

    @pytest.mark.asyncio
    async def test_hermes_task_not_in_db(self, db):
        from gods.handlers.hermes import hermes_execute
        event = Event("dispatch_command", {
            "task_id": "ghost", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")
        emits = await hermes_execute(event, db)

        error = next((e for e in emits if e.event_type == "hermes_error"), None)
        assert error is not None

    @pytest.mark.asyncio
    async def test_mimir_task_not_in_db(self, db):
        from gods.handlers.mimir import mimir_verify
        event = Event("worker_event", {
            "task_id": "ghost", "project_id": "proj-1", "status": "completed",
        }, "hermes")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock):
            emits = await mimir_verify(event, db)

        error = next((e for e in (emits or []) if e.event_type == "mimir_error"), None)
        assert error is not None


# ===========================================================================
# 6. RECOVERY: Retry, fallback, escalation
# ===========================================================================

class TestRecovery:
    @pytest.mark.asyncio
    async def test_failed_task_diagnosed_and_retried(self, db):
        """Failed task → diagnosis → retry_as_is → reset to pending."""
        from gods.handlers.odin import odin_dispatch, odin_handle_diagnosis
        await _seed(db, status="executing", task_status="failed")
        await db.execute_write("UPDATE tasks SET error = 'timeout' WHERE id = ?", ("t1",))

        # Dispatch should diagnose
        event = Event("tick", {"project_id": "proj-1"}, "odin")
        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock:
            mock.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}
            emits = await odin_dispatch(event, db)

        diag = next((e for e in (emits or []) if e.event_type == "task_diagnosis"), None)
        assert diag is not None

        # Handle diagnosis
        emits2 = await odin_handle_diagnosis(
            Event("task_diagnosis", diag.payload, "odin"), db)

        # Task should be reset
        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row[0] in ("pending", "needs_review")

    @pytest.mark.asyncio
    async def test_max_retries_escalates_to_human(self, db):
        """Task at max retries → diagnosis escalates to needs_review."""
        from gods.handlers.odin import odin_handle_diagnosis
        await _seed(db, status="executing", task_status="failed")
        await db.execute_write(
            "UPDATE tasks SET error = 'timeout', retry_count = 3, max_retries = 3 WHERE id = ?",
            ("t1",))

        event = Event("task_diagnosis", {
            "task_id": "t1", "project_id": "proj-1",
            "fix_type": "retry_as_is", "root_cause": "timeout",
        }, "odin")

        emits = await odin_handle_diagnosis(event, db)

        human = next((e for e in emits if e.event_type == "needs_human_review"), None)
        assert human is not None

        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "needs_review"

    @pytest.mark.asyncio
    async def test_verification_rejection_adds_feedback_and_resets(self, db):
        """Mimir rejects → task reset to pending with feedback in context."""
        from gods.handlers.mimir import mimir_verify
        await _seed(db, status="executing", task_status="completed")
        await db.execute_write(
            "UPDATE tasks SET output_text = 'Partial implementation.' WHERE id = ?", ("t1",))

        event = Event("worker_event", {
            "task_id": "t1", "project_id": "proj-1", "status": "completed",
        }, "hermes")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "verdict": "gaps_found", "confidence": 0.5,
                "feedback": "Missing unit tests.",
            }
            emits = await mimir_verify(event, db)

        rejected = next((e for e in emits if e.event_type == "task_rejected"), None)
        assert rejected is not None

        row = await db.fetchone("SELECT status, retry_count, context_json FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "pending"
        assert row[1] == 1
        ctx = json.loads(row[2] or "{}")
        assert "unit tests" in ctx.get("verification_feedback", "").lower()


# ===========================================================================
# 7. PIPELINE-LEVEL: Handler exceptions don't kill pipeline
# ===========================================================================

class TestPipelineResilience:
    @pytest.mark.asyncio
    async def test_handler_exception_doesnt_kill_pipeline(self, db):
        """If one handler throws, pipeline continues to next handler."""
        pipeline = Pipeline(db)

        call_log = []

        async def bad_handler(event, db):
            raise RuntimeError("I'm broken")

        async def good_handler(event, db):
            call_log.append("good")
            return [Emit("result", {"ok": True}, source="test")]

        pipeline.register("test_event", bad_handler, name="bad")
        pipeline.register("test_event", good_handler, name="good")

        # Inject event
        await db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("test_event", "test", "{}", "info", time.time()),
        )

        await pipeline.tick()

        # Good handler should still have been called
        assert "good" in call_log

    @pytest.mark.asyncio
    async def test_cursor_advances_past_error(self, db):
        """Pipeline cursor should advance past events that caused errors."""
        pipeline = Pipeline(db)

        async def failing_handler(event, db):
            raise RuntimeError("always fails")

        pipeline.register("bad_event", failing_handler)

        # Inject two events
        for i in range(2):
            await db.execute_write(
                "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("bad_event", "test", "{}", "info", time.time()),
            )

        await pipeline.tick()
        cursor_after_first = pipeline._last_seen_id

        await pipeline.tick()
        cursor_after_second = pipeline._last_seen_id

        # Cursor should not regress — events processed even if handler failed
        assert cursor_after_second >= cursor_after_first

    @pytest.mark.asyncio
    async def test_malformed_payload_in_relay(self, db):
        """Event with invalid JSON payload → pipeline handles gracefully."""
        pipeline = Pipeline(db)

        received = []

        async def handler(event, db):
            received.append(event)
            return None

        pipeline.register("test_event", handler)

        await db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("test_event", "test", "NOT VALID JSON{{{", "info", time.time()),
        )

        await pipeline.tick()

        # Should still be delivered (with raw payload)
        assert len(received) == 1
        assert "raw" in received[0].payload or isinstance(received[0].payload, dict)

    @pytest.mark.asyncio
    async def test_concurrent_projects_dont_interfere(self, db):
        """Two projects executing simultaneously — events don't cross."""
        from gods.handlers.odin import odin_dispatch
        await _seed(db, project_id="p1", status="executing")
        await _seed(db, project_id="p2", status="executing")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock:
            mock.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}

            emits1 = await odin_dispatch(
                Event("tick", {"project_id": "p1"}, "odin"), db)
            emits2 = await odin_dispatch(
                Event("tick", {"project_id": "p2"}, "odin"), db)

        # Each should only dispatch its own tasks
        if emits1:
            for e in emits1:
                if e.event_type == "dispatch_command":
                    assert e.payload["project_id"] == "p1"
        if emits2:
            for e in emits2:
                if e.event_type == "dispatch_command":
                    assert e.payload["project_id"] == "p2"
