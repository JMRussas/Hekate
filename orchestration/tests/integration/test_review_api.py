#  Orchestration Engine - Review Endpoint Tests
#
#  Tests for the POST /{task_id}/review endpoint.
#
#  Depends on: backend/routes/tasks.py
#  Used by:    pytest

import json
import time

import pytest

from backend.models.enums import TaskStatus


@pytest.fixture
async def review_setup(authed_client):
    """Create a project with a task in NEEDS_REVIEW status."""
    client = authed_client

    # Create project
    resp = await client.post("/api/projects", json={
        "name": "Review Test",
        "requirements": "Test review flow",
    })
    assert resp.status_code == 201
    project_id = resp.json()["id"]

    # Generate a plan (mocked)
    resp = await client.post(f"/api/projects/{project_id}/plan")
    # Plan generation may be mocked differently — use direct DB setup instead
    return client, project_id


async def _create_needs_review_task(client, project_id: str, db):
    """Insert a task directly in NEEDS_REVIEW state for testing."""
    now = time.time()
    task_id = "task_review_001"
    plan_id = "plan_review_001"

    await db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test-model', ?, 'approved', ?)",
        (plan_id, project_id, json.dumps({"summary": "test", "tasks": []}), now),
    )

    await db.execute_write(
        "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
        "priority, status, model_tier, wave, output_text, verification_status, "
        "verification_notes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, project_id, plan_id, "Review Task", "Do something",
         "code", 0, TaskStatus.NEEDS_REVIEW, "haiku", 0,
         "Some output that needs review", "human_needed",
         "Ambiguous requirements", now, now),
    )
    return task_id


class TestReviewEndpoint:
    async def test_approve_moves_to_completed(self, authed_client):
        """Approving a NEEDS_REVIEW task should set it to COMPLETED."""
        client = authed_client

        # Create project
        resp = await client.post("/api/projects", json={
            "name": "Approve Test", "requirements": "test",
        })
        project_id = resp.json()["id"]

        # Get the DB instance from the container
        from backend.app import container
        db = container.db()

        task_id = await _create_needs_review_task(client, project_id, db)

        resp = await client.post(f"/api/tasks/{task_id}/review", json={
            "action": "approve",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    async def test_retry_moves_to_pending_with_feedback(self, authed_client):
        """Retrying a NEEDS_REVIEW task should set it to PENDING with feedback in context."""
        client = authed_client

        resp = await client.post("/api/projects", json={
            "name": "Retry Test", "requirements": "test",
        })
        project_id = resp.json()["id"]

        from backend.app import container
        db = container.db()
        task_id = await _create_needs_review_task(client, project_id, db)

        resp = await client.post(f"/api/tasks/{task_id}/review", json={
            "action": "retry",
            "feedback": "Please use REST instead of GraphQL",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"
        assert data["verification_status"] is None
        assert data["output_text"] is None

        # Check that feedback was added to context
        row = await db.fetchone("SELECT context_json FROM tasks WHERE id = ?", (task_id,))
        ctx = json.loads(row["context_json"])
        review_entries = [e for e in ctx if e.get("type") == "review_feedback"]
        assert len(review_entries) == 1
        assert "REST instead of GraphQL" in review_entries[0]["content"]

    async def test_review_rejects_non_needs_review_task(self, authed_client):
        """Can only review tasks in NEEDS_REVIEW status."""
        client = authed_client

        resp = await client.post("/api/projects", json={
            "name": "Bad Review Test", "requirements": "test",
        })
        project_id = resp.json()["id"]

        from backend.app import container
        db = container.db()

        # Create a task in PENDING status
        now = time.time()
        task_id = "task_pending_001"
        plan_id = "plan_pending_001"
        await db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'approved', ?)",
            (plan_id, project_id, json.dumps({"summary": "test", "tasks": []}), now),
        )
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, project_id, plan_id, "Pending Task", "Do",
             "code", 0, TaskStatus.PENDING, "haiku", 0, now, now),
        )

        resp = await client.post(f"/api/tasks/{task_id}/review", json={
            "action": "approve",
        })
        assert resp.status_code == 400
        assert "needs_review" in resp.json()["detail"].lower()
