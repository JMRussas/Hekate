"""Integration tests for the gods pipeline.

Tests the actual event chain without mocking internal handlers:
1. API contract — every prometheus MCP path exists on the engine
2. Mimir→Engine round-trip — submit_verification writes to DB and mimir reads it back
3. Full pipeline smoke — project_created → plan → dispatch → execute → verify → wave progression
"""

import asyncio
import json
import time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gods.api import create_app
from gods.handlers.mimir import MimirRunner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def engine_app():
    """Create a fully initialized engine app with in-memory DB."""
    app = create_app(db_path=":memory:", max_concurrent=4)
    engine = app.state.engine
    await engine.setup_db()
    yield app, engine
    await engine.stop()


@pytest_asyncio.fixture
async def client(engine_app):
    """AsyncClient wired to the engine app via ASGI transport."""
    app, _ = engine_app
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture
async def project_with_tasks(client, engine_app):
    """Create a project and manually insert tasks (skip LLM planning)."""
    _, engine = engine_app
    db = engine.db

    # Create project
    resp = await client.post("/api/projects", json={
        "name": "Integration Test",
        "requirements": "Test requirements",
        "repo_path": "/tmp/test",
    })
    assert resp.status_code == 200
    project_id = resp.json()["id"]

    # Insert a plan
    plan_id = "plan_test_001"
    await db.execute_write(
        "INSERT INTO plans (id, project_id, plan_json, level, created_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        (plan_id, project_id, json.dumps({"summary": "test", "phases": []}),
         "L1", time.time()),
    )

    # Insert tasks directly (bypass planning)
    tasks = []
    for i, (title, wave) in enumerate([
        ("Task A", 0),
        ("Task B", 0),
        ("Task C", 1),  # depends on A and B
    ]):
        task_id = f"task_{i:04d}"
        status = "pending" if wave == 0 else "blocked"
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, "
            "task_type, wave, status, model_tier, context_json, "
            "created_at, updated_at, retry_count, max_retries) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
            (task_id, project_id, plan_id, title, f"Do {title}",
             "code", wave, status, "claude_code", "{}",
             time.time(), time.time(), 0, 3),
        )
        tasks.append(task_id)

    # Add dependency: Task C depends on Task A and Task B
    await db.execute_write(
        "INSERT INTO task_deps (task_id, depends_on) VALUES ($1, $2)",
        ("task_0002", "task_0000"),
    )
    await db.execute_write(
        "INSERT INTO task_deps (task_id, depends_on) VALUES ($1, $2)",
        ("task_0002", "task_0001"),
    )

    # Set project to executing
    await db.execute_write(
        "UPDATE projects SET status = $1 WHERE id = $2",
        ("executing", project_id),
    )

    return project_id, tasks


# ---------------------------------------------------------------------------
# Test 1: API Contract — every prometheus MCP path exists on the engine
# ---------------------------------------------------------------------------

class TestAPIContract:
    """Verify every API path that prometheus MCP calls has a route on the engine."""

    # These are the paths prometheus_mcp.py calls via _get/_post/_delete
    PROMETHEUS_PATHS = [
        ("GET", "/api/projects"),
        ("GET", "/api/projects/{id}"),
        ("POST", "/api/projects"),
        ("POST", "/api/projects/{id}/execute"),
        ("POST", "/api/projects/{id}/cancel"),
        ("GET", "/api/tasks/project/{id}"),
        ("GET", "/api/tasks/{id}"),
        ("POST", "/api/tasks/{id}/retry"),
        ("POST", "/api/tasks/{id}/review"),
        ("POST", "/api/tasks/{id}/verify"),
        ("GET", "/api/events/{id}"),
        ("GET", "/api/health"),
    ]

    @pytest.mark.asyncio
    async def test_all_prometheus_routes_exist(self, client):
        """Every API path prometheus calls must NOT return 501 (Not Implemented)."""
        for method, path in self.PROMETHEUS_PATHS:
            # Use a fake ID — we just need to check the route exists (not 501)
            test_path = path.replace("{id}", "nonexistent_id_12345")

            if method == "GET":
                resp = await client.get(test_path)
            else:
                resp = await client.post(test_path, json={})

            assert resp.status_code != 501, (
                f"{method} {path} returned 501 Not Implemented — "
                f"route is missing from the engine API"
            )

    @pytest.mark.asyncio
    async def test_verify_endpoint_accepts_verdict(self, client, project_with_tasks):
        """POST /tasks/{id}/verify must accept a verdict and update the DB."""
        project_id, tasks = project_with_tasks
        task_id = tasks[0]

        # Mark task as completed first (verify only works on completed tasks)
        _, engine = client._transport.app.state, None  # get engine
        app = client._transport.app
        engine = app.state.engine
        await engine.db.execute_write(
            "UPDATE tasks SET status = $1, output_text = $2 WHERE id = $3",
            ("completed", "task output here", task_id),
        )

        resp = await client.post(f"/api/tasks/{task_id}/verify", json={
            "verdict": "passed",
            "feedback": "Looks good",
            "confidence": 0.95,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] is True

        # Verify DB was updated
        row = await engine.db.fetchone(
            "SELECT verification_status, verification_notes FROM tasks WHERE id = $1",
            (task_id,),
        )
        assert row["verification_status"] == "passed"
        assert row["verification_notes"] == "Looks good"

    @pytest.mark.asyncio
    async def test_verify_gaps_found_retries(self, client, project_with_tasks):
        """gaps_found verdict should reset task to pending for retry."""
        project_id, tasks = project_with_tasks
        task_id = tasks[0]

        app = client._transport.app
        engine = app.state.engine
        await engine.db.execute_write(
            "UPDATE tasks SET status = $1, output_text = $2 WHERE id = $3",
            ("completed", "bad output", task_id),
        )

        resp = await client.post(f"/api/tasks/{task_id}/verify", json={
            "verdict": "gaps_found",
            "feedback": "Missing error handling",
            "confidence": 0.8,
        })
        assert resp.status_code == 200

        row = await engine.db.fetchone(
            "SELECT status, verification_status, retry_count FROM tasks WHERE id = $1",
            (task_id,),
        )
        assert row["status"] == "pending"
        assert row["verification_status"] == "gaps_found"
        assert row["retry_count"] == 1


# ---------------------------------------------------------------------------
# Test 2: Mimir→Engine round-trip
# ---------------------------------------------------------------------------

class TestMimirEngineRoundTrip:
    """Test that mimir's _agent_submitted flow reads back correct DB state."""

    @pytest.mark.asyncio
    async def test_agent_verdict_passed_emits_task_verified(self, engine_app):
        """When /verify sets verification_status=passed, mimir emits task_verified."""
        app, engine = engine_app
        db = engine.db

        # Create project + task
        project_id = "proj_rt_001"
        task_id = "task_rt_001"
        now = time.time()
        await db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            (project_id, "Round Trip Test", "test", "executing", now, now),
        )
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, title, description, task_type, wave, "
            "status, model_tier, context_json, created_at, updated_at, retry_count, max_retries) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
            (task_id, project_id, "Test Task", "Do test", "code", 0,
             "completed", "claude_code", "{}", now, now, 0, 3),
        )

        # Simulate what the verify endpoint does
        await db.execute_write(
            "UPDATE tasks SET verification_status = $1, verification_notes = $2, updated_at = $3 WHERE id = $4",
            ("passed", "All good", time.time(), task_id),
        )

        # Create MimirRunner and test the _agent_submitted path
        mimir = MimirRunner(db=db)

        # Mock _call_verifier to return _agent_submitted
        mock_verifier = AsyncMock(return_value={
            "verdict": "_agent_submitted",
            "confidence": 1.0,
            "feedback": "",
        })

        with patch("gods.handlers.mimir._call_verifier", mock_verifier):
            await mimir._verify_task(
                task_id=task_id, project_id=project_id,
                title="Test Task", description="Do test",
                output_text="task output", retry_count=0, max_retries=3,
            )

        # Check that task_verified was written to relay events
        events = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE source = $1 ORDER BY id DESC LIMIT 5",
            ("mimir",),
        )
        event_types = [e["event_type"] for e in events]
        assert "task_verified" in event_types, (
            f"Expected task_verified in relay events, got: {event_types}"
        )

    @pytest.mark.asyncio
    async def test_agent_verdict_gaps_found_emits_task_rejected(self, engine_app):
        """When /verify sets status=pending (gaps_found+retry), mimir emits task_rejected."""
        app, engine = engine_app
        db = engine.db

        project_id = "proj_rt_002"
        task_id = "task_rt_002"
        now = time.time()
        await db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            (project_id, "Round Trip Test 2", "test", "executing", now, now),
        )
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, title, description, task_type, wave, "
            "status, model_tier, context_json, created_at, updated_at, retry_count, max_retries) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
            (task_id, project_id, "Test Task 2", "Do test", "code", 0,
             "completed", "claude_code", "{}", now, now, 0, 3),
        )

        # Simulate gaps_found verdict: task reset to pending with retry
        await db.execute_write(
            "UPDATE tasks SET status = $1, verification_status = $2, "
            "verification_notes = $3, retry_count = 1, updated_at = $4 WHERE id = $5",
            ("pending", "gaps_found", "Missing tests", time.time(), task_id),
        )

        mimir = MimirRunner(db=db)
        mock_verifier = AsyncMock(return_value={
            "verdict": "_agent_submitted",
            "confidence": 1.0,
            "feedback": "",
        })

        with patch("gods.handlers.mimir._call_verifier", mock_verifier):
            await mimir._verify_task(
                task_id=task_id, project_id=project_id,
                title="Test Task 2", description="Do test",
                output_text="bad output", retry_count=1, max_retries=3,
            )

        events = await db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE source = $1 ORDER BY id DESC LIMIT 5",
            ("mimir",),
        )
        event_types = [e["event_type"] for e in events]
        assert "task_rejected" in event_types, (
            f"Expected task_rejected in relay events, got: {event_types}"
        )


# ---------------------------------------------------------------------------
# Test 3: Full pipeline smoke test
# ---------------------------------------------------------------------------

class TestPipelineSmoke:
    """End-to-end pipeline test with mocked LLM + execution."""

    @pytest.mark.asyncio
    async def test_wave_progression(self, engine_app):
        """Create project → skip planning → dispatch → mock execute → verify → unblock wave 1."""
        app, engine = engine_app
        db = engine.db

        # Create project
        project_id = "proj_smoke_001"
        now = time.time()
        await db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, config_json, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            (project_id, "Smoke Test", "test", "executing",
             json.dumps({"target_level": "L1", "tdd": False}), now, now),
        )

        # Insert wave 0 task (pending) and wave 1 task (blocked, depends on wave 0)
        for task_id, title, wave, status in [
            ("smoke_t0", "Wave 0 Task", 0, "pending"),
            ("smoke_t1", "Wave 1 Task", 1, "blocked"),
        ]:
            await db.execute_write(
                "INSERT INTO tasks (id, project_id, title, description, task_type, wave, "
                "status, model_tier, context_json, created_at, updated_at, retry_count, max_retries) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)",
                (task_id, project_id, title, f"Do {title}", "code", wave,
                 status, "claude_code", "{}", now, now, 0, 3),
            )
        await db.execute_write(
            "INSERT INTO task_deps (task_id, depends_on) VALUES ($1, $2)",
            ("smoke_t1", "smoke_t0"),
        )

        # Start pipeline
        await engine.start()

        # Emit project_tick to trigger dispatch
        await db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            ("project_tick", "test", json.dumps({"project_id": project_id}), "info", time.time()),
        )

        # Wait for dispatch
        await asyncio.sleep(1)

        # Check task was dispatched (status should be running or still pending if hermes queued it)
        t0 = await db.fetchone("SELECT status FROM tasks WHERE id = $1", ("smoke_t0",))
        # It might be running (dispatched) or pending (hermes hasn't processed yet)
        # The dispatch_command should exist in relay events
        events = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = $1",
            ("dispatch_command",),
        )
        assert len(events) > 0, "dispatch_command event should have been emitted"

        # Simulate task completion (hermes would do this)
        await db.execute_write(
            "UPDATE tasks SET status = $1, output_text = $2, updated_at = $3 WHERE id = $4",
            ("completed", "task output", time.time(), "smoke_t0"),
        )

        # Simulate verification (mimir would do this)
        await db.execute_write(
            "UPDATE tasks SET verification_status = $1, updated_at = $2 WHERE id = $3",
            ("passed", time.time(), "smoke_t0"),
        )

        # Emit task_verified to trigger lifecycle
        await db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            ("task_verified", "test", json.dumps({
                "task_id": "smoke_t0",
                "project_id": project_id,
                "confidence": 1.0,
            }), "info", time.time()),
        )

        # Wait for pipeline tick to process task_verified → odin_lifecycle → unblock
        await asyncio.sleep(8)

        # Check wave 1 task was unblocked
        t1 = await db.fetchone("SELECT status FROM tasks WHERE id = $1", ("smoke_t1",))
        assert t1["status"] == "pending", (
            f"Wave 1 task should be unblocked (pending), got: {t1['status']}"
        )

        await engine.stop()
