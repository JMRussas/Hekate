#  Orchestration Engine - Pagination Integration Tests
#
#  Tests for limit/offset pagination on list endpoints.
#
#  Depends on: backend/routes/tasks.py, backend/routes/checkpoints.py,
#              backend/routes/projects.py, backend/routes/usage.py,
#              backend/routes/admin.py, tests/conftest.py
#  Used by:    pytest

import json
import time


async def _seed_many_tasks(client, db, count=10):
    """Create a project with `count` tasks."""
    resp = await client.post("/api/projects", json={
        "name": "Pagination Test", "requirements": "Test pagination",
    })
    project_id = resp.json()["id"]
    now = time.time()
    plan_id = "plan_pagination"

    await db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
        (plan_id, project_id, json.dumps({"summary": "test", "tasks": []}), now),
    )

    for i in range(count):
        tid = f"ptask_{i:03d}"
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'code', ?, 'pending', 'haiku', 0, ?, ?)",
            (tid, project_id, plan_id, f"Task {i}", f"Do task {i}", i, now + i, now + i),
        )

    return project_id


class TestTaskPagination:
    async def test_default_returns_all_within_limit(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_many_tasks(authed_client, db, count=5)
        resp = await authed_client.get(f"/api/tasks/project/{pid}")
        assert resp.status_code == 200
        assert len(resp.json()) == 5

    async def test_custom_limit(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_many_tasks(authed_client, db, count=10)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    async def test_offset_skips_rows(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_many_tasks(authed_client, db, count=5)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?limit=2&offset=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_offset_beyond_data_returns_empty(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_many_tasks(authed_client, db, count=3)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?limit=10&offset=100")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_limit_exceeds_max_returns_422(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        pid = await _seed_many_tasks(authed_client, db, count=1)
        resp = await authed_client.get(f"/api/tasks/project/{pid}?limit=501")
        assert resp.status_code == 422


class TestCheckpointPagination:
    async def test_default_pagination(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        resp = await authed_client.post("/api/projects", json={
            "name": "CP Test", "requirements": "test",
        })
        pid = resp.json()["id"]
        now = time.time()

        # Seed checkpoints
        for i in range(5):
            await db.execute_write(
                "INSERT INTO checkpoints (id, project_id, checkpoint_type, summary, "
                "question, created_at) VALUES (?, ?, 'retry_exhausted', ?, ?, ?)",
                (f"cp_{i}", pid, f"Summary {i}", f"Question {i}", now + i),
            )

        resp = await authed_client.get(f"/api/checkpoints/project/{pid}?resolved=true&limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3


class TestPlanPagination:
    async def test_plan_pagination(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        resp = await authed_client.post("/api/projects", json={
            "name": "Plan Test", "requirements": "test",
        })
        pid = resp.json()["id"]
        now = time.time()

        for i in range(5):
            await db.execute_write(
                "INSERT INTO plans (id, project_id, version, model_used, plan_json, "
                "status, created_at) VALUES (?, ?, ?, 'test', '{}', 'draft', ?)",
                (f"plan_p_{i}", pid, i + 1, now),
            )

        resp = await authed_client.get(f"/api/projects/{pid}/plans?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


class TestKnowledgePagination:
    async def test_knowledge_pagination(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        resp = await authed_client.post("/api/projects", json={
            "name": "Knowledge Test", "requirements": "test",
        })
        pid = resp.json()["id"]
        now = time.time()

        for i in range(5):
            await db.execute_write(
                "INSERT INTO project_knowledge (id, project_id, category, content, "
                "content_hash, created_at) VALUES (?, ?, 'discovery', ?, ?, ?)",
                (f"kn_{i}", pid, f"Finding {i}", f"hash_{i}", now + i),
            )

        resp = await authed_client.get(f"/api/projects/{pid}/knowledge?limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    async def test_knowledge_offset(self, authed_client, tmp_db):
        from backend.app import container
        db = container.db()
        resp = await authed_client.post("/api/projects", json={
            "name": "Knowledge Test 2", "requirements": "test",
        })
        pid = resp.json()["id"]
        now = time.time()

        for i in range(5):
            await db.execute_write(
                "INSERT INTO project_knowledge (id, project_id, category, content, "
                "content_hash, created_at) VALUES (?, ?, 'discovery', ?, ?, ?)",
                (f"kn2_{i}", pid, f"Finding {i}", f"hash2_{i}", now + i),
            )

        resp = await authed_client.get(f"/api/projects/{pid}/knowledge?limit=2&offset=4")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestExportRateLimit:
    async def test_export_rate_limited(self, authed_client, tmp_db):
        """Verify that the 6th rapid export request returns 429."""
        resp = await authed_client.post("/api/projects", json={
            "name": "Export RL Test", "requirements": "test",
        })
        pid = resp.json()["id"]

        for i in range(5):
            resp = await authed_client.get(f"/api/projects/{pid}/export")
            assert resp.status_code == 200, f"Request {i+1} should succeed"

        resp = await authed_client.get(f"/api/projects/{pid}/export")
        assert resp.status_code == 429
