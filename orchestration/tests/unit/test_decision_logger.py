#  Decision Logger — Unit Tests
#
#  Tests for DecisionLogger: log_decision, query_decisions,
#  query_similar_decisions, _row_to_record.

from __future__ import annotations

import json
import time

import pytest

from tests.conftest import create_test_project

pytestmark = pytest.mark.anyio

from backend.services.sentinel.decision_logger import DecisionLogger
from backend.services.sentinel.models import DecisionRecord, SentinelCommand


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _insert_decision(db, decision_id, project_id, command, reasoning, confidence, outcome=None, details=None, ts=None):
    """Insert a decision row directly for query tests."""
    await db.execute_write(
        """INSERT INTO sentinel_decisions
           (id, project_id, timestamp, command, reasoning, confidence, outcome, details_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            decision_id,
            project_id,
            ts or time.time(),
            command,
            reasoning,
            confidence,
            outcome,
            json.dumps(details) if details else None,
        ),
    )


# ===========================================================================
# log_decision
# ===========================================================================

class TestLogDecision:
    async def test_returns_decision_id(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        logger = DecisionLogger(tmp_db)
        decision_id = await logger.log_decision(
            project_id="proj1",
            command="retry_task",
            reasoning="Task stuck for 10 minutes",
            confidence=0.85,
        )
        assert isinstance(decision_id, str)
        assert len(decision_id) == 32  # uuid4 hex

    async def test_persists_to_database(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        logger = DecisionLogger(tmp_db)
        decision_id = await logger.log_decision(
            project_id="proj1",
            command="skip_task",
            reasoning="Cascade failure detected",
            confidence=0.72,
            outcome="skipped",
            details={"task_id": "t5", "wave": 2},
        )
        row = await tmp_db.fetchone(
            "SELECT * FROM sentinel_decisions WHERE id = ?", (decision_id,)
        )
        assert row is not None
        assert row["project_id"] == "proj1"
        assert row["command"] == "skip_task"
        assert row["reasoning"] == "Cascade failure detected"
        assert row["confidence"] == 0.72
        assert row["outcome"] == "skipped"
        details = json.loads(row["details_json"])
        assert details["task_id"] == "t5"

    async def test_details_none_stored_as_null(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        logger = DecisionLogger(tmp_db)
        decision_id = await logger.log_decision(
            project_id="proj1",
            command="retry_task",
            reasoning="test",
            confidence=0.5,
        )
        row = await tmp_db.fetchone(
            "SELECT details_json FROM sentinel_decisions WHERE id = ?", (decision_id,)
        )
        assert row["details_json"] is None

    async def test_graceful_on_db_failure(self, tmp_db):
        """log_decision should not raise even if the DB write fails."""
        from unittest.mock import AsyncMock
        logger = DecisionLogger(tmp_db)
        logger._db = AsyncMock()
        logger._db.execute_write = AsyncMock(side_effect=RuntimeError("boom"))
        # Should return a decision_id without raising
        decision_id = await logger.log_decision(
            project_id="proj1",
            command="retry_task",
            reasoning="test",
            confidence=0.5,
        )
        assert isinstance(decision_id, str)


# ===========================================================================
# query_decisions
# ===========================================================================

class TestQueryDecisions:
    async def test_returns_records_for_project(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        await _insert_decision(tmp_db, "d1", "proj1", "retry_task", "reason1", 0.8)
        await _insert_decision(tmp_db, "d2", "proj1", "skip_task", "reason2", 0.6)

        logger = DecisionLogger(tmp_db)
        records = await logger.query_decisions("proj1")
        assert len(records) == 2
        assert all(isinstance(r, DecisionRecord) for r in records)

    async def test_filters_by_project(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        await create_test_project(tmp_db, "proj2")
        await _insert_decision(tmp_db, "d1", "proj1", "retry_task", "r1", 0.8)
        await _insert_decision(tmp_db, "d2", "proj2", "retry_task", "r2", 0.7)

        logger = DecisionLogger(tmp_db)
        records = await logger.query_decisions("proj1")
        assert len(records) == 1
        assert records[0].project_id == "proj1"

    async def test_filters_by_command(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        await _insert_decision(tmp_db, "d1", "proj1", "retry_task", "r1", 0.8)
        await _insert_decision(tmp_db, "d2", "proj1", "skip_task", "r2", 0.6)

        logger = DecisionLogger(tmp_db)
        records = await logger.query_decisions("proj1", command="skip_task")
        assert len(records) == 1
        assert records[0].command == SentinelCommand.SKIP_TASK

    async def test_respects_limit(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        for i in range(10):
            await _insert_decision(tmp_db, f"d{i}", "proj1", "retry_task", f"r{i}", 0.5, ts=time.time() + i)

        logger = DecisionLogger(tmp_db)
        records = await logger.query_decisions("proj1", limit=3)
        assert len(records) == 3

    async def test_ordered_newest_first(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        base = time.time()
        await _insert_decision(tmp_db, "old", "proj1", "retry_task", "old", 0.5, ts=base)
        await _insert_decision(tmp_db, "new", "proj1", "retry_task", "new", 0.5, ts=base + 100)

        logger = DecisionLogger(tmp_db)
        records = await logger.query_decisions("proj1")
        assert records[0].id == "new"
        assert records[1].id == "old"

    async def test_empty_for_unknown_project(self, tmp_db):
        logger = DecisionLogger(tmp_db)
        records = await logger.query_decisions("nonexistent")
        assert records == []

    async def test_graceful_on_db_failure(self, tmp_db):
        from unittest.mock import AsyncMock
        logger = DecisionLogger(tmp_db)
        logger._db = AsyncMock()
        logger._db.fetchall = AsyncMock(side_effect=RuntimeError("boom"))
        records = await logger.query_decisions("proj1")
        assert records == []


# ===========================================================================
# query_similar_decisions
# ===========================================================================

class TestQuerySimilarDecisions:
    async def test_returns_all_projects_for_command(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        await create_test_project(tmp_db, "proj2")
        await _insert_decision(tmp_db, "d1", "proj1", "retry_task", "r1", 0.8)
        await _insert_decision(tmp_db, "d2", "proj2", "retry_task", "r2", 0.7)
        await _insert_decision(tmp_db, "d3", "proj1", "skip_task", "r3", 0.6)

        logger = DecisionLogger(tmp_db)
        records = await logger.query_similar_decisions("retry_task")
        assert len(records) == 2
        assert all(r.command == SentinelCommand.RETRY_TASK for r in records)

    async def test_respects_limit(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        for i in range(10):
            await _insert_decision(tmp_db, f"d{i}", "proj1", "skip_task", f"r{i}", 0.5, ts=time.time() + i)

        logger = DecisionLogger(tmp_db)
        records = await logger.query_similar_decisions("skip_task", limit=5)
        assert len(records) == 5

    async def test_graceful_on_db_failure(self, tmp_db):
        from unittest.mock import AsyncMock
        logger = DecisionLogger(tmp_db)
        logger._db = AsyncMock()
        logger._db.fetchall = AsyncMock(side_effect=RuntimeError("boom"))
        records = await logger.query_similar_decisions("retry_task")
        assert records == []


# ===========================================================================
# _row_to_record
# ===========================================================================

class TestRowToRecord:
    async def test_parses_valid_row(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        ts = time.time()
        await _insert_decision(
            tmp_db, "d1", "proj1", "retry_task", "5-whys chain", 0.9,
            outcome="retried", details={"depth": 3}, ts=ts,
        )
        rows = await tmp_db.fetchall(
            "SELECT id, project_id, timestamp, command, reasoning, confidence, outcome, details_json "
            "FROM sentinel_decisions WHERE id = 'd1'"
        )
        record = DecisionLogger._row_to_record(rows[0])
        assert record.id == "d1"
        assert record.project_id == "proj1"
        assert record.command == SentinelCommand.RETRY_TASK
        assert record.reasoning == "5-whys chain"
        assert record.confidence == 0.9
        assert record.outcome == "retried"
        assert record.details == {"depth": 3}  # type: ignore[attr-defined]

    async def test_unknown_command_falls_back(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        await _insert_decision(tmp_db, "d1", "proj1", "unknown_future_cmd", "test", 0.5)
        rows = await tmp_db.fetchall(
            "SELECT id, project_id, timestamp, command, reasoning, confidence, outcome, details_json "
            "FROM sentinel_decisions WHERE id = 'd1'"
        )
        record = DecisionLogger._row_to_record(rows[0])
        assert record.command == SentinelCommand.DISPATCH_TASK  # fallback

    async def test_null_details_yields_empty_dict(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        await _insert_decision(tmp_db, "d1", "proj1", "retry_task", "test", 0.5)
        rows = await tmp_db.fetchall(
            "SELECT id, project_id, timestamp, command, reasoning, confidence, outcome, details_json "
            "FROM sentinel_decisions WHERE id = 'd1'"
        )
        record = DecisionLogger._row_to_record(rows[0])
        assert record.details == {}  # type: ignore[attr-defined]

    async def test_null_outcome_yields_empty_string(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        await _insert_decision(tmp_db, "d1", "proj1", "retry_task", "test", 0.5)
        rows = await tmp_db.fetchall(
            "SELECT id, project_id, timestamp, command, reasoning, confidence, outcome, details_json "
            "FROM sentinel_decisions WHERE id = 'd1'"
        )
        record = DecisionLogger._row_to_record(rows[0])
        assert record.outcome == ""

    async def test_timestamp_is_utc(self, tmp_db):
        from datetime import timezone
        await create_test_project(tmp_db, "proj1")
        await _insert_decision(tmp_db, "d1", "proj1", "retry_task", "test", 0.5)
        rows = await tmp_db.fetchall(
            "SELECT id, project_id, timestamp, command, reasoning, confidence, outcome, details_json "
            "FROM sentinel_decisions WHERE id = 'd1'"
        )
        record = DecisionLogger._row_to_record(rows[0])
        assert record.timestamp.tzinfo == timezone.utc
