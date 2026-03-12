#  Orchestration Engine - Bulk Task Operations Tests
#
#  Tests for POST /tasks/bulk endpoint.
#
#  Depends on: backend/routes/tasks.py, tests/conftest.py
#  Used by:    pytest

import json
import time

from backend.config import MAX_TASK_RETRIES


async def _seed_bulk_tasks(client, db):
    """Create a project with tasks in various states for bulk testing."""
    resp = await client.post("/api/projects", json={
        "name": "Bulk Test", "requirements": "Test bulk ops",
    })
    project_id = resp.json()["id"]
    now = time.time()

    plan_id = "plan_bulk"
    await db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
        (plan_id, project_id, json.dumps({"summary": "test", "tasks": []}), now),
    )

    tasks = [
        ("bt_0", "pending", 0),
        ("bt_1", "failed", 0),
        ("bt_2", "failed", 0),
        ("bt_3", "completed", 0),
        ("bt_4", "blocked", 0),
    ]
    for tid, status, retry_count in tasks:
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, retry_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'desc', 'code', 50, ?, ?, ?, ?)",
            (tid, project_id, plan_id, f"Task {tid}", status, retry_count, now, now),
        )

    return project_id


class TestBulkRetry:
    async def test_retry_failed_tasks(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        await _seed_bulk_tasks(authed_client, db)

        resp = await authed_client.post("/api/tasks/bulk", json={
            "action": "retry", "task_ids": ["bt_1", "bt_2"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["succeeded"]) == {"bt_1", "bt_2"}
        assert data["failed"] == []

    async def test_retry_mixed_states(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        await _seed_bulk_tasks(authed_client, db)

        resp = await authed_client.post("/api/tasks/bulk", json={
            "action": "retry", "task_ids": ["bt_1", "bt_0"],  # bt_0 is pending
        })
        data = resp.json()
        assert "bt_1" in data["succeeded"]
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "bt_0"

    async def test_retry_max_retries_reached(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        await _seed_bulk_tasks(authed_client, db)
        await db.execute_write(
            "UPDATE tasks SET retry_count = ? WHERE id = ?",
            (MAX_TASK_RETRIES, "bt_1"),
        )

        resp = await authed_client.post("/api/tasks/bulk", json={
            "action": "retry", "task_ids": ["bt_1"],
        })
        data = resp.json()
        assert data["succeeded"] == []
        assert data["failed"][0]["reason"] == "Max retries reached"


class TestBulkCancel:
    async def test_cancel_pending_tasks(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        await _seed_bulk_tasks(authed_client, db)

        resp = await authed_client.post("/api/tasks/bulk", json={
            "action": "cancel", "task_ids": ["bt_0", "bt_4"],  # pending + blocked
        })
        data = resp.json()
        assert set(data["succeeded"]) == {"bt_0", "bt_4"}

    async def test_cancel_completed_returns_failure(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        await _seed_bulk_tasks(authed_client, db)

        resp = await authed_client.post("/api/tasks/bulk", json={
            "action": "cancel", "task_ids": ["bt_3"],
        })
        data = resp.json()
        assert data["succeeded"] == []
        assert len(data["failed"]) == 1


class TestBulkValidation:
    async def test_empty_list_returns_422(self, authed_client, tmp_db):
        resp = await authed_client.post("/api/tasks/bulk", json={
            "action": "retry", "task_ids": [],
        })
        assert resp.status_code == 422

    async def test_nonexistent_task(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        await _seed_bulk_tasks(authed_client, db)

        resp = await authed_client.post("/api/tasks/bulk", json={
            "action": "retry", "task_ids": ["nonexistent"],
        })
        data = resp.json()
        assert data["succeeded"] == []
        assert len(data["failed"]) == 1

    async def test_invalid_action_returns_422(self, authed_client, tmp_db):
        resp = await authed_client.post("/api/tasks/bulk", json={
            "action": "delete", "task_ids": ["bt_0"],
        })
        assert resp.status_code == 422
