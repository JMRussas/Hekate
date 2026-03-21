"""Tests for MimirRunner — async verification with in-flight tracking.

MimirRunner manages background LLM verification:
  - handle_verify: receives worker_event:completed, launches background task, returns immediately
  - _verify_task: background coroutine that calls LLM verifier, writes results to relay
  - shutdown: gracefully waits for in-flight verifications

Test categories:
  - Launch: immediate return, dedup, slots full, idempotency
  - Fast-path: already verified, already-done phrases
  - Heuristic: empty output retry, empty output escalation
  - Background: relay writes on success, relay writes on gaps, exception handling
  - Normalization: non-dict LLM responses
  - Lifecycle: shutdown waits for in-flight, deferred queue drains
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from gods.pipeline import Event, Emit
from gods.handlers.mimir import MimirRunner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(sqlite_db):
    """SQLite with full schema for MimirRunner tests."""
    for ddl in [
        """CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT,
            status TEXT DEFAULT 'executing',
            config_json TEXT DEFAULT '{}',
            updated_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, project_id TEXT, title TEXT,
            description TEXT DEFAULT '', task_type TEXT DEFAULT 'code',
            complexity TEXT DEFAULT 'medium', status TEXT DEFAULT 'completed',
            model_tier TEXT DEFAULT 'claude_code', output_text TEXT,
            error TEXT, context_json TEXT DEFAULT '{}',
            retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3,
            verification_status TEXT, verification_notes TEXT,
            updated_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS project_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, task_id TEXT, content TEXT,
            created_at REAL
        )""",
    ]:
        await sqlite_db.execute_write(ddl)
    return sqlite_db


async def _seed_task(db, task_id="t1", project_id="proj-1", status="completed",
                     output_text="Implemented feature X. All tests pass.",
                     description="Build feature X", retry_count=0, max_retries=3,
                     verification_status=None):
    now = time.time()
    await db.execute_write(
        "INSERT OR REPLACE INTO projects (id, name, status, updated_at) VALUES (?, ?, ?, ?)",
        (project_id, "Test", "executing", now),
    )
    await db.execute_write(
        "INSERT OR REPLACE INTO tasks (id, project_id, title, description, task_type, "
        "status, output_text, retry_count, max_retries, verification_status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, project_id, f"Task {task_id}", description, "code",
         status, output_text, retry_count, max_retries, verification_status, now),
    )


def _completed_event(task_id="t1", project_id="proj-1"):
    return Event("worker_event", {
        "task_id": task_id,
        "project_id": project_id,
        "status": "completed",
    }, "hermes")


# ===========================================================================
# 1. LAUNCH — handle_verify returns verification_started, tracks task
# ===========================================================================

class TestHandleVerifyLaunch:
    @pytest.mark.asyncio
    async def test_handle_verify_launches_background_task(self, db):
        """handle_verify returns verification_started emit and task appears in _tasks."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            # Slow verifier so background task stays in-flight
            async def slow_verify(**kw):
                await asyncio.sleep(5.0)
                return {"verdict": "passed", "confidence": 0.9, "feedback": ""}
            mock_v.side_effect = slow_verify
            mock_k.return_value = []

            emits = await runner.handle_verify(_completed_event())

        # Should emit verification_started
        assert emits is not None
        started = next((e for e in emits if e.event_type == "verification_started"), None)
        assert started is not None
        assert started.payload["task_id"] == "t1"

        # Task should be tracked in _tasks
        assert "t1" in runner._tasks
        assert "t1" in runner.in_flight

        await runner.shutdown(timeout=0.5)

    @pytest.mark.asyncio
    async def test_handle_verify_dedup(self, db):
        """Second call with same task_id returns None (dedup)."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            async def slow_verify(**kw):
                await asyncio.sleep(5.0)
                return {"verdict": "passed", "confidence": 0.9, "feedback": ""}
            mock_v.side_effect = slow_verify

            emits1 = await runner.handle_verify(_completed_event())
            emits2 = await runner.handle_verify(_completed_event())

        # First should launch
        assert emits1 is not None
        assert any(e.event_type == "verification_started" for e in emits1)

        # Second should be None (dedup)
        assert emits2 is None

        await runner.shutdown(timeout=0.5)

    @pytest.mark.asyncio
    async def test_handle_verify_slots_full(self, db):
        """When max_concurrent reached, returns verification_deferred."""
        runner = MimirRunner(db=db, max_concurrent=1)

        # Seed two tasks
        await _seed_task(db, task_id="t1")
        await _seed_task(db, task_id="t2", output_text="Implemented feature Y. Tests pass.")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            async def slow_verify(**kw):
                await asyncio.sleep(5.0)
                return {"verdict": "passed", "confidence": 0.9, "feedback": ""}
            mock_v.side_effect = slow_verify

            emits1 = await runner.handle_verify(_completed_event("t1"))
            emits2 = await runner.handle_verify(_completed_event("t2"))

        # First should launch
        assert any(e.event_type == "verification_started" for e in emits1)

        # Second should be deferred
        assert emits2 is not None
        deferred = next((e for e in emits2 if e.event_type == "verification_deferred"), None)
        assert deferred is not None
        assert deferred.payload["task_id"] == "t2"
        assert deferred.payload["in_flight"] == 1
        assert deferred.payload["max_concurrent"] == 1

        await runner.shutdown(timeout=0.5)


# ===========================================================================
# 2. FAST-PATH — already verified, already-done phrases
# ===========================================================================

class TestHandleVerifyFastPath:
    @pytest.mark.asyncio
    async def test_handle_verify_already_verified(self, db):
        """Task with verification_status='passed' returns task_verified immediately."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db, verification_status="passed")

        emits = await runner.handle_verify(_completed_event())

        assert emits is not None
        verified = next((e for e in emits if e.event_type == "task_verified"), None)
        assert verified is not None
        assert verified.payload["task_id"] == "t1"
        assert verified.payload["confidence"] == 1.0
        assert verified.payload["already_verified"] is True

        # Should NOT launch background task
        assert "t1" not in runner._tasks

    @pytest.mark.asyncio
    async def test_handle_verify_already_done_phrases(self, db):
        """Output containing 'already exists' etc. passes without LLM call."""
        runner = MimirRunner(db=db, max_concurrent=4)

        phrases = [
            "The file already exists in the project",
            "Feature already done and tested",
            "Config already in place, nothing to do",
            "Module already has the required export",
            "Component already present in the bundle",
            "Interface already defined in types.ts",
            "Function already implemented correctly",
            "Nothing to do — all changes were applied",
            "No changes needed, file already matches spec",
            "File already contains the requested code",
        ]

        for i, phrase in enumerate(phrases):
            task_id = f"t-phrase-{i}"
            await _seed_task(db, task_id=task_id, output_text=phrase)

            emits = await runner.handle_verify(_completed_event(task_id))

            assert emits is not None, f"Phrase {i} returned None: {phrase}"
            verified = next((e for e in emits if e.event_type == "task_verified"), None)
            assert verified is not None, f"Phrase {i} missing task_verified: {phrase}"
            assert verified.payload["already_done"] is True
            assert verified.payload["confidence"] == 0.8

            # Should NOT launch background task
            assert task_id not in runner._tasks


# ===========================================================================
# 3. HEURISTIC — empty output retries and escalation
# ===========================================================================

class TestHeuristicFailure:
    @pytest.mark.asyncio
    async def test_handle_verify_heuristic_failure_retries(self, db):
        """Empty output triggers retry when retry_count < max_retries."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db, output_text="", retry_count=0, max_retries=3)

        emits = await runner.handle_verify(_completed_event())

        # Should emit task_rejected (retry)
        assert emits is not None
        rejected = next((e for e in emits if e.event_type == "task_rejected"), None)
        assert rejected is not None
        assert rejected.payload["task_id"] == "t1"
        assert "empty" in rejected.payload["feedback"].lower()

        # DB should show pending with bumped retry_count
        row = await db.fetchone("SELECT status, retry_count, context_json FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "pending"
        assert row["retry_count"] == 1
        assert "verification_feedback" in (row["context_json"] or "")

        # No background task launched
        assert "t1" not in runner._tasks

    @pytest.mark.asyncio
    async def test_handle_verify_heuristic_failure_escalates(self, db):
        """Empty output escalates to needs_review when retries exhausted."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db, output_text="", retry_count=3, max_retries=3)

        emits = await runner.handle_verify(_completed_event())

        # Should emit needs_human_review
        assert emits is not None
        review = next((e for e in emits if e.event_type == "needs_human_review"), None)
        assert review is not None
        assert review.payload["task_id"] == "t1"

        # DB should show needs_review
        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "needs_review"

    @pytest.mark.asyncio
    async def test_short_output_triggers_heuristic_failure(self, db):
        """Very short output (< 10 chars) triggers heuristic failure."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db, output_text="ok", retry_count=0, max_retries=3)

        emits = await runner.handle_verify(_completed_event())

        assert emits is not None
        rejected = next((e for e in emits if e.event_type == "task_rejected"), None)
        assert rejected is not None
        assert "short" in rejected.payload["feedback"].lower()


# ===========================================================================
# 4. BACKGROUND — relay writes on success, gaps, exception
# ===========================================================================

class TestVerifyTaskBackground:
    @pytest.mark.asyncio
    async def test_verify_task_writes_relay_on_success(self, db):
        """Background task writes task_verified to relay on passed verdict."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            mock_v.return_value = {"verdict": "passed", "confidence": 0.95, "feedback": ""}
            mock_k.return_value = ["Finding 1"]

            emits = await runner.handle_verify(_completed_event())
            assert any(e.event_type == "verification_started" for e in emits)

            # Wait for background task to finish
            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        # Check relay table
        rows = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = ?",
            ("task_verified",))
        assert len(rows) >= 1
        payload = json.loads(rows[0]["payload"])
        assert payload["task_id"] == "t1"
        assert payload["project_id"] == "proj-1"
        assert payload["confidence"] == 0.95

    @pytest.mark.asyncio
    async def test_verify_task_writes_relay_on_gaps(self, db):
        """Background task writes task_rejected to relay on gaps_found verdict."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db, retry_count=0, max_retries=3)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = {
                "verdict": "gaps_found",
                "confidence": 0.6,
                "feedback": "Missing error handling.",
            }

            emits = await runner.handle_verify(_completed_event())
            assert any(e.event_type == "verification_started" for e in emits)

            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        # Check relay for task_rejected
        rows = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = ?",
            ("task_rejected",))
        assert len(rows) >= 1
        payload = json.loads(rows[0]["payload"])
        assert payload["task_id"] == "t1"
        assert "error handling" in payload["feedback"].lower()

        # DB should show pending with retry bump
        row = await db.fetchone("SELECT status, retry_count FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "pending"
        assert row["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_verify_task_handles_verifier_exception_with_output(self, db):
        """If _call_verifier throws and task has output, auto-pass with 0.5 confidence."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db, output_text="Implemented feature X with full test coverage and documentation.")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            mock_v.side_effect = ConnectionError("Gateway unavailable")
            mock_k.return_value = []

            emits = await runner.handle_verify(_completed_event())
            assert any(e.event_type == "verification_started" for e in emits)

            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        # Should auto-pass with 0.5 confidence
        rows = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = ?",
            ("task_verified",))
        assert len(rows) >= 1
        payload = json.loads(rows[0]["payload"])
        assert payload["task_id"] == "t1"
        assert payload["confidence"] == 0.5

    @pytest.mark.asyncio
    async def test_verify_task_handles_verifier_exception_no_output(self, db):
        """If _call_verifier throws and task has output > 20 chars, auto-pass with 0.5."""
        runner = MimirRunner(db=db, max_concurrent=4)
        # Output > 20 chars stripped and passes heuristic
        await _seed_task(db, output_text="Some short text here.")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            mock_v.side_effect = ConnectionError("Gateway unavailable")
            mock_k.return_value = []

            emits = await runner.handle_verify(_completed_event())
            assert any(e.event_type == "verification_started" for e in emits)

            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        # The output is > 20 chars stripped, so it should auto-pass with 0.5
        rows = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = ?",
            ("task_verified",))
        assert len(rows) >= 1
        payload = json.loads(rows[0]["payload"])
        assert payload["confidence"] == 0.5

    @pytest.mark.asyncio
    async def test_verify_task_exception_short_output_escalates(self, db):
        """If _call_verifier throws and output is very short (<=20 chars), escalate to needs_review."""
        runner = MimirRunner(db=db, max_concurrent=4)
        # Need output that passes _check_output_quality but is <= 20 stripped chars
        # _check_output_quality requires >= 10 chars and no error patterns
        # So 11-20 char output should pass heuristic but fail the verifier fallback
        await _seed_task(db, output_text="Done it all.")  # 12 chars stripped

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.side_effect = ConnectionError("Gateway unavailable")

            emits = await runner.handle_verify(_completed_event())
            assert any(e.event_type == "verification_started" for e in emits)

            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        # Short output + verifier failure -> escalate to needs_human_review
        rows = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = ?",
            ("needs_human_review",))
        assert len(rows) >= 1
        payload = json.loads(rows[0]["payload"])
        assert payload["task_id"] == "t1"

        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "needs_review"

    @pytest.mark.asyncio
    async def test_gaps_found_escalates_after_max_retries(self, db):
        """Background gaps_found with retries exhausted -> needs_human_review in relay."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db, retry_count=3, max_retries=3)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = {
                "verdict": "gaps_found",
                "confidence": 0.4,
                "feedback": "Still missing tests after 3 retries.",
            }

            emits = await runner.handle_verify(_completed_event())
            assert any(e.event_type == "verification_started" for e in emits)

            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = ?",
            ("needs_human_review",))
        assert len(rows) >= 1
        payload = json.loads(rows[0]["payload"])
        assert "3 retries" in payload["reason"]

        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "needs_review"


# ===========================================================================
# 5. NORMALIZATION — non-dict LLM responses
# ===========================================================================

class TestVerifyTaskNormalization:
    @pytest.mark.asyncio
    async def test_string_response_passed(self, db):
        """String response containing 'passed' normalizes to passed verdict."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            mock_v.return_value = "All checks passed successfully"
            mock_k.return_value = []

            await runner.handle_verify(_completed_event())
            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = ?",
            ("task_verified",))
        assert len(rows) >= 1

    @pytest.mark.asyncio
    async def test_string_response_unclear(self, db):
        """String response without pass keywords -> human_needed."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = "I'm not sure about this output"

            await runner.handle_verify(_completed_event())
            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = ?",
            ("needs_human_review",))
        assert len(rows) >= 1

    @pytest.mark.asyncio
    async def test_list_response_with_dict(self, db):
        """List containing a dict uses first dict element."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            mock_v.return_value = [{"verdict": "passed", "confidence": 0.85, "feedback": "OK"}]
            mock_k.return_value = []

            await runner.handle_verify(_completed_event())
            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = ?",
            ("task_verified",))
        assert len(rows) >= 1

    @pytest.mark.asyncio
    async def test_list_response_without_dict(self, db):
        """List of strings -> human_needed."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = ["some", "random", "strings"]

            await runner.handle_verify(_completed_event())
            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = ?",
            ("needs_human_review",))
        assert len(rows) >= 1

    @pytest.mark.asyncio
    async def test_none_response(self, db):
        """None response -> human_needed."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = None

            await runner.handle_verify(_completed_event())
            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = ?",
            ("needs_human_review",))
        assert len(rows) >= 1

    @pytest.mark.asyncio
    async def test_dict_missing_verdict_key(self, db):
        """Dict without 'verdict' key defaults to human_needed."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = {"confidence": 0.5, "feedback": "Hmm"}

            await runner.handle_verify(_completed_event())
            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = ?",
            ("needs_human_review",))
        assert len(rows) >= 1


# ===========================================================================
# 6. LIFECYCLE — shutdown, in-flight cleanup
# ===========================================================================

class TestMimirRunnerLifecycle:
    @pytest.mark.asyncio
    async def test_shutdown_waits_for_inflight(self, db):
        """shutdown() waits for background tasks to complete."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        completed = []

        async def slow_verify(**kw):
            await asyncio.sleep(0.3)
            completed.append(True)
            return {"verdict": "passed", "confidence": 0.9, "feedback": ""}

        with patch("gods.handlers.mimir._call_verifier", side_effect=slow_verify), \
             patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            mock_k.return_value = []

            await runner.handle_verify(_completed_event())
            assert "t1" in runner.in_flight

            # Shutdown should wait for the 0.3s task
            await runner.shutdown(timeout=5.0)

        assert len(completed) == 1
        assert len(runner._tasks) == 0

    @pytest.mark.asyncio
    async def test_shutdown_timeout_forces_cleanup(self, db):
        """If tasks don't finish in time, shutdown still completes."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            async def hang_forever(**kw):
                await asyncio.sleep(999)
                return {"verdict": "passed", "confidence": 1.0, "feedback": ""}
            mock_v.side_effect = hang_forever

            await runner.handle_verify(_completed_event())
            assert "t1" in runner.in_flight

            # Short timeout -- should not hang
            await runner.shutdown(timeout=0.5)

        # _tasks should be cleared
        assert len(runner._tasks) == 0

    @pytest.mark.asyncio
    async def test_shutdown_noop_when_empty(self, db):
        """shutdown() with no in-flight tasks is a no-op."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await runner.shutdown(timeout=1.0)
        assert len(runner._tasks) == 0

    @pytest.mark.asyncio
    async def test_in_flight_cleared_after_background_completes(self, db):
        """Task removed from in_flight after background task finishes."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            mock_v.return_value = {"verdict": "passed", "confidence": 0.9, "feedback": ""}
            mock_k.return_value = []

            await runner.handle_verify(_completed_event())
            assert "t1" in runner.in_flight

            # Wait for background to finish
            await asyncio.sleep(0.3)

        # done_callback should have removed it
        assert "t1" not in runner.in_flight

        await runner.shutdown(timeout=1.0)

    @pytest.mark.asyncio
    async def test_deferred_queue_drains(self, db):
        """If deferred queue exists, queued tasks launch when slots free.

        Note: if MimirRunner does not implement an internal deferred queue
        (it currently returns verification_deferred and relies on the pipeline
        to re-send), this test verifies that behavior -- a deferred task can
        be re-submitted once a slot frees.
        """
        runner = MimirRunner(db=db, max_concurrent=1)
        await _seed_task(db, task_id="t1")
        await _seed_task(db, task_id="t2", output_text="Implemented feature Y. Good output here.")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            mock_v.return_value = {"verdict": "passed", "confidence": 0.9, "feedback": ""}
            mock_k.return_value = []

            # First task fills the slot
            emits1 = await runner.handle_verify(_completed_event("t1"))
            assert any(e.event_type == "verification_started" for e in emits1)

            # Second task gets deferred
            emits2 = await runner.handle_verify(_completed_event("t2"))
            assert any(e.event_type == "verification_deferred" for e in emits2)

            # Wait for first to complete, freeing the slot
            await asyncio.sleep(0.3)

            # Re-submit deferred task -- should now launch
            emits3 = await runner.handle_verify(_completed_event("t2"))

        assert emits3 is not None
        assert any(e.event_type == "verification_started" for e in emits3)

        await runner.shutdown(timeout=2.0)


# ===========================================================================
# 7. EDGE CASES
# ===========================================================================

class TestMimirRunnerEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_task_id(self, db):
        """Event without task_id returns mimir_error."""
        runner = MimirRunner(db=db, max_concurrent=4)

        event = Event("worker_event", {
            "project_id": "proj-1",
            "status": "completed",
        }, "hermes")

        emits = await runner.handle_verify(event)

        assert emits is not None
        error = next((e for e in emits if e.event_type == "mimir_error"), None)
        assert error is not None

    @pytest.mark.asyncio
    async def test_non_completed_status_returns_none(self, db):
        """Events with status != 'completed' return None."""
        runner = MimirRunner(db=db, max_concurrent=4)

        event = Event("worker_event", {
            "task_id": "t1",
            "project_id": "proj-1",
            "status": "failed",
        }, "hermes")

        emits = await runner.handle_verify(event)
        assert emits is None

    @pytest.mark.asyncio
    async def test_task_not_found_returns_error(self, db):
        """Dispatch for nonexistent task returns mimir_error."""
        runner = MimirRunner(db=db, max_concurrent=4)

        emits = await runner.handle_verify(_completed_event("ghost"))

        assert emits is not None
        error = next((e for e in emits if e.event_type == "mimir_error"), None)
        assert error is not None
        assert "not found" in error.payload["error"].lower()

    @pytest.mark.asyncio
    async def test_knowledge_extraction_failure_non_blocking(self, db):
        """Knowledge extraction failure should not block verification."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db)

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            mock_v.return_value = {"verdict": "passed", "confidence": 0.9, "feedback": ""}
            mock_k.side_effect = RuntimeError("Knowledge extraction broke")

            await runner.handle_verify(_completed_event())
            await asyncio.sleep(0.3)
            await runner.shutdown(timeout=2.0)

        # Should still write task_verified despite knowledge extraction failure
        rows = await db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = ?",
            ("task_verified",))
        assert len(rows) >= 1

    @pytest.mark.asyncio
    async def test_error_pattern_output_triggers_heuristic(self, db):
        """Output that is mostly error messages triggers heuristic failure."""
        runner = MimirRunner(db=db, max_concurrent=4)
        await _seed_task(db,
                         output_text="Error: command not found\nTraceback: something failed",
                         retry_count=0, max_retries=3)

        emits = await runner.handle_verify(_completed_event())

        assert emits is not None
        rejected = next((e for e in emits if e.event_type == "task_rejected"), None)
        assert rejected is not None
