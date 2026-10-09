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
    try:
        await engine.setup_db()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        # Client is closed by now; release the engine's DB connection.
        # db is None if setup_db failed before opening it.
        if engine.db is not None:
            await engine.db.close()


@pytest_asyncio.fixture
async def engine(client):
    """The engine behind the client, for seeding rows the API cannot set."""
    return client._transport.app.state.engine


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


class TestTaskListProjection:
    """list_tasks projects depends_on (sorted task IDs) and tools (list[str])."""

    @staticmethod
    async def _project(client, name="P"):
        resp = await client.post("/api/projects", json={
            "name": name, "requirements": "X",
        })
        return resp.json()["id"]

    @staticmethod
    async def _decompose(client, pid, phases):
        resp = await client.post(f"/api/projects/{pid}/decompose", json={
            "phases": phases,
        })
        assert resp.status_code == 200
        resp = await client.get(f"/api/tasks/project/{pid}")
        return {t["title"]: t for t in resp.json()}

    @pytest.mark.asyncio
    async def test_exact_chain_and_tasks_without_deps(self, client):
        pid = await self._project(client)
        by_title = await self._decompose(client, pid, [{"name": "Core", "tasks": [
            {"title": "A", "task_type": "code"},
            {"title": "B", "task_type": "code", "depends_on": [0]},
            {"title": "C", "task_type": "code", "depends_on": [1]},
            {"title": "D", "task_type": "code", "depends_on": [2, 0, 1]},
            {"title": "Solo", "task_type": "code"},
        ]}])

        a, b, c, d, solo = (by_title[k]["id"] for k in ("A", "B", "C", "D", "Solo"))
        assert by_title["A"]["depends_on"] == []
        assert by_title["B"]["depends_on"] == [a]
        assert by_title["C"]["depends_on"] == [b]
        assert by_title["D"]["depends_on"] == sorted([a, b, c])
        assert by_title["Solo"]["depends_on"] == []
        assert solo not in by_title["D"]["depends_on"]
        assert d not in by_title["D"]["depends_on"]

    @pytest.mark.asyncio
    async def test_default_shape_excludes_output_and_error(self, client):
        pid = await self._project(client)
        by_title = await self._decompose(client, pid, [{"name": "Core", "tasks": [
            {"title": "A", "task_type": "code"},
        ]}])
        row = by_title["A"]
        assert {"depends_on", "tools"} <= set(row)
        for absent in ("tools_json", "output_text", "error"):
            assert absent not in row

    @pytest.mark.asyncio
    async def test_cross_project_isolation(self, client):
        p1 = await self._project(client, "one")
        p2 = await self._project(client, "two")
        one = await self._decompose(client, p1, [{"name": "Core", "tasks": [
            {"title": "A1", "task_type": "code"},
            {"title": "B1", "task_type": "code", "depends_on": [0]},
        ]}])
        two = await self._decompose(client, p2, [{"name": "Core", "tasks": [
            {"title": "A2", "task_type": "code"},
            {"title": "B2", "task_type": "code", "depends_on": [0]},
        ]}])

        assert set(one) == {"A1", "B1"}
        assert set(two) == {"A2", "B2"}
        assert one["B1"]["depends_on"] == [one["A1"]["id"]]
        assert two["B2"]["depends_on"] == [two["A2"]["id"]]

    @pytest.mark.asyncio
    async def test_status_and_wave_filters(self, client, engine):
        pid = await self._project(client)
        by_title = await self._decompose(client, pid, [
            {"name": "W0", "tasks": [
                {"title": "A", "task_type": "code"},
                {"title": "B", "task_type": "code", "depends_on": [0]},
            ]},
            {"name": "W1", "tasks": [
                # Resolves to the first wave-0 task (existing decomposer behavior)
                {"title": "C", "task_type": "code", "depends_on": [0]},
            ]},
        ])
        a = by_title["A"]["id"]
        assert by_title["C"]["wave"] == 1 and by_title["C"]["depends_on"] == [a]

        # Status filter: dependency of a returned task is reported even if the
        # dependency itself is filtered out of the list.
        resp = await client.get(f"/api/tasks/project/{pid}", params={"status": "blocked"})
        blocked = {t["title"]: t for t in resp.json()}
        assert set(blocked) == {"B", "C"}
        assert blocked["B"]["depends_on"] == [a]
        assert blocked["C"]["depends_on"] == [a]

        resp = await client.get(f"/api/tasks/project/{pid}", params={"wave": 1})
        assert [t["title"] for t in resp.json()] == ["C"]
        assert resp.json()[0]["depends_on"] == [a]

        resp = await client.get(
            f"/api/tasks/project/{pid}", params={"wave": 0, "status": "pending"},
        )
        assert [t["title"] for t in resp.json()] == ["A"]
        assert resp.json()[0]["depends_on"] == []

        # Unfiltered order stays wave-ordered
        resp = await client.get(f"/api/tasks/project/{pid}")
        waves = [t["wave"] for t in resp.json()]
        assert waves == sorted(waves)

    @pytest.mark.asyncio
    async def test_empty_results_run_no_dependency_query(self, client, engine):
        pid = await self._project(client)
        queries = _record_fetchall(engine)

        resp = await client.get(f"/api/tasks/project/{pid}")
        assert resp.json() == []
        resp = await client.get("/api/tasks/project/nonexistent")
        assert resp.json() == []
        assert not [q for q, _ in queries if "task_deps" in q]

        await self._decompose(client, pid, [{"name": "Core", "tasks": [
            {"title": "A", "task_type": "code"},
        ]}])
        queries.clear()
        resp = await client.get(f"/api/tasks/project/{pid}", params={"wave": 9})
        assert resp.json() == []
        assert not [q for q, _ in queries if "task_deps" in q]

    @pytest.mark.asyncio
    async def test_tools_string_array_is_projected(self, client, engine):
        pid = await self._project(client)
        by_title = await self._decompose(client, pid, [{"name": "Core", "tasks": [
            {"title": "A", "task_type": "code"},
            {"title": "B", "task_type": "code"},
        ]}])
        await _set_tools(engine, by_title["A"]["id"], json.dumps(["Read", "Edit"]))
        await _set_tools(engine, by_title["B"]["id"], json.dumps([]))

        resp = await client.get(f"/api/tasks/project/{pid}")
        tools = {t["title"]: t["tools"] for t in resp.json()}
        assert tools == {"A": ["Read", "Edit"], "B": []}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", [
        None,
        "",
        "{not json",
        '{"Read": true}',
        '"Read"',
        "42",
        "true",
        "null",
        '["Read", 1]',
        '["Read", null]',
        '[["Read"]]',
        '[{"name": "Read"}]',
    ])
    async def test_invalid_tools_become_empty_list(self, client, engine, raw):
        pid = await self._project(client)
        by_title = await self._decompose(client, pid, [{"name": "Core", "tasks": [
            {"title": "A", "task_type": "code"},
        ]}])
        if raw is not None:
            await _set_tools(engine, by_title["A"]["id"], raw)

        resp = await client.get(f"/api/tasks/project/{pid}")
        assert resp.status_code == 200
        assert resp.json()[0]["tools"] == []

    @pytest.mark.asyncio
    async def test_dependency_queries_are_bulk_and_bounded(self, client, engine, monkeypatch):
        import gods.api as api_module

        assert api_module.TASK_DEPS_CHUNK_SIZE == 500

        monkeypatch.setattr(api_module, "TASK_DEPS_CHUNK_SIZE", 2)
        pid = await self._project(client)
        titles = [f"T{i}" for i in range(5)]
        by_title = await self._decompose(client, pid, [{"name": "Core", "tasks": [
            {"title": t, "task_type": "code", **({"depends_on": [i - 1]} if i else {})}
            for i, t in enumerate(titles)
        ]}])
        queries = _record_fetchall(engine)

        resp = await client.get(f"/api/tasks/project/{pid}")
        rows = resp.json()
        assert len(rows) == 5

        task_queries = [(q, p) for q, p in queries if "FROM tasks" in q]
        dep_queries = [(q, p) for q, p in queries if "task_deps" in q]
        assert len(task_queries) == 1
        # 5 tasks / chunk of 2 => 3 queries, not 5; none exceeds the bound
        assert len(dep_queries) == 3
        assert all(len(p) <= 2 for _, p in dep_queries)
        # Every returned task ID was covered by exactly one dependency query
        queried = [i for _, p in dep_queries for i in p]
        assert sorted(queried) == sorted(t["id"] for t in rows)

        # No dependency lost across chunk boundaries
        for i in range(1, 5):
            assert by_title[f"T{i}"]["depends_on"] == [by_title[f"T{i - 1}"]["id"]]
            got = next(r for r in rows if r["title"] == f"T{i}")
            assert got["depends_on"] == [by_title[f"T{i - 1}"]["id"]]
        assert next(r for r in rows if r["title"] == "T0")["depends_on"] == []

    @pytest.mark.asyncio
    async def test_single_dependency_query_below_bound(self, client, engine):
        pid = await self._project(client)
        await self._decompose(client, pid, [{"name": "Core", "tasks": [
            {"title": f"T{i}", "task_type": "code"} for i in range(6)
        ]}])
        queries = _record_fetchall(engine)

        await client.get(f"/api/tasks/project/{pid}")
        assert len([q for q, _ in queries if "task_deps" in q]) == 1


def _record_fetchall(engine):
    """Wrap engine.db.fetchall to record (sql, params); returns the live list."""
    seen: list[tuple[str, tuple]] = []
    original = engine.db.fetchall

    async def recording(sql, params=()):
        seen.append((sql, params))
        return await original(sql, params)

    engine.db.fetchall = recording
    return seen


async def _set_tools(engine, task_id, raw):
    await engine.db.execute_write(
        "UPDATE tasks SET tools_json = $1 WHERE id = $2", (raw, task_id),
    )


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
