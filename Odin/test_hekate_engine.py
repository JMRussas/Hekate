"""Tests for the unified Hekate engine.

RED PHASE: The unified engine doesn't exist yet.

The engine:
  1. Mounts pipeline as a lifespan task in FastAPI
  2. Creates projects via API → emits project_created
  3. Streams relay events via SSE
  4. Plans without importing monolith PlannerService
  5. Decomposes without importing monolith DecomposerService
"""

import asyncio
import json
import time
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from gods.pipeline import Pipeline, Event, Emit
from gods.plan_levels import PlanLevel, TaskSpec, validate_plan


# ---------------------------------------------------------------------------
# 1. Pipeline mounts as lifespan task
# ---------------------------------------------------------------------------

class TestPipelineLifespan:
    @pytest.mark.asyncio
    async def test_pipeline_starts_on_lifespan(self):
        """Pipeline should start ticking when the app starts."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()
        assert engine.pipeline is not None
        assert len(engine.pipeline._handlers) > 0

    @pytest.mark.asyncio
    async def test_pipeline_stops_on_shutdown(self):
        """Pipeline should stop cleanly on app shutdown."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()
        await engine.start()
        assert engine.running

        await engine.stop()
        assert not engine.running

    @pytest.mark.asyncio
    async def test_handlers_registered(self):
        """All god handlers should be registered."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()
        handler_names = [r.name for r in engine.pipeline._handlers]

        assert "athena_plan_leveled" in handler_names
        assert "odin_start" in handler_names
        assert "odin_dispatch" in handler_names
        assert "handle_dispatch" in handler_names
        assert "handle_verify" in handler_names


# ---------------------------------------------------------------------------
# 2. Project creation → event emission
# ---------------------------------------------------------------------------

class TestProjectCreation:
    @pytest.mark.asyncio
    async def test_create_project_emits_event(self):
        """Creating a project should write a project_created event to relay."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        project_id = await engine.create_project(
            name="Test Project",
            requirements="Add a health endpoint",
        )

        assert project_id is not None

        # Check project exists in DB
        row = await engine.db.fetchone(
            "SELECT id, name, status FROM projects WHERE id = $1",
            (project_id,),
        )
        assert row is not None
        assert row["status"] == "draft"

    @pytest.mark.asyncio
    async def test_create_project_writes_relay_event(self):
        """project_created event should be in the relay table."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        project_id = await engine.create_project(
            name="Test Project",
            requirements="Build something",
        )

        events = await engine.db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = $1",
            ("project_created",),
        )
        assert len(events) >= 1
        payload = json.loads(events[0]["payload"])
        assert payload["project_id"] == project_id

    @pytest.mark.asyncio
    async def test_create_project_with_config(self):
        """Project config (tdd, target_level, etc) should be stored."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        project_id = await engine.create_project(
            name="Test",
            requirements="Build X",
            config={"tdd": True, "target_level": "L3", "max_concurrent": 2},
        )

        row = await engine.db.fetchone(
            "SELECT config_json FROM projects WHERE id = $1",
            (project_id,),
        )
        cfg = json.loads(row["config_json"])
        assert cfg["tdd"] is True
        assert cfg["target_level"] == "L3"


# ---------------------------------------------------------------------------
# 3. SSE event streaming
# ---------------------------------------------------------------------------

class TestSSEStreaming:
    @pytest.mark.asyncio
    async def test_stream_events_yields_new_events(self):
        """SSE stream should yield events as they appear in the relay."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        # Write an event
        await engine.db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            ("narration", "athena", '{"text": "Planning..."}', "info", time.time()),
        )

        # Stream should yield it
        events = []
        async for event in engine.stream_events(since_id=0, max_events=1):
            events.append(event)

        assert len(events) == 1
        assert events[0]["event_type"] == "narration"

    @pytest.mark.asyncio
    async def test_stream_filters_by_project(self):
        """SSE stream can filter by project_id."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        await engine.db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            ("narration", "athena", '{"project_id": "proj-1", "text": "Planning"}', "info", time.time()),
        )
        await engine.db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            ("narration", "athena", '{"project_id": "proj-2", "text": "Other"}', "info", time.time()),
        )

        events = []
        async for event in engine.stream_events(since_id=0, project_id="proj-1", max_events=10):
            events.append(event)

        assert len(events) == 1
        assert "proj-1" in events[0]["payload"]


# ---------------------------------------------------------------------------
# 4. Standalone planner (no monolith import)
# ---------------------------------------------------------------------------

class TestStandalonePlanner:
    @pytest.mark.asyncio
    async def test_plan_via_gateway_not_monolith(self):
        """Planning should call the LLM gateway directly, not import PlannerService."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")

        # The planner should use the gateway, not backend.services.planner
        with patch("gods.engine._call_gateway", new_callable=AsyncMock) as mock_gw:
            mock_gw.return_value = {
                "text": json.dumps({
                    "summary": "Add health endpoint",
                    "phases": [{"name": "Core", "tasks": [
                        {"title": "Add endpoint", "description": "GET /health", "task_type": "code"},
                    ]}],
                }),
            }

            plan = await engine.generate_plan(
                requirements="Add a health endpoint",
                project_name="Test",
            )

        assert plan is not None
        assert len(plan.get("tasks", [])) > 0 or len(plan.get("phases", [])) > 0
        mock_gw.assert_called()


# ---------------------------------------------------------------------------
# 5. Standalone decomposer
# ---------------------------------------------------------------------------

class TestStandaloneDecomposer:
    @pytest.mark.asyncio
    async def test_decompose_creates_task_rows(self):
        """Decomposer should create task rows from plan JSON."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        project_id = await engine.create_project(
            name="Test", requirements="Build X",
        )

        plan = {
            "phases": [
                {
                    "name": "Core",
                    "tasks": [
                        {"title": "Task 1", "description": "Do thing 1", "task_type": "code"},
                        {"title": "Task 2", "description": "Do thing 2", "task_type": "code",
                         "depends_on": [0]},
                    ],
                },
                {
                    "name": "Testing",
                    "tasks": [
                        {"title": "Task 3", "description": "Test things", "task_type": "code"},
                    ],
                },
            ],
        }

        task_count = await engine.decompose_plan(project_id, plan)

        assert task_count == 3

        # Verify tasks in DB
        tasks = await engine.db.fetchall(
            "SELECT id, title, wave, status FROM tasks WHERE project_id = $1 ORDER BY wave",
            (project_id,),
        )
        assert len(tasks) == 3
        assert tasks[0]["status"] == "pending"
        # Phase 2 tasks should be in a later wave
        waves = {t["wave"] for t in tasks}
        assert len(waves) >= 2  # at least 2 waves

    @pytest.mark.asyncio
    async def test_decompose_creates_deps(self):
        """Decomposer should create task_deps rows for dependencies."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        project_id = await engine.create_project(name="Test", requirements="X")

        plan = {
            "phases": [{
                "name": "Core",
                "tasks": [
                    {"title": "Task A", "task_type": "code"},
                    {"title": "Task B", "task_type": "code", "depends_on": [0]},
                ],
            }],
        }

        await engine.decompose_plan(project_id, plan)

        deps = await engine.db.fetchall(
            "SELECT task_id, depends_on FROM task_deps", (),
        )
        assert len(deps) >= 1


# ---------------------------------------------------------------------------
# 6. Project status query
# ---------------------------------------------------------------------------

class TestProjectStatus:
    @pytest.mark.asyncio
    async def test_get_project_status(self):
        """Should return project with task breakdown."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        project_id = await engine.create_project(name="Test", requirements="X")

        status = await engine.get_project_status(project_id)
        assert status["id"] == project_id
        assert status["name"] == "Test"
        assert status["status"] == "draft"
        assert "tasks" in status

    @pytest.mark.asyncio
    async def test_list_projects(self):
        """Should list all projects."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        await engine.create_project(name="P1", requirements="X")
        await engine.create_project(name="P2", requirements="Y")

        projects = await engine.list_projects()
        assert len(projects) == 2


# ---------------------------------------------------------------------------
# 7. Startup recovery — reset stuck tasks
# ---------------------------------------------------------------------------

class TestStartupRecovery:
    @pytest.mark.asyncio
    async def test_resets_running_tasks_on_startup(self):
        """Tasks stuck in 'running' from a crash should be reset to 'pending'."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        # Create project and task, manually set to running (simulating crash)
        pid = await engine.create_project(name="Recovery Test", requirements="X")
        await engine.db.execute_write(
            "INSERT INTO tasks (id, project_id, title, status, retry_count, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            ("stuck-1", pid, "Stuck Task", "running", 0, time.time(), time.time()),
        )

        # Run recovery
        await engine.recover_stuck_tasks()

        row = await engine.db.fetchone("SELECT status, retry_count FROM tasks WHERE id = $1", ("stuck-1",))
        assert row["status"] == "pending"
        assert row["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_resets_planning_projects_on_startup(self):
        """Projects stuck in 'planning' should be reset to 'draft'."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        pid = await engine.create_project(name="Planning Stuck", requirements="X")
        await engine.db.execute_write(
            "UPDATE projects SET status = $1 WHERE id = $2", ("planning", pid),
        )

        await engine.recover_stuck_tasks()

        row = await engine.db.fetchone("SELECT status FROM projects WHERE id = $1", (pid,))
        assert row["status"] == "draft"

    @pytest.mark.asyncio
    async def test_does_not_reset_completed_tasks(self):
        """Completed tasks should not be touched."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        pid = await engine.create_project(name="Done Test", requirements="X")
        await engine.db.execute_write(
            "INSERT INTO tasks (id, project_id, title, status, retry_count, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            ("done-1", pid, "Done Task", "completed", 0, time.time(), time.time()),
        )

        await engine.recover_stuck_tasks()

        row = await engine.db.fetchone("SELECT status FROM tasks WHERE id = $1", ("done-1",))
        assert row["status"] == "completed"

    @pytest.mark.asyncio
    async def test_recovery_runs_on_start(self):
        """start() should call recover_stuck_tasks."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        pid = await engine.create_project(name="Auto Recovery", requirements="X")
        await engine.db.execute_write(
            "INSERT INTO tasks (id, project_id, title, status, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            ("auto-1", pid, "Auto Stuck", "running", time.time(), time.time()),
        )

        await engine.start()
        await asyncio.sleep(0.1)
        await engine.stop()

        row = await engine.db.fetchone("SELECT status FROM tasks WHERE id = $1", ("auto-1",))
        assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# 8. Decompose attaches TaskDefinition
# ---------------------------------------------------------------------------

class TestDecomposeTaskDefinition:
    @pytest.mark.asyncio
    async def test_decompose_attaches_task_definition(self):
        """Engine decompose_plan should attach task_definition in context_json."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        project_id = await engine.create_project(name="TD Test", requirements="X")

        plan = {
            "phases": [{
                "name": "Core",
                "tasks": [
                    {"title": "Code task", "task_type": "code"},
                    {"title": "Research task", "task_type": "research"},
                ],
            }],
        }

        await engine.decompose_plan(project_id, plan)

        tasks = await engine.db.fetchall(
            "SELECT title, context_json FROM tasks WHERE project_id = $1",
            (project_id,),
        )
        for task_row in tasks:
            ctx = json.loads(task_row["context_json"])
            assert "task_definition" in ctx, f"Missing task_definition in {task_row['title']}"
            td = ctx["task_definition"]
            assert "retry_count" in td
            assert "timeout_seconds" in td

    @pytest.mark.asyncio
    async def test_decompose_respects_task_definition_override(self):
        """If task dict has task_definition key, use it instead of defaults."""
        from gods.engine import create_engine

        engine = create_engine(db_path=":memory:")
        await engine.setup_db()

        project_id = await engine.create_project(name="Override", requirements="X")

        plan = {
            "phases": [{
                "name": "Core",
                "tasks": [
                    {
                        "title": "Custom",
                        "task_type": "code",
                        "task_definition": {
                            "retry_count": 7,
                            "timeout_seconds": 5000,
                        },
                    },
                ],
            }],
        }

        await engine.decompose_plan(project_id, plan)

        task = await engine.db.fetchone(
            "SELECT context_json FROM tasks WHERE project_id = $1", (project_id,),
        )
        td = json.loads(task["context_json"])["task_definition"]
        assert td["retry_count"] == 7
        assert td["timeout_seconds"] == 5000
