"""Tests for the Hekate FastAPI app.

RED PHASE: gods/api.py does not exist yet.

The API wraps the HekateEngine and exposes the routes
the dashboard needs. No monolith imports.
"""

import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

# RED: this doesn't exist yet
from gods.api import create_app


@pytest_asyncio.fixture
async def client():
    """Test client for the Hekate API with initialized DB."""
    from httpx import AsyncClient, ASGITransport

    app = create_app(db_path=":memory:")
    # Manually init engine since lifespan doesn't run in test transport
    engine = app.state.engine
    await engine.setup_db()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHealth:
    @pytest.mark.asyncio
    async def test_health(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class TestProjects:
    @pytest.mark.asyncio
    async def test_create_project(self, client):
        resp = await client.post("/api/projects", json={
            "name": "Test Project",
            "requirements": "Add a health endpoint",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["name"] == "Test Project"
        assert data["status"] == "draft"

    @pytest.mark.asyncio
    async def test_list_projects(self, client):
        await client.post("/api/projects", json={
            "name": "P1", "requirements": "X",
        })
        await client.post("/api/projects", json={
            "name": "P2", "requirements": "Y",
        })

        resp = await client.get("/api/projects")
        assert resp.status_code == 200
        projects = resp.json()
        assert len(projects) >= 2

    @pytest.mark.asyncio
    async def test_get_project(self, client):
        create = await client.post("/api/projects", json={
            "name": "Detail Test", "requirements": "Z",
        })
        pid = create.json()["id"]

        resp = await client.get(f"/api/projects/{pid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == pid
        assert "tasks" in data

    @pytest.mark.asyncio
    async def test_get_missing_project(self, client):
        resp = await client.get("/api/projects/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_project_with_config(self, client):
        resp = await client.post("/api/projects", json={
            "name": "Configured",
            "requirements": "Build X",
            "config": {"tdd": True, "target_level": "L3"},
        })
        data = resp.json()
        assert data["config_json"] is not None
        cfg = json.loads(data["config_json"])
        assert cfg["tdd"] is True


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class TestTasks:
    @pytest.mark.asyncio
    async def test_list_tasks_for_project(self, client):
        create = await client.post("/api/projects", json={
            "name": "Task Test", "requirements": "X",
        })
        pid = create.json()["id"]

        resp = await client.get(f"/api/tasks/project/{pid}")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_list_tasks_returns_depends_on_and_tools(self, client):
        """list_tasks should include depends_on (from task_deps join) +
        parsed tools (from tools_json) so the canvas/UI can render the
        DAG without an N+1 per-task fetch."""
        create = await client.post("/api/projects", json={
            "name": "DepTest", "requirements": "X",
        })
        pid = create.json()["id"]

        # Decompose a plan where task[1] depends on task[0].
        # depends_on in the plan is a list of indices into the SAME task
        # list (decomposer resolves to task IDs and writes task_deps).
        await client.post(f"/api/projects/{pid}/decompose", json={
            "phases": [{"name": "Core", "tasks": [
                {"title": "First",  "task_type": "code", "depends_on": []},
                {"title": "Second", "task_type": "code", "depends_on": [0]},
            ]}],
        })

        resp = await client.get(f"/api/tasks/project/{pid}")
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 2

        by_title = {t["title"]: t for t in tasks}
        first = by_title["First"]
        second = by_title["Second"]

        # Both surface depends_on as a list (empty for roots).
        assert first.get("depends_on") == []
        assert second.get("depends_on") == [first["id"]]

        # Both surface tools as a list (empty when tools_json is null).
        assert first.get("tools") == []
        assert second.get("tools") == []

        # Other newly-included fields are present (may be None/0/empty,
        # but the keys exist so the frontend doesn't have to defensively
        # ?? everything).
        for t in tasks:
            assert "phase" in t
            assert "priority" in t
            assert "output_text" in t
            assert "error" in t
            assert "started_at" in t
            assert "completed_at" in t

    @pytest.mark.asyncio
    async def test_get_task_detail(self, client):
        # Create project and decompose a plan to get tasks
        create = await client.post("/api/projects", json={
            "name": "Task Detail", "requirements": "X",
        })
        pid = create.json()["id"]

        # Manually decompose a plan
        resp = await client.post(f"/api/projects/{pid}/decompose", json={
            "phases": [{"name": "Core", "tasks": [
                {"title": "Do thing", "task_type": "code"},
            ]}],
        })
        tasks = await client.get(f"/api/tasks/project/{pid}")
        task_list = tasks.json()
        if task_list:
            tid = task_list[0]["id"]
            detail = await client.get(f"/api/tasks/{tid}")
            assert detail.status_code == 200
            assert detail.json()["id"] == tid


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

class TestExecution:
    @pytest.mark.asyncio
    async def test_start_execution(self, client):
        create = await client.post("/api/projects", json={
            "name": "Exec Test", "requirements": "X",
        })
        pid = create.json()["id"]

        # Set to planned first
        resp = await client.post(f"/api/projects/{pid}/execute")
        # Should emit project_created event for pipeline to pick up
        assert resp.status_code in (200, 202)


# ---------------------------------------------------------------------------
# SSE Events
# ---------------------------------------------------------------------------

class TestEvents:
    @pytest.mark.asyncio
    async def test_get_events(self, client):
        create = await client.post("/api/projects", json={
            "name": "Events Test", "requirements": "X",
        })
        pid = create.json()["id"]

        resp = await client.get(f"/api/events/{pid}")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

class TestServices:
    @pytest.mark.asyncio
    async def test_list_services(self, client):
        resp = await client.get("/api/services")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
