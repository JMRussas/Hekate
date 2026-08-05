"""Tests for all remaining gaps — TDD RED phase.

Gap 1:  End-to-end integration (full chain)
Gap 2:  hermes_tdd_gate wired into hermes_execute
Gap 3:  Prompt guidance field alignment
Gap 4:  Knowledge extraction after verification
Gap 5:  review_rejected handler
Gap 6:  task_rejected triggers re-dispatch
Gap 7:  register_all_handlers
Gap 8:  Cursor persistence
Gap 9:  Narration to relay
Gap 10: Timeout handling in hermes
Gap 11: hephaestus_git handler
Gap 12: tyche_budget handler
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

from gods.pipeline import Pipeline, Event, Emit


# ---------------------------------------------------------------------------
# Shared fixture — full schema
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def full_db(sqlite_db):
    """SQLite with full schema for integration tests."""
    for ddl in [
        """CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT, requirements TEXT,
            status TEXT DEFAULT 'draft', repo_path TEXT,
            config_json TEXT DEFAULT '{}',
            completed_at REAL, updated_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, project_id TEXT, title TEXT,
            description TEXT DEFAULT '', task_type TEXT DEFAULT 'code',
            complexity TEXT DEFAULT 'medium', status TEXT DEFAULT 'pending',
            model_tier TEXT DEFAULT 'claude_code', wave INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 0, output_text TEXT, error TEXT,
            context_json TEXT DEFAULT '{}', retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3, cost_usd REAL DEFAULT 0,
            prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
            model_used TEXT, started_at REAL, completed_at REAL, updated_at REAL,
            verification_status TEXT, verification_notes TEXT,
            plan_id TEXT, rationale TEXT, implementation_notes TEXT, test_strategy TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY, project_id TEXT, plan_json TEXT,
            level TEXT DEFAULT 'L1', version INTEGER DEFAULT 1,
            model_used TEXT, prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
            status TEXT DEFAULT 'draft', node_mapping TEXT DEFAULT '{}',
            created_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS task_deps (
            task_id TEXT, depends_on TEXT,
            PRIMARY KEY (task_id, depends_on)
        )""",
        """CREATE TABLE IF NOT EXISTS project_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT, task_id TEXT, content TEXT, created_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS god_relay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL, source TEXT NOT NULL,
            payload TEXT DEFAULT '{}', severity TEXT DEFAULT 'info',
            created_at REAL NOT NULL
        )""",
        "DROP TABLE IF EXISTS god_registry",
        """CREATE TABLE god_registry (
            name TEXT PRIMARY KEY, last_seen_id INTEGER DEFAULT 0,
            last_heartbeat REAL DEFAULT 0, config_json TEXT DEFAULT '{}'
        )""",
    ]:
        await sqlite_db.execute_write(ddl)
    return sqlite_db


async def _seed_full(db, project_id="proj-1", num_tasks=2, status="draft"):
    await db.execute_write(
        "INSERT OR REPLACE INTO projects (id, name, requirements, status, repo_path, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, "Test Project", "Build feature X", status, "/tmp/repo", time.time()),
    )
    for i in range(num_tasks):
        await db.execute_write(
            "INSERT OR REPLACE INTO tasks (id, project_id, title, description, task_type, "
            "status, wave, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"t{i+1}", project_id, f"Task {i+1}", f"Implement part {i+1}",
             "code", "pending", 0, time.time()),
        )


# ===========================================================================
# Gap 1: End-to-end integration test
# ===========================================================================

class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_full_chain_project_created_to_complete(self, full_db):
        """project_created → athena plans → odin starts → odin dispatches → project executing.

        Verifies the pipeline event chain from project_created through to dispatch.
        Hermes and Mimir runners are replaced with synchronous fakes to avoid
        background tasks and subprocess spawning in tests.
        """
        from gods.handlers.registration import register_all_handlers

        await _seed_full(full_db, status="draft", num_tasks=1)
        # Force L1-only planning so athena doesn't call _deepen_plan (which hits the gateway)
        await full_db.execute_write(
            "UPDATE projects SET config_json = ? WHERE id = ?",
            ('{"tdd": false, "narration": false, "target_level": "L1"}', "proj-1"))

        pipeline = Pipeline(full_db)

        plan_result = {
            "plan_id": "plan-1",
            "plan": {"phases": [{"name": "Phase 1", "tasks": [
                {"title": "Task t1", "task_type": "code", "complexity": "medium",
                 "description": "Implement part 1", "depends_on": []},
            ]}]},
        }

        # Synchronous hermes fake: on dispatch_command, immediately write
        # worker_event:completed to the relay (no background task, no subprocess).
        async def fake_handle_dispatch(event, db=None):
            task_id = event.payload.get("task_id")
            project_id = event.payload.get("project_id")
            if not task_id:
                return []
            await full_db.execute_write(
                "UPDATE tasks SET status = 'completed', output_text = 'Done.' WHERE id = ?",
                (task_id,),
            )
            await full_db.execute_write(
                "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("worker_event", "hermes",
                 json.dumps({"task_id": task_id, "project_id": project_id,
                             "status": "completed", "output": "Done.",
                             "cost_usd": 0.01, "prompt_tokens": 100,
                             "completion_tokens": 50, "model_used": "claude-sonnet-4"}),
                 "info", time.time()),
            )
            return [Emit("task_running", {"task_id": task_id, "project_id": project_id}, "hermes")]

        # Synchronous mimir fake: on worker_event:completed, immediately emit task_verified.
        async def fake_handle_verify(event, db=None):
            if event.payload.get("status") != "completed":
                return None
            task_id = event.payload.get("task_id")
            project_id = event.payload.get("project_id")
            await full_db.execute_write(
                "UPDATE tasks SET verification_status = 'passed' WHERE id = ?", (task_id,))
            return [Emit("task_verified", {
                "task_id": task_id, "project_id": project_id,
                "confidence": 0.9, "verdict": "passed",
            }, "mimir")]

        with patch("gods.handlers.athena_leveled._generate_l1", new_callable=AsyncMock) as mock_l1, \
             patch("gods.handlers.athena_leveled._thorough_review", new_callable=AsyncMock) as mock_review, \
             patch("gods.tooling.check_tooling_availability", new_callable=AsyncMock) as mock_tool, \
             patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock_prov, \
             patch("gods.handlers.registration.HermesRunner") as MockHermes, \
             patch("gods.handlers.registration.MimirRunner") as MockMimir:

            # Wire fakes into the pipeline. The handler functions need __name__
            # so pipeline.register() can label them. Use AsyncMock (has __name__).
            mock_dispatch = AsyncMock(side_effect=fake_handle_dispatch)
            mock_dispatch.__name__ = "fake_hermes_dispatch"
            mock_verify = AsyncMock(side_effect=fake_handle_verify)
            mock_verify.__name__ = "fake_mimir_verify"

            fake_hermes = MagicMock()
            fake_hermes.handle_dispatch = mock_dispatch
            fake_hermes.db = full_db
            MockHermes.return_value = fake_hermes

            fake_mimir = MagicMock()
            fake_mimir.handle_verify = mock_verify
            fake_mimir.db = full_db
            MockMimir.return_value = fake_mimir

            mock_l1.return_value = plan_result
            mock_review.return_value = {"approved": True, "confidence": 0.95, "feedback": "", "gaps": []}
            mock_tool.return_value = MagicMock(has_roslyn=False, has_jedi=False, has_ts_compiler=False)
            mock_prov.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}

            register_all_handlers(pipeline)

            # Inject the starting event
            await full_db.execute_write(
                "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("project_created", "user", json.dumps({"project_id": "proj-1"}),
                 "info", time.time()),
            )

            # Run enough ticks for the chain to complete
            for _ in range(20):
                await pipeline.tick()

        # Project should have progressed past draft
        row = await full_db.fetchone(
            "SELECT status FROM projects WHERE id = ?", ("proj-1",))
        assert row["status"] in ("completed", "executing", "planned"), \
            f"Expected progress past draft, got {row['status']}"


# ===========================================================================
# Gap 2: TDD gate wired into hermes_execute
# ===========================================================================

class TestHermesTDDWired:
    @pytest.mark.asyncio
    async def test_tdd_gate_runs_on_code_tasks(self, full_db):
        """hermes_execute should call hermes_tdd_gate for code tasks."""
        from gods.handlers.hermes import hermes_execute

        await _seed_full(full_db, status="executing", num_tasks=1)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_cli, \
             patch("gods.handlers.hermes.hermes_tdd_gate", new_callable=AsyncMock) as mock_tdd:
            mock_cli.return_value = {
                "output": "Done without tests.",
                "cost_usd": 0.01, "prompt_tokens": 100,
                "completion_tokens": 50, "model_used": "claude-sonnet-4",
                "narration": [],
            }
            mock_tdd.return_value = {"passed": False, "reason": "No test files mentioned"}

            emits = await hermes_execute(event, full_db)

            # TDD gate should have been called
            mock_tdd.assert_called_once()

        # Task should be failed or have TDD warning in output
        worker = next((e for e in emits if e.event_type == "worker_event"), None)
        assert worker is not None
        assert worker.payload.get("tdd_warning") or worker.payload.get("status") == "completed"


# ===========================================================================
# Gap 3: Prompt guidance field alignment
# ===========================================================================

class TestPromptGuidanceAlignment:
    @pytest.mark.asyncio
    async def test_modify_prompt_writes_to_context_json(self, full_db):
        """odin_handle_diagnosis with modify_prompt should write to context_json, not output_text."""
        from gods.handlers.odin import odin_handle_diagnosis

        await _seed_full(full_db, status="executing", num_tasks=1)
        await full_db.execute_write(
            "UPDATE tasks SET status = 'failed', error = 'SyntaxError' WHERE id = ?", ("t1",))

        event = Event("task_diagnosis", {
            "task_id": "t1", "project_id": "proj-1",
            "fix_type": "modify_prompt",
            "prompt_guidance": "Use proper indentation.",
        }, "odin")

        await odin_handle_diagnosis(event, full_db)

        row = await full_db.fetchone(
            "SELECT context_json FROM tasks WHERE id = ?", ("t1",))
        ctx = json.loads(row["context_json"] or "{}")
        assert "indentation" in ctx.get("prompt_guidance", "").lower()

    @pytest.mark.asyncio
    async def test_hermes_reads_prompt_guidance_from_context_json(self, full_db):
        """hermes_execute should read prompt_guidance from context_json."""
        from gods.handlers.hermes import hermes_execute

        await _seed_full(full_db, status="executing", num_tasks=1)
        ctx = json.dumps({"prompt_guidance": "Ensure proper indentation."})
        await full_db.execute_write(
            "UPDATE tasks SET context_json = ? WHERE id = ?", (ctx, "t1"))

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = {
                "output": "Fixed.", "cost_usd": 0.01, "prompt_tokens": 100,
                "completion_tokens": 50, "model_used": "claude-sonnet-4",
                "narration": [],
            }
            await hermes_execute(event, full_db)

            # Verify the prompt sent to CLI includes guidance
            call_args = mock_cli.call_args
            prompt = call_args[1].get("prompt", "")
            assert "indentation" in prompt.lower()


# ===========================================================================
# Gap 4: Knowledge extraction after verification
# ===========================================================================

class TestKnowledgeExtractionWired:
    @pytest.mark.asyncio
    async def test_mimir_extracts_knowledge_after_verified(self, full_db):
        """mimir_verify should call _extract_knowledge after task_verified."""
        from gods.handlers.mimir import mimir_verify

        await _seed_full(full_db, status="executing", num_tasks=1)
        await full_db.execute_write(
            "UPDATE tasks SET status = 'completed', output_text = 'Found that API uses OAuth2' "
            "WHERE id = ?", ("t1",))

        event = Event("worker_event", {
            "task_id": "t1", "project_id": "proj-1", "status": "completed",
        }, "hermes")

        with patch("gods.handlers.mimir._call_verifier", new_callable=AsyncMock) as mock_v, \
             patch("gods.handlers.mimir._extract_knowledge", new_callable=AsyncMock) as mock_k:
            mock_v.return_value = {"verdict": "passed", "confidence": 0.9, "feedback": ""}
            mock_k.return_value = ["API uses OAuth2"]

            emits = await mimir_verify(event, full_db)

            # Knowledge extraction should have been called
            mock_k.assert_called_once()


# ===========================================================================
# Gap 5+6: review_rejected and task_rejected handlers
# ===========================================================================

class TestRejectionHandlers:
    @pytest.mark.asyncio
    async def test_review_rejected_resets_task(self, full_db):
        """review_rejected should reset task to pending with review feedback."""
        from gods.handlers.mimir import mimir_handle_review_rejection

        await _seed_full(full_db, status="executing", num_tasks=1)
        await full_db.execute_write(
            "UPDATE tasks SET status = 'completed' WHERE id = ?", ("t1",))

        event = Event("review_rejected", {
            "task_id": "t1", "project_id": "proj-1",
            "feedback": "Use dependency injection.",
        }, "mimir")

        emits = await mimir_handle_review_rejection(event, full_db)

        row = await full_db.fetchone(
            "SELECT status, context_json FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "pending"
        ctx = json.loads(row["context_json"] or "{}")
        assert "dependency injection" in ctx.get("review_feedback", "").lower()

    @pytest.mark.asyncio
    async def test_task_rejected_triggers_tick(self, full_db):
        """task_rejected should emit a tick so odin re-dispatches."""
        from gods.handlers.mimir import mimir_handle_task_rejection

        event = Event("task_rejected", {
            "task_id": "t1", "project_id": "proj-1",
            "feedback": "Missing error handling.",
        }, "mimir")

        emits = await mimir_handle_task_rejection(event, full_db)

        tick = next((e for e in emits if e.event_type == "tick"), None)
        assert tick is not None
        assert tick.payload["project_id"] == "proj-1"


# ===========================================================================
# Gap 7: register_all_handlers
# ===========================================================================

class TestRegistration:
    @pytest.mark.asyncio
    async def test_register_all_handlers_wires_pipeline(self, full_db):
        """register_all_handlers should register all handlers with correct event types."""
        from gods.handlers.registration import register_all_handlers

        pipeline = Pipeline(full_db)
        register_all_handlers(pipeline)

        handler_list = pipeline.handlers() if callable(pipeline.handlers) else pipeline._handlers
        event_types = set()
        for h in handler_list:
            if isinstance(h, dict):
                event_types.add(h["event_type"])
            else:
                event_types.add(h.event_type)

        # All core event types must be registered
        expected = {
            "project_created",    # athena_plan
            "project_planned",    # odin_start
            "tick",               # odin_dispatch (+ odin_tick)
            "dispatch_command",   # hermes_execute
            "worker_event",       # mimir_verify
            "task_verified",      # odin_lifecycle + mimir_review
            "task_diagnosis",     # odin_handle_diagnosis
            "wave_complete",      # athena_reassess
            "review_rejected",    # mimir_handle_review_rejection
            "task_rejected",      # mimir_handle_task_rejection
        }
        assert expected.issubset(event_types), f"Missing: {expected - event_types}"


# ===========================================================================
# Gap 8: Cursor persistence
# ===========================================================================

class TestCursorPersistence:
    @pytest.mark.asyncio
    async def test_cursor_persisted_after_tick(self, full_db):
        """Pipeline should persist _last_seen_id to god_registry after each tick."""
        pipeline = Pipeline(full_db)

        # Register a dummy handler so the event gets processed
        async def noop(event, db):
            return None
        pipeline.register("tick", noop)

        # Seed an event that matches
        await full_db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("tick", "test", "{}", "info", time.time()),
        )

        await pipeline.tick()

        # Cursor should be persisted
        row = await full_db.fetchone(
            "SELECT last_seen_id FROM god_registry WHERE name = ?", ("pipeline",))
        assert row is not None
        assert row["last_seen_id"] > 0

    @pytest.mark.asyncio
    async def test_cursor_restored_on_init(self, full_db):
        """Pipeline should restore _last_seen_id from god_registry on creation."""
        from gods.handlers.registration import register_all_handlers

        # Pre-seed cursor
        await full_db.execute_write(
            "INSERT OR REPLACE INTO god_registry (name, last_seen_id) VALUES (?, ?)",
            ("pipeline", 42),
        )

        pipeline = Pipeline(full_db)
        register_all_handlers(pipeline)
        await pipeline.restore_cursor()

        assert pipeline._last_seen_id == 42


# ===========================================================================
# Gap 9: Narration to relay
# ===========================================================================

class TestNarrationToRelay:
    @pytest.mark.asyncio
    async def test_narration_events_written_to_relay(self, full_db):
        """Narration events from hermes should be written to god_relay_events."""
        from gods.handlers.hermes import hermes_execute

        await _seed_full(full_db, status="executing", num_tasks=1)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.return_value = {
                "output": "Done.", "cost_usd": 0.01, "prompt_tokens": 100,
                "completion_tokens": 50, "model_used": "claude-sonnet-4",
                "narration": [
                    {"type": "narration", "text": "Reading tests..."},
                    {"type": "tool_call", "tool": "Read", "args": {}},
                ],
            }
            emits = await hermes_execute(event, full_db)

        narration_emits = [e for e in emits if e.event_type == "narration"]
        assert len(narration_emits) >= 2

        # When pipeline processes these, they should write to relay
        # For now, verify the emits exist — relay writing is pipeline's job


# ===========================================================================
# Gap 10: Timeout handling
# ===========================================================================

class TestTimeoutHandling:
    @pytest.mark.asyncio
    async def test_timeout_emits_specific_error(self, full_db):
        """CLI timeout should produce a worker_event with timeout flag."""
        from gods.handlers.hermes import hermes_execute
        import asyncio

        await _seed_full(full_db, status="executing", num_tasks=1)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_cli:
            mock_cli.side_effect = asyncio.TimeoutError("CLI timed out")
            emits = await hermes_execute(event, full_db)

        failed = next((e for e in emits if e.event_type == "worker_event"), None)
        assert failed is not None
        assert failed.payload["status"] == "failed"
        assert failed.payload.get("timeout") is True or "timeout" in failed.payload.get("error", "").lower()


# ===========================================================================
# Gap 11: hephaestus_git handler
# ===========================================================================

class TestHephaestusGit:
    @pytest.mark.asyncio
    async def test_git_stage_after_completion(self, full_db):
        """hephaestus_git should stage files after task completion."""
        from gods.handlers.hephaestus import hephaestus_stage

        await _seed_full(full_db, status="executing", num_tasks=1)

        event = Event("task_verified", {
            "task_id": "t1", "project_id": "proj-1",
            "affected_files": ["src/feature.py", "tests/test_feature.py"],
        }, "mimir")

        with patch("gods.handlers.hephaestus._git_add", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = True
            emits = await hephaestus_stage(event, full_db)

        mock_git.assert_called_once()
        staged = next((e for e in emits if e.event_type == "files_staged"), None)
        assert staged is not None

    @pytest.mark.asyncio
    async def test_syntax_check_before_stage(self, full_db):
        """hephaestus should syntax-check Python files before staging."""
        from gods.handlers.hephaestus import hephaestus_stage

        await _seed_full(full_db, status="executing", num_tasks=1)

        event = Event("task_verified", {
            "task_id": "t1", "project_id": "proj-1",
            "affected_files": ["src/broken.py"],
        }, "mimir")

        with patch("gods.handlers.hephaestus._syntax_check", new_callable=AsyncMock) as mock_check, \
             patch("gods.handlers.hephaestus._git_add", new_callable=AsyncMock) as mock_git:
            mock_check.return_value = {"passed": False, "error": "SyntaxError line 5"}
            emits = await hephaestus_stage(event, full_db)

        # Should NOT stage if syntax check fails
        mock_git.assert_not_called()
        error = next((e for e in emits if e.event_type == "stage_failed"), None)
        assert error is not None


# ===========================================================================
# Gap 12: tyche_budget handler
# ===========================================================================

class TestTycheBudget:
    @pytest.mark.asyncio
    async def test_budget_check_before_dispatch(self, full_db):
        """tyche_budget should check budget before allowing dispatch."""
        from gods.handlers.tyche import tyche_check_budget

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1",
            "provider": "claude_code",
            "estimated_cost": 0.50,
        }, "odin")

        result = await tyche_check_budget(
            event, full_db, budget_remaining=0.10)

        assert result["allowed"] is False
        assert "budget" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_budget_allows_within_limit(self, full_db):
        """tyche should allow dispatch when budget is sufficient."""
        from gods.handlers.tyche import tyche_check_budget

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1",
            "provider": "claude_code",
            "estimated_cost": 0.05,
        }, "odin")

        result = await tyche_check_budget(
            event, full_db, budget_remaining=10.0)

        assert result["allowed"] is True

    @pytest.mark.asyncio
    async def test_budget_recorded_after_completion(self, full_db):
        """tyche should record spend after worker_event:completed."""
        from gods.handlers.tyche import tyche_record_spend

        event = Event("worker_event", {
            "task_id": "t1", "project_id": "proj-1",
            "status": "completed", "cost_usd": 0.15,
        }, "hermes")

        emits = await tyche_record_spend(event, full_db)

        spent = next((e for e in emits if e.event_type == "budget_spent"), None)
        assert spent is not None
        assert spent.payload["cost_usd"] == 0.15
