#  Orchestration Engine - Progress Manager Unit Tests
#
#  Tests for event persistence and broadcast.
#
#  Depends on: backend/services/progress.py, tests/conftest.py
#  Used by:    pytest

import asyncio
import json
import time

import pytest

from backend.services.progress import ProgressManager
from tests.conftest import create_test_project, create_test_task


class TestPushEvent:
    async def test_push_event_persists_to_db(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "t1", "proj1")
        pm = ProgressManager(db=tmp_db)
        await pm.push_event("proj1", "task_started", message="Starting task A", task_id="t1")

        rows = await tmp_db.fetchall(
            "SELECT * FROM task_events WHERE project_id = 'proj1'"
        )
        assert len(rows) == 1
        assert rows[0]["event_type"] == "task_started"
        assert rows[0]["message"] == "Starting task A"
        assert rows[0]["task_id"] == "t1"

    async def test_push_event_with_extra_data(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        pm = ProgressManager(db=tmp_db)
        await pm.push_event("proj1", "task_progress", progress=42, detail="halfway")

        rows = await tmp_db.fetchall(
            "SELECT data_json FROM task_events WHERE project_id = 'proj1'"
        )
        data = json.loads(rows[0]["data_json"])
        assert data["progress"] == 42
        assert data["detail"] == "halfway"

    async def test_push_event_broadcasts_to_subscribers(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        pm = ProgressManager(db=tmp_db)
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        pm._subscribers.setdefault("proj1", []).append(queue)

        await pm.push_event("proj1", "task_completed", message="Done")

        event = queue.get_nowait()
        assert event["type"] == "task_completed"
        assert event["message"] == "Done"

    async def test_push_event_no_subscribers_ok(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        pm = ProgressManager(db=tmp_db)
        # Should not raise even with no subscribers
        await pm.push_event("proj1", "task_started")

    async def test_push_event_drops_when_queue_full(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        pm = ProgressManager(db=tmp_db)
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        queue.put_nowait({"type": "filler"})  # fill it
        pm._subscribers.setdefault("proj1", []).append(queue)

        # Should not raise even though queue is full
        await pm.push_event("proj1", "task_started")
        assert queue.qsize() == 1  # unchanged — event was dropped


class TestGetEvents:
    async def test_get_events_returns_persisted(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "t1", "proj1")
        pm = ProgressManager(db=tmp_db)
        await pm.push_event("proj1", "task_started", task_id="t1")
        await pm.push_event("proj1", "task_completed", task_id="t1")

        events = await pm.get_events("proj1")
        assert len(events) == 2
        assert events[0]["event_type"] == "task_started"
        assert events[1]["event_type"] == "task_completed"

    async def test_get_events_filter_by_task_id(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "t1", "proj1")
        await create_test_task(tmp_db, "t2", "proj1")
        pm = ProgressManager(db=tmp_db)
        await pm.push_event("proj1", "task_started", task_id="t1")
        await pm.push_event("proj1", "task_started", task_id="t2")

        events = await pm.get_events("proj1", task_id="t1")
        assert len(events) == 1
        assert events[0]["task_id"] == "t1"

    async def test_get_events_respects_limit(self, tmp_db):
        await create_test_project(tmp_db, "proj1")
        pm = ProgressManager(db=tmp_db)
        for i in range(5):
            await pm.push_event("proj1", f"event_{i}")

        events = await pm.get_events("proj1", limit=3)
        assert len(events) == 3

    async def test_get_events_empty_project(self, tmp_db):
        pm = ProgressManager(db=tmp_db)
        events = await pm.get_events("no_such_project")
        assert events == []


class TestForeignKeyEnforcement:
    """Verify FK constraints on task_events behave correctly."""

    async def test_orphan_project_id_rejected(self, tmp_db):
        """Inserting a task_event with nonexistent project_id raises IntegrityError."""
        import aiosqlite

        with pytest.raises(aiosqlite.IntegrityError):
            await tmp_db.execute_write(
                "INSERT INTO task_events (id, project_id, event_type, timestamp) "
                "VALUES (?, ?, ?, ?)",
                ("evt_orphan", "nonexistent_project", "test", time.time()),
            )

    async def test_cascade_delete_project_removes_events(self, tmp_db):
        """Deleting a project cascades to its task_events."""
        await create_test_project(tmp_db, "proj_cascade")
        pm = ProgressManager(db=tmp_db)
        await pm.push_event("proj_cascade", "task_start", message="test")

        # Verify event exists
        events = await tmp_db.fetchall(
            "SELECT * FROM task_events WHERE project_id = ?", ("proj_cascade",)
        )
        assert len(events) == 1

        # Delete project — should cascade
        await tmp_db.execute_write("DELETE FROM projects WHERE id = ?", ("proj_cascade",))

        events = await tmp_db.fetchall(
            "SELECT * FROM task_events WHERE project_id = ?", ("proj_cascade",)
        )
        assert len(events) == 0

    async def test_delete_task_sets_null_on_events(self, tmp_db):
        """Deleting a task sets task_id to NULL on its task_events (ON DELETE SET NULL)."""
        await create_test_project(tmp_db, "proj_setnull")
        await create_test_task(tmp_db, "task_setnull", "proj_setnull")
        pm = ProgressManager(db=tmp_db)
        await pm.push_event("proj_setnull", "task_start", task_id="task_setnull")

        # Verify event has task_id set
        event = await tmp_db.fetchone(
            "SELECT task_id FROM task_events WHERE project_id = ?", ("proj_setnull",)
        )
        assert event["task_id"] == "task_setnull"

        # Delete the task — should set task_id to NULL
        await tmp_db.execute_write("DELETE FROM tasks WHERE id = ?", ("task_setnull",))

        event = await tmp_db.fetchone(
            "SELECT task_id FROM task_events WHERE project_id = ?", ("proj_setnull",)
        )
        assert event["task_id"] is None
