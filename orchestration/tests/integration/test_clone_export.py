#  Orchestration Engine - Clone & Export API Tests
#
#  Tests for POST /{id}/clone and GET /{id}/export endpoints.
#
#  Depends on: backend/routes/projects.py, tests/conftest.py
#  Used by:    pytest

import json
import time


async def _seed_project(client, db):
    """Create a project with a plan and tasks."""
    resp = await client.post("/api/projects", json={
        "name": "Clone Test", "requirements": "Req A\nReq B",
    })
    project_id = resp.json()["id"]
    now = time.time()

    plan_id = "plan_clone"
    await db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test-model', ?, 'approved', ?)",
        (plan_id, project_id, json.dumps({"summary": "test", "tasks": []}), now),
    )

    task_ids = []
    for i, (title, status) in enumerate([("Task 1", "completed"), ("Task 2", "pending")]):
        tid = f"ct_{i}"
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, output_text, cost_usd, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'code', ?, ?, 'haiku', 0, ?, ?, ?, ?)",
            (tid, project_id, plan_id, title, f"Do {title}", i,
             status, f"output_{i}" if status == "completed" else None,
             0.01 if status == "completed" else 0.0, now, now),
        )
        task_ids.append(tid)

    # Add a dependency: Task 2 depends on Task 1
    await db.execute_write(
        "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
        (task_ids[1], task_ids[0]),
    )

    return project_id, plan_id, task_ids


class TestCloneProject:
    async def test_clone_creates_new_project(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid, _, _ = await _seed_project(authed_client, db)
        resp = await authed_client.post(f"/api/projects/{pid}/clone")
        assert resp.status_code == 201
        data = resp.json()
        assert data["id"] != pid
        assert data["name"] == "Clone Test (clone)"
        assert data["status"] == "draft"

    async def test_clone_copies_plan(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid, _, _ = await _seed_project(authed_client, db)
        resp = await authed_client.post(f"/api/projects/{pid}/clone")
        new_id = resp.json()["id"]

        plans = await db.fetchall(
            "SELECT * FROM plans WHERE project_id = ?", (new_id,)
        )
        assert len(plans) == 1
        assert plans[0]["status"] == "draft"
        assert plans[0]["version"] == 1

    async def test_clone_resets_task_status(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid, _, _ = await _seed_project(authed_client, db)
        resp = await authed_client.post(f"/api/projects/{pid}/clone")
        new_id = resp.json()["id"]

        tasks = await db.fetchall(
            "SELECT * FROM tasks WHERE project_id = ?", (new_id,)
        )
        assert len(tasks) == 2
        for t in tasks:
            assert t["status"] == "pending"
            assert t["output_text"] is None
            assert t["cost_usd"] is None or t["cost_usd"] == 0.0

    async def test_clone_remaps_dependencies(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid, _, _ = await _seed_project(authed_client, db)
        resp = await authed_client.post(f"/api/projects/{pid}/clone")
        new_id = resp.json()["id"]

        new_tasks = await db.fetchall(
            "SELECT id FROM tasks WHERE project_id = ?", (new_id,)
        )
        new_task_ids = {t["id"] for t in new_tasks}

        deps = await db.fetchall(
            "SELECT task_id, depends_on FROM task_deps WHERE task_id IN ({})".format(
                ",".join("?" * len(new_task_ids))
            ),
            list(new_task_ids),
        )
        assert len(deps) == 1
        # Both sides of the dep must be in the new task set
        assert deps[0]["task_id"] in new_task_ids
        assert deps[0]["depends_on"] in new_task_ids

    async def test_clone_nonexistent_returns_404(self, authed_client):
        resp = await authed_client.post("/api/projects/nonexistent/clone")
        assert resp.status_code == 404

    async def test_clone_empty_project(self, authed_client, tmp_db):
        resp = await authed_client.post("/api/projects", json={
            "name": "Empty", "requirements": "Nothing",
        })
        pid = resp.json()["id"]
        resp2 = await authed_client.post(f"/api/projects/{pid}/clone")
        assert resp2.status_code == 201
        assert resp2.json()["name"] == "Empty (clone)"


class TestExportProject:
    async def test_export_returns_json(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid, _, _ = await _seed_project(authed_client, db)
        resp = await authed_client.get(f"/api/projects/{pid}/export")
        assert resp.status_code == 200
        data = resp.json()
        assert "project" in data
        assert "plans" in data
        assert "tasks" in data
        assert "events" in data
        assert "checkpoints" in data
        assert "usage" in data
        assert "exported_at" in data

    async def test_export_includes_tasks(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid, _, _ = await _seed_project(authed_client, db)
        resp = await authed_client.get(f"/api/projects/{pid}/export")
        data = resp.json()
        assert len(data["tasks"]) == 2
        assert len(data["plans"]) == 1

    async def test_export_content_disposition(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid, _, _ = await _seed_project(authed_client, db)
        resp = await authed_client.get(f"/api/projects/{pid}/export")
        assert "attachment" in resp.headers.get("content-disposition", "")

    async def test_export_nonexistent_returns_404(self, authed_client):
        resp = await authed_client.get("/api/projects/nonexistent/export")
        assert resp.status_code == 404

    async def test_export_empty_project(self, authed_client, tmp_db):
        resp = await authed_client.post("/api/projects", json={
            "name": "Empty", "requirements": "Nothing",
        })
        pid = resp.json()["id"]
        resp2 = await authed_client.get(f"/api/projects/{pid}/export")
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["tasks"] == []
        assert data["plans"] == []
