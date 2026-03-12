#  Orchestration Engine - Auth Gap Tests
#
#  Tests for registration disabled (403), SSE token scope, and
#  cancelling a project with running tasks.
#
#  Depends on: backend/routes/auth.py, backend/services/auth.py
#  Used by:    pytest

import json
import time
from unittest.mock import patch



# ---------------------------------------------------------------------------
# TestRegistrationDisabled
# ---------------------------------------------------------------------------

class TestRegistrationDisabled:

    async def test_register_returns_403_when_disabled(self, app_client):
        """Registration endpoint returns 403 when allow_registration is False."""
        with patch("backend.services.auth.AUTH_ALLOW_REGISTRATION", False):
            resp = await app_client.post("/api/auth/register", json={
                "email": "new@example.com",
                "password": "testpass123",
                "display_name": "New User",
            })
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# TestSSETokenScope
# ---------------------------------------------------------------------------

class TestSSETokenScope:

    async def test_sse_token_rejected_for_wrong_project(self, authed_client, tmp_db):
        """SSE token scoped to project A is rejected when used on project B."""
        now = time.time()

        # Create two projects owned by the test user
        user = await tmp_db.fetchone("SELECT id FROM users LIMIT 1")
        user_id = user["id"]

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, owner_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 'draft', ?, ?, ?)",
            ("proj_sse_a", "Project A", "req", user_id, now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, owner_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 'draft', ?, ?, ?)",
            ("proj_sse_b", "Project B", "req", user_id, now, now),
        )

        # Get SSE token for project A
        resp = await authed_client.post("/api/events/proj_sse_a/token")
        assert resp.status_code == 200
        token = resp.json()["token"]

        # Try to use that token on project B's stream
        resp = await authed_client.get(f"/api/events/proj_sse_b?token={token}")
        # Should be rejected (403 or 401)
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# TestCancelRunningProject
# ---------------------------------------------------------------------------

class TestCancelRunningProject:

    async def test_cancel_with_pending_tasks(self, authed_client, tmp_db):
        """Cancelling a project with pending tasks cancels the tasks too."""
        now = time.time()

        user = await tmp_db.fetchone("SELECT id FROM users LIMIT 1")
        user_id = user["id"]

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, owner_id, created_at, updated_at) "
            "VALUES (?, ?, ?, 'executing', ?, ?, ?)",
            ("proj_cancel_001", "Cancel Test", "req", user_id, now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
            ("plan_cancel_001", "proj_cancel_001", json.dumps({"summary": "t", "tasks": []}), now),
        )
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, max_retries, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task_cancel_001", "proj_cancel_001", "plan_cancel_001", "Running Task",
             "Do X", "code", 50, "pending", "haiku", 0, 0, 5, now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, max_retries, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task_cancel_002", "proj_cancel_001", "plan_cancel_001", "Blocked Task",
             "Do Y", "code", 50, "blocked", "haiku", 0, 0, 5, now, now),
        )

        resp = await authed_client.post("/api/projects/proj_cancel_001/cancel")
        assert resp.status_code == 200

        proj = await tmp_db.fetchone(
            "SELECT status FROM projects WHERE id = ?", ("proj_cancel_001",)
        )
        assert proj["status"] == "cancelled"

        tasks = await tmp_db.fetchall(
            "SELECT status FROM tasks WHERE project_id = ?", ("proj_cancel_001",)
        )
        for t in tasks:
            assert t["status"] == "cancelled"
