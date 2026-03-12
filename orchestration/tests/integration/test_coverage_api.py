#  Orchestration Engine - Coverage Endpoint Tests
#
#  Tests for the GET /{project_id}/coverage endpoint.
#
#  Depends on: backend/routes/projects.py
#  Used by:    pytest

import json
import time


from backend.models.enums import TaskStatus


class TestCoverageEndpoint:
    async def test_full_coverage(self, authed_client):
        """All requirements covered by tasks."""
        client = authed_client

        resp = await client.post("/api/projects", json={
            "name": "Coverage Full",
            "requirements": "Build auth\n\nAdd logging",
        })
        assert resp.status_code == 201
        project_id = resp.json()["id"]

        # Insert tasks with requirement_ids directly
        from backend.app import container
        db = container.db()
        now = time.time()
        plan_id = "plan_cov_001"

        await db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'approved', ?)",
            (plan_id, project_id, json.dumps({"summary": "test", "tasks": []}), now),
        )
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, requirement_ids_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task_cov_001", project_id, plan_id, "Auth", "Do auth",
             "code", 0, TaskStatus.PENDING, "haiku", 0, json.dumps(["R1", "R2"]), now, now),
        )

        resp = await client.get(f"/api/projects/{project_id}/coverage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requirements"] == 2
        assert data["covered_count"] == 2
        assert data["uncovered_count"] == 0
        assert all(r["covered"] for r in data["requirements"])

    async def test_partial_coverage(self, authed_client):
        """Some requirements uncovered."""
        client = authed_client

        resp = await client.post("/api/projects", json={
            "name": "Coverage Partial",
            "requirements": "Build auth\n\nAdd logging\n\nWrite tests",
        })
        assert resp.status_code == 201
        project_id = resp.json()["id"]

        from backend.app import container
        db = container.db()
        now = time.time()
        plan_id = "plan_cov_002"

        await db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'approved', ?)",
            (plan_id, project_id, json.dumps({"summary": "test", "tasks": []}), now),
        )
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, requirement_ids_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task_cov_002", project_id, plan_id, "Auth", "Do auth",
             "code", 0, TaskStatus.PENDING, "haiku", 0, json.dumps(["R1"]), now, now),
        )

        resp = await client.get(f"/api/projects/{project_id}/coverage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requirements"] == 3
        assert data["covered_count"] == 1
        assert data["uncovered_count"] == 2
        assert data["requirements"][0]["covered"] is True
        assert data["requirements"][1]["covered"] is False
        assert data["requirements"][2]["covered"] is False

    async def test_no_tasks_means_zero_coverage(self, authed_client):
        """Project with requirements but no tasks has zero coverage."""
        client = authed_client

        resp = await client.post("/api/projects", json={
            "name": "Coverage Zero",
            "requirements": "Build something\n\nTest it",
        })
        assert resp.status_code == 201
        project_id = resp.json()["id"]

        resp = await client.get(f"/api/projects/{project_id}/coverage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requirements"] == 2
        assert data["covered_count"] == 0
        assert data["uncovered_count"] == 2
