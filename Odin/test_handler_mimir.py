"""Tests for gods/handlers/mimir.py — verification, review, knowledge extraction.

Mimir receives worker_event:completed and:
  1. Runs output verification (does output match task requirements?)
  2. Runs code review (is the code quality acceptable?)
  3. Runs TDD check (were tests written and do they pass?)
  4. Extracts knowledge from successful completions
  5. Emits task_verified or task_rejected
"""

import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from gods.pipeline import Event, Emit

from gods.handlers.mimir import (
    mimir_verify,
    mimir_review,
    _check_output_quality,
    _extract_knowledge,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def verify_db(sqlite_db):
    """SQLite with task schema for mimir tests."""
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT DEFAULT 'executing',
            config_json TEXT DEFAULT '{}',
            updated_at REAL
        )
    """)
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            title TEXT,
            description TEXT DEFAULT '',
            task_type TEXT DEFAULT 'code',
            complexity TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'completed',
            model_tier TEXT DEFAULT 'claude_code',
            output_text TEXT,
            error TEXT,
            context_json TEXT DEFAULT '{}',
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            verification_status TEXT,
            verification_notes TEXT,
            updated_at REAL
        )
    """)
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS project_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            task_id TEXT,
            content TEXT,
            created_at REAL
        )
    """)
    return sqlite_db


async def _seed_task(db, task_id="t1", project_id="proj-1", status="completed",
                     output_text="Implemented feature X.", description="Build feature X",
                     task_type="code"):
    await db.execute_write(
        "INSERT OR REPLACE INTO projects (id, name, status, updated_at) VALUES (?, ?, ?, ?)",
        (project_id, "Test", "executing", time.time()),
    )
    await db.execute_write(
        "INSERT OR REPLACE INTO tasks (id, project_id, title, description, task_type, "
        "status, output_text, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, project_id, f"Task {task_id}", description, task_type,
         status, output_text, time.time()),
    )


# ---------------------------------------------------------------------------
# mimir_verify — worker_event:completed → task_verified / task_rejected
# ---------------------------------------------------------------------------

class TestMimirVerify:
    @pytest.mark.asyncio
    async def test_good_output_emits_task_verified(self, verify_db):
        """Completed task with good output → task_verified."""
        await _seed_task(verify_db, output_text="Implemented feature X. All tests pass.")
        event = Event("worker_event", {
            "task_id": "t1",
            "project_id": "proj-1",
            "status": "completed",
        }, "hermes")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = {
                "verdict": "passed",
                "confidence": 0.95,
                "feedback": "",
            }
            emits = await mimir_verify(event, verify_db)

        verified = next((e for e in emits if e.event_type == "task_verified"), None)
        assert verified is not None
        assert verified.payload["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_bad_output_emits_task_rejected(self, verify_db):
        """Completed task with gaps → task_rejected."""
        await _seed_task(verify_db, output_text="Partial implementation.")
        event = Event("worker_event", {
            "task_id": "t1",
            "project_id": "proj-1",
            "status": "completed",
        }, "hermes")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = {
                "verdict": "gaps_found",
                "confidence": 0.7,
                "feedback": "Missing error handling for edge cases.",
            }
            emits = await mimir_verify(event, verify_db)

        rejected = next((e for e in emits if e.event_type == "task_rejected"), None)
        assert rejected is not None
        assert "error handling" in rejected.payload["feedback"]

    @pytest.mark.asyncio
    async def test_rejected_task_reset_to_pending(self, verify_db):
        """task_rejected should reset task to pending with feedback in context."""
        await _seed_task(verify_db)
        event = Event("worker_event", {
            "task_id": "t1",
            "project_id": "proj-1",
            "status": "completed",
        }, "hermes")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = {
                "verdict": "gaps_found",
                "confidence": 0.6,
                "feedback": "No tests written.",
            }
            emits = await mimir_verify(event, verify_db)

        row = await verify_db.fetchone(
            "SELECT status, retry_count, context_json FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "pending"
        assert row["retry_count"] == 1
        assert "No tests written" in (row["context_json"] or "")

    @pytest.mark.asyncio
    async def test_skips_failed_worker_events(self, verify_db):
        """worker_event with status=failed should not be verified."""
        await _seed_task(verify_db, status="failed")
        event = Event("worker_event", {
            "task_id": "t1",
            "project_id": "proj-1",
            "status": "failed",
        }, "hermes")

        emits = await mimir_verify(event, verify_db)

        # Should pass through without verification
        assert emits is None or len(emits) == 0

    @pytest.mark.asyncio
    async def test_escalates_after_max_verification_retries(self, verify_db):
        """After max retries, verification failure → needs_human_review."""
        await _seed_task(verify_db)
        await verify_db.execute_write(
            "UPDATE tasks SET retry_count = 3, max_retries = 3 WHERE id = ?", ("t1",))

        event = Event("worker_event", {
            "task_id": "t1",
            "project_id": "proj-1",
            "status": "completed",
        }, "hermes")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = {
                "verdict": "gaps_found",
                "confidence": 0.5,
                "feedback": "Still has gaps.",
            }
            emits = await mimir_verify(event, verify_db)

        human = next((e for e in emits if e.event_type == "needs_human_review"), None)
        assert human is not None

        row = await verify_db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "needs_review"


# ---------------------------------------------------------------------------
# mimir_review — code quality review
# ---------------------------------------------------------------------------

class TestMimirReview:
    @pytest.mark.asyncio
    async def test_approved_emits_review_passed(self, verify_db):
        await _seed_task(verify_db, output_text="Clean implementation with tests.")
        event = Event("task_verified", {
            "task_id": "t1",
            "project_id": "proj-1",
        }, "mimir")

        with patch("gods.handlers.mimir._call_reviewer", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = {
                "verdict": "approved",
                "feedback": "LGTM",
            }
            emits = await mimir_review(event, verify_db)

        passed = next((e for e in emits if e.event_type == "review_passed"), None)
        assert passed is not None

    @pytest.mark.asyncio
    async def test_changes_requested_emits_review_rejected(self, verify_db):
        await _seed_task(verify_db, output_text="Hacky implementation.")
        event = Event("task_verified", {
            "task_id": "t1",
            "project_id": "proj-1",
        }, "mimir")

        with patch("gods.handlers.mimir._call_reviewer", new_callable=AsyncMock) as mock_r:
            mock_r.return_value = {
                "verdict": "changes_requested",
                "feedback": "Use dependency injection instead of globals.",
            }
            emits = await mimir_review(event, verify_db)

        # Review is advisory — always emits review_passed, verdict in payload
        passed = next((e for e in emits if e.event_type == "review_passed"), None)
        assert passed is not None
        assert passed.payload["verdict"] == "changes_requested"
        assert "dependency injection" in passed.payload["feedback"]


# ---------------------------------------------------------------------------
# Output quality check (heuristic, no LLM)
# ---------------------------------------------------------------------------

class TestOutputQualityCheck:
    def test_empty_output_fails(self):
        result = _check_output_quality("")
        assert result["passed"] is False

    def test_none_output_fails(self):
        result = _check_output_quality(None)
        assert result["passed"] is False

    def test_normal_output_passes(self):
        result = _check_output_quality("Implemented the feature. Added tests. All passing.")
        assert result["passed"] is True

    def test_error_only_output_fails(self):
        result = _check_output_quality("Error: command not found\nTraceback...")
        assert result["passed"] is False

    def test_very_short_output_warns(self):
        result = _check_output_quality("ok")
        assert result["passed"] is False or result.get("warning")


# ---------------------------------------------------------------------------
# Knowledge extraction
# ---------------------------------------------------------------------------

class TestKnowledgeExtraction:
    @pytest.mark.asyncio
    async def test_extracts_and_stores_knowledge(self, verify_db):
        await _seed_task(verify_db,
                         output_text="Discovered that the API requires auth header X-Api-Key. "
                                     "The rate limit is 100 req/min.")

        with patch("gods.handlers.mimir._call_knowledge_extractor", new_callable=AsyncMock) as mock_k:
            mock_k.return_value = [
                "API requires X-Api-Key header",
                "Rate limit: 100 req/min",
            ]
            findings = await _extract_knowledge(
                task_id="t1",
                project_id="proj-1",
                output_text="Discovered that the API requires auth header...",
                db=verify_db,
            )

        assert len(findings) == 2

        rows = await verify_db.fetchall(
            "SELECT content FROM project_knowledge WHERE project_id = ?", ("proj-1",))
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_skips_extraction_on_empty_output(self, verify_db):
        findings = await _extract_knowledge(
            task_id="t1",
            project_id="proj-1",
            output_text="",
            db=verify_db,
        )
        assert findings == []
