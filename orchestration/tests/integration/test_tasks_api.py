#  Orchestration Engine - Tasks API Integration Tests
#
#  Task listing, detail, update, retry, and cancel via HTTP.
#
#  Depends on: backend/routes/tasks.py, tests/conftest.py
#  Used by:    pytest

import json
import time



async def _seed_project_with_tasks(client, db):
    """Create a project with decomposed tasks for testing."""
    # Create project
    resp = await client.post("/api/projects", json={
        "name": "Task Test", "requirements": "Test tasks",
    })
    project_id = resp.json()["id"]

    # Seed a plan + tasks directly via DB (faster than mocking Claude)
    now = time.time()
    plan_id = "plan_task_test"
    await db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
        (plan_id, project_id, json.dumps({"summary": "test", "tasks": []}), now),
    )

    task_ids = []
    for i, (title, status) in enumerate([
        ("Task A", "pending"),
        ("Task B", "failed"),
        ("Task C", "completed"),
    ]):
        tid = f"task_{i}"
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'code', ?, ?, ?, ?)",
            (tid, project_id, plan_id, title, f"Do {title}", 50 + i, status, now, now),
        )
        task_ids.append(tid)

    return project_id, task_ids


class TestListTasks:
    async def test_list_returns_tasks(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        project_id, task_ids = await _seed_project_with_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/project/{project_id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    async def test_list_filter_by_status(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        project_id, _ = await _seed_project_with_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/project/{project_id}?status=pending")
        assert len(resp.json()) == 1
        assert resp.json()[0]["title"] == "Task A"


class TestGetTask:
    async def test_get_existing(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        _, task_ids = await _seed_project_with_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/{task_ids[0]}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Task A"

    async def test_get_nonexistent_returns_404(self, authed_client):
        resp = await authed_client.get("/api/tasks/nope")
        assert resp.status_code == 404


class TestRetryTask:
    async def test_retry_failed_task(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        _, task_ids = await _seed_project_with_tasks(authed_client, db)
        # task_1 is "failed"
        resp = await authed_client.post(f"/api/tasks/{task_ids[1]}/retry")
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"

    async def test_retry_pending_task_returns_400(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        _, task_ids = await _seed_project_with_tasks(authed_client, db)
        resp = await authed_client.post(f"/api/tasks/{task_ids[0]}/retry")
        assert resp.status_code == 400


class TestUpdateTask:
    async def test_update_description(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        _, task_ids = await _seed_project_with_tasks(authed_client, db)
        resp = await authed_client.patch(f"/api/tasks/{task_ids[0]}", json={
            "description": "Updated description",
        })
        assert resp.status_code == 200
        assert resp.json()["description"] == "Updated description"

    async def test_update_priority(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        _, task_ids = await _seed_project_with_tasks(authed_client, db)
        resp = await authed_client.patch(f"/api/tasks/{task_ids[0]}", json={
            "priority": 10,
        })
        assert resp.status_code == 200
        assert resp.json()["priority"] == 10

    async def test_update_no_fields_returns_400(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        _, task_ids = await _seed_project_with_tasks(authed_client, db)
        resp = await authed_client.patch(f"/api/tasks/{task_ids[0]}", json={})
        assert resp.status_code == 400

    async def test_update_completed_task_returns_400(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        _, task_ids = await _seed_project_with_tasks(authed_client, db)
        # task_2 is completed
        resp = await authed_client.patch(f"/api/tasks/{task_ids[2]}", json={
            "description": "Try to edit completed",
        })
        assert resp.status_code == 400

    async def test_update_nonexistent_returns_404(self, authed_client):
        resp = await authed_client.patch("/api/tasks/nope", json={
            "description": "won't work",
        })
        assert resp.status_code == 404


class TestCancelTask:
    async def test_cancel_pending_task(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        _, task_ids = await _seed_project_with_tasks(authed_client, db)
        resp = await authed_client.post(f"/api/tasks/{task_ids[0]}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    async def test_cancel_completed_task_returns_400(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        _, task_ids = await _seed_project_with_tasks(authed_client, db)
        resp = await authed_client.post(f"/api/tasks/{task_ids[2]}/cancel")
        assert resp.status_code == 400

    async def test_cancel_nonexistent_returns_404(self, authed_client):
        resp = await authed_client.post("/api/tasks/nope/cancel")
        assert resp.status_code == 404


class TestRetryTaskEdgeCases:
    async def test_retry_nonexistent_returns_404(self, authed_client):
        resp = await authed_client.post("/api/tasks/nope/retry")
        assert resp.status_code == 404

    async def test_retry_at_max_retries_returns_400(self, authed_client, tmp_db):
        from backend.config import MAX_TASK_RETRIES
        from backend.app import container
        db = container.db()
        project_id, task_ids = await _seed_project_with_tasks(authed_client, db)
        # Set retry_count to the limit on the failed task (task_ids[1])
        await db.execute_write(
            "UPDATE tasks SET retry_count = ? WHERE id = ?",
            (MAX_TASK_RETRIES, task_ids[1]),
        )
        resp = await authed_client.post(f"/api/tasks/{task_ids[1]}/retry")
        assert resp.status_code == 400
        assert "retry limit" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Task Filtering & Sorting
# ---------------------------------------------------------------------------

async def _seed_filterable_tasks(client, db):
    """Create a project with tasks having varied wave, model_tier, and output."""
    resp = await client.post("/api/projects", json={
        "name": "Filter Test", "requirements": "Test filtering",
    })
    project_id = resp.json()["id"]

    now = time.time()
    plan_id = "plan_filter_test"
    await db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
        (plan_id, project_id, json.dumps({"summary": "test", "tasks": []}), now),
    )

    tasks = [
        ("task_f0", "Alpha Research", "Research alpha", "pending", "haiku", 0, 100),
        ("task_f1", "Beta Code", "Code beta module", "completed", "sonnet", 0, 50),
        ("task_f2", "Gamma Analysis", "Analyze gamma data", "failed", "ollama", 1, 75),
        ("task_f3", "Delta Integration", "Integrate delta", "pending", "haiku", 1, 25),
    ]
    for tid, title, desc, status, model_tier, wave, priority in tasks:
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, output_text, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'code', ?, ?, ?, ?, ?, ?, ?)",
            (tid, project_id, plan_id, title, desc, priority, status, model_tier, wave,
             f"output for {title}" if status == "completed" else None, now, now),
        )

    return project_id


class TestListTasksFiltering:
    async def test_filter_by_wave(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_filterable_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?wave=0")
        assert resp.status_code == 200
        titles = {t["title"] for t in resp.json()}
        assert titles == {"Alpha Research", "Beta Code"}

    async def test_filter_by_model_tier(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_filterable_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?model_tier=haiku")
        assert resp.status_code == 200
        titles = {t["title"] for t in resp.json()}
        assert titles == {"Alpha Research", "Delta Integration"}

    async def test_search_by_title(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_filterable_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?search=Beta")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["title"] == "Beta Code"

    async def test_search_by_description(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_filterable_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?search=gamma")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["title"] == "Gamma Analysis"

    async def test_sort_by_wave_desc(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_filterable_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?sort=wave&sort_dir=desc")
        assert resp.status_code == 200
        waves = [t["wave"] for t in resp.json()]
        assert waves == [1, 1, 0, 0]

    async def test_sort_by_created_at(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_filterable_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?sort=created_at")
        assert resp.status_code == 200
        assert len(resp.json()) == 4

    async def test_exclude_output(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_filterable_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?exclude_output=true")
        assert resp.status_code == 200
        for t in resp.json():
            assert t["output_text"] is None

    async def test_invalid_sort_dir_returns_422(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_filterable_tasks(authed_client, db)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?sort_dir=invalid")
        assert resp.status_code == 422

    async def test_combined_filters(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_filterable_tasks(authed_client, db)
        resp = await authed_client.get(
            f"/api/tasks/project/{pid}?status=pending&wave=1&sort=priority"
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["title"] == "Delta Integration"
