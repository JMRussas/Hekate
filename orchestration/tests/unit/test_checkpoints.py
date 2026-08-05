#  Orchestration Engine - Checkpoint Tests
#
#  Tests for checkpoint creation during retry exhaustion
#  and the checkpoint API endpoints.
#
#  Depends on: backend/services/task_lifecycle.py, backend/routes/checkpoints.py
#  Used by:    pytest

import json
import time
from unittest.mock import AsyncMock

import pytest

from backend.models.enums import ProjectStatus, TaskStatus
from backend.services.task_lifecycle import create_checkpoint


@pytest.fixture
async def checkpoint_setup(tmp_db):
    """Create a project with a task ready for checkpoint testing."""
    now = time.time()
    project_id = "proj_cp_001"
    plan_id = "plan_cp_001"
    task_id = "task_cp_001"

    await tmp_db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, "Checkpoint Test", "Test checkpoints", ProjectStatus.EXECUTING, now, now),
    )
    await tmp_db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test-model', ?, 'approved', ?)",
        (plan_id, project_id, json.dumps({"summary": "test", "tasks": []}), now),
    )
    await tmp_db.execute_write(
        "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
        "priority, status, model_tier, wave, retry_count, max_retries, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, project_id, plan_id, "Failing Task", "This task keeps failing",
         "code", 0, TaskStatus.RUNNING, "haiku", 0, 3, 3, now, now),
    )

    # Add some retry event history
    for i in range(3):
        await tmp_db.execute_write(
            "INSERT INTO task_events (project_id, task_id, event_type, message, data_json, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, task_id, "task_retry", f"Retry {i+1}: Connection error", "{}", now + i),
        )

    return tmp_db, project_id, task_id


class TestCheckpointCreation:
    async def test_retry_exhausted_creates_checkpoint(self, checkpoint_setup):
        """When a task exhausts retries, a checkpoint record should be created."""
        tmp_db, project_id, task_id = checkpoint_setup

        mock_progress = AsyncMock()
        mock_progress.push_event = AsyncMock()

        task_row = await tmp_db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        await create_checkpoint(
            project_id=project_id, task_id=task_id, task_row=task_row,
            error_msg="Max retries exceeded: Connection error",
            db=tmp_db, progress=mock_progress,
        )

        # Check checkpoint was created
        cp = await tmp_db.fetchone(
            "SELECT * FROM checkpoints WHERE task_id = ?", (task_id,),
        )
        assert cp is not None
        assert cp["project_id"] == project_id
        assert cp["checkpoint_type"] == "retry_exhausted"
        assert "Failing Task" in cp["summary"]
        assert cp["resolved_at"] is None

        # Check attempts include our retry events
        attempts = json.loads(cp["attempts_json"])
        assert len(attempts) == 3
        assert "Retry 1" in attempts[0]["message"]

    async def test_checkpoint_sets_task_needs_review(self, checkpoint_setup):
        """Creating a checkpoint should set the task to NEEDS_REVIEW."""
        tmp_db, project_id, task_id = checkpoint_setup

        mock_progress = AsyncMock()
        mock_progress.push_event = AsyncMock()

        task_row = await tmp_db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        await create_checkpoint(
            project_id=project_id, task_id=task_id, task_row=task_row,
            error_msg="Max retries exceeded",
            db=tmp_db, progress=mock_progress,
        )

        task = await tmp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert task["status"] == TaskStatus.NEEDS_REVIEW

    async def test_checkpoint_emits_sse_event(self, checkpoint_setup):
        """Creating a checkpoint should emit a 'checkpoint' SSE event."""
        tmp_db, project_id, task_id = checkpoint_setup

        mock_progress = AsyncMock()
        mock_progress.push_event = AsyncMock()

        task_row = await tmp_db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        await create_checkpoint(
            project_id=project_id, task_id=task_id, task_row=task_row,
            error_msg="Max retries exceeded",
            db=tmp_db, progress=mock_progress,
        )

        # Find the checkpoint event call
        checkpoint_calls = [
            c for c in mock_progress.push_event.call_args_list
            if c.args[1] == "checkpoint"
        ]
        assert len(checkpoint_calls) == 1
        assert "Failing Task" in checkpoint_calls[0].args[2]


class TestCheckpointAPI:
    async def test_list_checkpoints(self, authed_client):
        """GET /api/checkpoints/project/{id} returns unresolved checkpoints."""
        client = authed_client

        # Create project
        resp = await client.post("/api/projects", json={
            "name": "CP API Test", "requirements": "test",
        })
        project_id = resp.json()["id"]

        from backend.app import container
        db = container.db()

        # Insert a checkpoint directly
        now = time.time()
        plan_id = "plan_cp_api_001"
        task_id = "task_cp_api_001"
        await db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
            (plan_id, project_id, '{"summary":"t","tasks":[]}', now),
        )
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, project_id, plan_id, "T", "D", "code", 0,
             TaskStatus.NEEDS_REVIEW, "haiku", 0, now, now),
        )
        await db.execute_write(
            "INSERT INTO checkpoints (id, project_id, task_id, checkpoint_type, "
            "summary, attempts_json, question, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("cp_001", project_id, task_id, "retry_exhausted",
             "Task failed", "[]", "What to do?", now),
        )

        resp = await client.get(f"/api/checkpoints/project/{project_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == "cp_001"
        assert data[0]["resolved_at"] is None

    async def test_resolve_with_retry(self, authed_client):
        """Resolving a checkpoint with 'retry' resets the task to PENDING."""
        client = authed_client

        resp = await client.post("/api/projects", json={
            "name": "CP Retry Test", "requirements": "test",
        })
        project_id = resp.json()["id"]

        from backend.app import container
        db = container.db()

        now = time.time()
        plan_id = "plan_cp_retry"
        task_id = "task_cp_retry"
        await db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
            (plan_id, project_id, '{"summary":"t","tasks":[]}', now),
        )
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, error, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, project_id, plan_id, "T", "D", "code", 0,
             TaskStatus.NEEDS_REVIEW, "haiku", 0, 5, "Max retries", now, now),
        )
        await db.execute_write(
            "INSERT INTO checkpoints (id, project_id, task_id, checkpoint_type, "
            "summary, attempts_json, question, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("cp_retry", project_id, task_id, "retry_exhausted",
             "Task failed", "[]", "What to do?", now),
        )

        resp = await client.post("/api/checkpoints/cp_retry/resolve", json={
            "action": "retry",
            "guidance": "Try using a different approach",
        })
        assert resp.status_code == 200
        assert resp.json()["resolved_at"] is not None

        # Task should be back to pending with reset retry count
        task = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        assert task["status"] == TaskStatus.PENDING
        assert task["retry_count"] == 0
        assert task["error"] is None

        # Check guidance was added to context
        ctx = json.loads(task["context_json"])
        guidance_entries = [e for e in ctx if e.get("type") == "checkpoint_guidance"]
        assert len(guidance_entries) == 1
        assert "different approach" in guidance_entries[0]["content"]

    async def test_resolve_with_skip(self, authed_client):
        """Resolving with 'skip' cancels the task."""
        client = authed_client

        resp = await client.post("/api/projects", json={
            "name": "CP Skip Test", "requirements": "test",
        })
        project_id = resp.json()["id"]

        from backend.app import container
        db = container.db()

        now = time.time()
        plan_id = "plan_cp_skip"
        task_id = "task_cp_skip"
        await db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
            (plan_id, project_id, '{"summary":"t","tasks":[]}', now),
        )
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, project_id, plan_id, "T", "D", "code", 0,
             TaskStatus.NEEDS_REVIEW, "haiku", 0, now, now),
        )
        await db.execute_write(
            "INSERT INTO checkpoints (id, project_id, task_id, checkpoint_type, "
            "summary, attempts_json, question, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("cp_skip", project_id, task_id, "retry_exhausted",
             "Task failed", "[]", "What to do?", now),
        )

        resp = await client.post("/api/checkpoints/cp_skip/resolve", json={
            "action": "skip",
        })
        assert resp.status_code == 200

        task = await db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert task["status"] == TaskStatus.CANCELLED

    async def test_cannot_resolve_twice(self, authed_client):
        """Already-resolved checkpoints should return 400."""
        client = authed_client

        resp = await client.post("/api/projects", json={
            "name": "CP Dupe Test", "requirements": "test",
        })
        project_id = resp.json()["id"]

        from backend.app import container
        db = container.db()

        now = time.time()
        await db.execute_write(
            "INSERT INTO checkpoints (id, project_id, checkpoint_type, "
            "summary, question, response, resolved_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("cp_dupe", project_id, "retry_exhausted",
             "Task failed", "What to do?", "Already handled", now, now),
        )

        resp = await client.post("/api/checkpoints/cp_dupe/resolve", json={
            "action": "fail",
        })
        assert resp.status_code == 400
        assert "already resolved" in resp.json()["detail"].lower()


class TestCheckpointSchemaValidation:
    """Tests for structured checkpoint responses with JSON Schema validation."""

    async def _setup_checkpoint_with_schema(self, client, schema_json=None):
        """Helper: create project + task + checkpoint, optionally with schema_json."""
        resp = await client.post("/api/projects", json={
            "name": "Schema Test", "requirements": "test",
        })
        project_id = resp.json()["id"]

        from backend.app import container
        db = container.db()

        now = time.time()
        plan_id = f"plan_schema_{now}"
        task_id = f"task_schema_{now}"
        cp_id = f"cp_schema_{now}"

        await db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
            (plan_id, project_id, '{"summary":"t","tasks":[]}', now),
        )
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, error, context_json, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, project_id, plan_id, "Schema Task", "Test task", "code", 0,
             TaskStatus.NEEDS_REVIEW, "haiku", 0, 3, "Max retries", "[]", now, now),
        )
        await db.execute_write(
            "INSERT INTO checkpoints (id, project_id, task_id, checkpoint_type, "
            "summary, attempts_json, question, schema_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cp_id, project_id, task_id, "retry_exhausted",
             "Task failed", "[]", "What to do?",
             json.dumps(schema_json) if schema_json else None, now),
        )
        return db, project_id, task_id, cp_id

    async def test_create_checkpoint_with_schema(self, authed_client):
        """Checkpoint created with schema_json should persist and return it."""
        schema = {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["approve", "reject"]}},
            "required": ["action"],
        }
        db, project_id, task_id, cp_id = await self._setup_checkpoint_with_schema(
            authed_client, schema_json=schema,
        )

        resp = await authed_client.get(f"/api/checkpoints/{cp_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["schema_json"] is not None
        assert data["schema_json"]["required"] == ["action"]

    async def test_resolve_with_valid_structured_response(self, authed_client):
        """Resolving with a structured_response that passes schema validation succeeds."""
        schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["approve", "reject"]},
                "reason": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        }
        db, project_id, task_id, cp_id = await self._setup_checkpoint_with_schema(
            authed_client, schema_json=schema,
        )

        resp = await authed_client.post(f"/api/checkpoints/{cp_id}/resolve", json={
            "action": "retry",
            "structured_response": {"action": "approve", "reason": "Looks good"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved_at"] is not None
        # Response should contain the structured data as JSON
        assert "approve" in data["response"]

    async def test_resolve_with_invalid_structured_response_returns_422(self, authed_client):
        """Resolving with a structured_response that fails schema validation returns 422."""
        schema = {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["approve", "reject"]},
            },
            "required": ["action"],
            "additionalProperties": False,
        }
        db, project_id, task_id, cp_id = await self._setup_checkpoint_with_schema(
            authed_client, schema_json=schema,
        )

        resp = await authed_client.post(f"/api/checkpoints/{cp_id}/resolve", json={
            "action": "retry",
            "structured_response": {"action": "invalid_value"},
        })
        assert resp.status_code == 422
        assert "validation failed" in resp.json()["detail"].lower()

    async def test_structured_response_injected_into_task_context(self, authed_client):
        """Valid structured response should be injected into task context_json."""
        schema = {
            "title": "approve_reject",
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["approve", "reject"]},
                "reason": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        }
        db, project_id, task_id, cp_id = await self._setup_checkpoint_with_schema(
            authed_client, schema_json=schema,
        )

        resp = await authed_client.post(f"/api/checkpoints/{cp_id}/resolve", json={
            "action": "retry",
            "structured_response": {"action": "reject", "reason": "Needs rework"},
        })
        assert resp.status_code == 200

        task = await db.fetchone("SELECT context_json FROM tasks WHERE id = ?", (task_id,))
        ctx = json.loads(task["context_json"])
        structured_entries = [e for e in ctx if e.get("type") == "checkpoint_structured_response"]
        assert len(structured_entries) == 1
        assert structured_entries[0]["schema_name"] == "approve_reject"
        assert structured_entries[0]["data"]["action"] == "reject"
        assert structured_entries[0]["data"]["reason"] == "Needs rework"

    async def test_backward_compat_no_schema_accepts_plain_guidance(self, authed_client):
        """Checkpoint without schema_json accepts plain guidance (backward compat)."""
        db, project_id, task_id, cp_id = await self._setup_checkpoint_with_schema(
            authed_client, schema_json=None,
        )

        resp = await authed_client.post(f"/api/checkpoints/{cp_id}/resolve", json={
            "action": "retry",
            "guidance": "Try a different approach",
        })
        assert resp.status_code == 200
        assert resp.json()["resolved_at"] is not None

        # Guidance should land in context as plain checkpoint_guidance
        task = await db.fetchone("SELECT context_json FROM tasks WHERE id = ?", (task_id,))
        ctx = json.loads(task["context_json"])
        guidance_entries = [e for e in ctx if e.get("type") == "checkpoint_guidance"]
        assert len(guidance_entries) == 1
        assert "different approach" in guidance_entries[0]["content"]

    async def test_predefined_schemas_endpoint(self, authed_client):
        """GET /api/checkpoints/schemas returns all predefined schemas."""
        resp = await authed_client.get("/api/checkpoints/schemas")
        assert resp.status_code == 200
        data = resp.json()
        expected_keys = {"approve_reject", "select_option", "provide_file_path", "free_text_with_reason"}
        assert set(data.keys()) == expected_keys
        # Each schema should be a valid JSON Schema object
        for name, schema in data.items():
            assert schema["type"] == "object"
            assert "properties" in schema
            assert "required" in schema
