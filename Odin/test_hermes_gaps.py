"""Tests for remaining hermes + pipeline gaps.

Gap 1: Subprocess tracking + kill on cancel
Gap 2: Progress heartbeat for running tasks
Gap 4: Backpressure — odin respects in-flight count
Gap 5: Shared CLI module (hermes_cli.py)
Gap 6: Cost parsing from Claude Code stream-json
Gap 7: Concurrent DB writes from background tasks
Gap 8: Tick scheduler
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

from gods.pipeline import Pipeline, Event, Emit


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db(sqlite_db):
    for ddl in [
        """CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT, status TEXT DEFAULT 'executing',
            repo_path TEXT, updated_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, project_id TEXT, title TEXT,
            description TEXT DEFAULT '', task_type TEXT DEFAULT 'code',
            status TEXT DEFAULT 'pending', model_tier TEXT DEFAULT 'claude_code',
            wave INTEGER DEFAULT 0, priority INTEGER DEFAULT 0,
            output_text TEXT, error TEXT,
            context_json TEXT DEFAULT '{}', retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3, cost_usd REAL DEFAULT 0,
            prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
            model_used TEXT, started_at REAL, completed_at REAL, updated_at REAL
        )""",
        """CREATE TABLE IF NOT EXISTS task_deps (
            task_id TEXT, depends_on TEXT, PRIMARY KEY (task_id, depends_on)
        )""",
        "DROP TABLE IF EXISTS god_relay_events",
        """CREATE TABLE god_relay_events (
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


async def _seed(db, task_id="t1", project_id="proj-1", num_tasks=1, status="pending"):
    now = time.time()
    await db.execute_write(
        "INSERT OR REPLACE INTO projects (id, name, status, repo_path, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, "Test", "executing", "/tmp/repo", now))
    for i in range(num_tasks):
        tid = task_id if num_tasks == 1 else f"t{i+1}"
        await db.execute_write(
            "INSERT OR REPLACE INTO tasks (id, project_id, title, description, task_type, "
            "status, wave, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, project_id, f"Task {tid}", f"Do {tid}", "code", status, 0, now))


# ===========================================================================
# Gap 5: Shared CLI module
# ===========================================================================

class TestSharedCLI:
    def test_build_cli_command_importable_from_cli_module(self):
        """_build_cli_command should be importable from hermes_cli."""
        from gods.handlers.hermes_cli import build_cli_command
        cmd, stdin = build_cli_command("claude_code", "test prompt", "/tmp")
        assert "claude" in cmd[0].lower() or cmd[0] == "claude"

    def test_parse_stream_event_importable_from_cli_module(self):
        """_parse_stream_event should be importable from hermes_cli."""
        from gods.handlers.hermes_cli import parse_stream_event
        result = parse_stream_event('{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}')
        assert result is not None
        assert result["type"] == "narration"

    def test_tdd_gate_importable_from_cli_module(self):
        """TDD gate should be importable from hermes_cli."""
        from gods.handlers.hermes_cli import check_tdd
        result = check_tdd("Wrote test_feature.py. All tests pass.", "code")
        assert result["passed"] is True


# ===========================================================================
# Gap 6: Cost parsing from stream-json
# ===========================================================================

class TestCostParsing:
    def test_parses_usage_event(self):
        """Should parse cost from Claude Code usage event."""
        from gods.handlers.hermes_cli import parse_stream_event
        line = '{"type":"result","subtype":"success","cost_usd":0.0523,"duration_ms":45200,"is_error":false,"num_turns":3,"session_id":"abc123"}'
        event = parse_stream_event(line)
        assert event is not None
        assert event["type"] == "result"
        assert event["cost_usd"] == 0.0523

    def test_accumulates_cost_across_events(self):
        """Cost tracker should accumulate across multiple usage events."""
        from gods.handlers.hermes_cli import CostTracker
        tracker = CostTracker()
        tracker.add_usage({"input_tokens": 1000, "output_tokens": 500})
        tracker.add_usage({"input_tokens": 2000, "output_tokens": 1000})
        assert tracker.prompt_tokens == 3000
        assert tracker.completion_tokens == 1500

    def test_cost_from_result_event(self):
        """CostTracker should extract cost_usd from result events."""
        from gods.handlers.hermes_cli import CostTracker
        tracker = CostTracker()
        tracker.add_result({"cost_usd": 0.05, "num_turns": 3})
        assert tracker.cost_usd == 0.05


# ===========================================================================
# Gap 1: Subprocess tracking + kill
# ===========================================================================

class TestSubprocessTracking:
    @pytest.mark.asyncio
    async def test_cancel_kills_subprocess(self, db):
        """cancel() should terminate the actual subprocess, not just the asyncio task."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_proc.kill = MagicMock()
        mock_proc.terminate = MagicMock()

        async def fake_cli(**kwargs):
            # Register the fake process
            runner._processes[kwargs.get("task_id", "t1")] = mock_proc
            await asyncio.sleep(999)

        patcher = patch.object(runner, "_run_cli_background", side_effect=fake_cli)
        patcher.start()

        await runner.handle_dispatch(Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin"))

        await asyncio.sleep(0.1)
        await runner.cancel("t1")

        patcher.stop()
        await runner.shutdown(timeout=1.0)

        # Process should have been terminated
        assert mock_proc.terminate.called or mock_proc.kill.called


# ===========================================================================
# Gap 2: Progress heartbeat
# ===========================================================================

class TestProgressHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_emitted_for_running_task(self, db):
        """Running tasks should emit periodic heartbeat events to relay."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4, heartbeat_interval=0.2)
        await _seed(db)

        async def slow_cli(**kwargs):
            await asyncio.sleep(1.0)
            return {"output": "Done.", "cost_usd": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "model_used": "claude", "narration": []}

        with patch.object(runner, "_run_cli_background", side_effect=slow_cli):
            await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
            }, "odin"))

            # Wait for heartbeats to fire
            await asyncio.sleep(0.7)
            await runner.shutdown(timeout=3.0)

        rows = await db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = ?",
            ("heartbeat",))
        assert len(rows) >= 2, f"Expected at least 2 heartbeats, got {len(rows)}"


# ===========================================================================
# Gap 4: Backpressure — odin checks in-flight
# ===========================================================================

class TestBackpressure:
    @pytest.mark.asyncio
    async def test_odin_skips_dispatch_when_slots_reported_full(self, db):
        """odin_dispatch should check running task count and not over-dispatch."""
        from gods.handlers.odin import odin_dispatch

        await _seed(db, num_tasks=3)
        # Set 2 tasks to running — simulating hermes already executing them
        await db.execute_write("UPDATE tasks SET status = 'running' WHERE id IN ('t1', 't2')")

        event = Event("tick", {"project_id": "proj-1", "max_concurrent": 2}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock:
            mock.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}
            emits = await odin_dispatch(event, db)

        commands = [e for e in (emits or []) if e.event_type == "dispatch_command"]
        assert len(commands) == 0, "Should not dispatch when slots are full"

    @pytest.mark.asyncio
    async def test_odin_dispatches_when_slots_available(self, db):
        """odin_dispatch should dispatch when running count is below max."""
        from gods.handlers.odin import odin_dispatch

        await _seed(db, num_tasks=3)
        await db.execute_write("UPDATE tasks SET status = 'running' WHERE id = 't1'")

        event = Event("tick", {"project_id": "proj-1", "max_concurrent": 3}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock:
            mock.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}
            emits = await odin_dispatch(event, db)

        commands = [e for e in (emits or []) if e.event_type == "dispatch_command"]
        assert len(commands) >= 1, "Should dispatch when slots are available"


# ===========================================================================
# Gap 7: Concurrent DB writes
# ===========================================================================

class TestConcurrentDBWrites:
    @pytest.mark.asyncio
    async def test_two_tasks_completing_simultaneously(self, db):
        """Two background tasks writing to relay at the same time shouldn't conflict."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db, num_tasks=2)

        async def fast_cli(**kwargs):
            await asyncio.sleep(0.1)
            tid = kwargs.get("task_id", "?")
            return {"output": f"Done {tid}.", "cost_usd": 0.01, "prompt_tokens": 100,
                    "completion_tokens": 50, "model_used": "claude", "narration": []}

        with patch.object(runner, "_run_cli_background", side_effect=fast_cli):
            await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
            }, "odin"))
            await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t2", "project_id": "proj-1", "provider": "gemini_cli",
            }, "odin"))

            await asyncio.sleep(0.5)
            await runner.shutdown(timeout=3.0)

        # Both should have written worker_events
        rows = await db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = ?",
            ("worker_event",))
        assert len(rows) == 2
        task_ids = {json.loads(r["payload"])["task_id"] for r in rows}
        assert task_ids == {"t1", "t2"}


# ===========================================================================
# Gap 8: Tick scheduler
# ===========================================================================

class TestTickScheduler:
    @pytest.mark.asyncio
    async def test_scheduler_injects_tick_events(self, db):
        """Pipeline with scheduler should auto-inject tick events."""
        from gods.pipeline import Pipeline

        pipeline = Pipeline(db)

        ticks_received = []

        async def track_tick(event, db):
            ticks_received.append(event)
            return None

        pipeline.register("tick", track_tick)

        # Start scheduler with fast interval
        pipeline.start_scheduler(interval=0.1)

        # Tick the pipeline multiple times to pick up scheduler events
        for _ in range(5):
            await asyncio.sleep(0.1)
            await pipeline.tick()

        pipeline.stop_scheduler()

        # Should have received multiple ticks
        assert len(ticks_received) >= 2

    @pytest.mark.asyncio
    async def test_scheduler_ticks_are_in_relay(self, db):
        """Scheduler-injected ticks should appear in the relay table.
        With dedup, cursor must advance between ticks (via pipeline.tick())."""
        from gods.pipeline import Pipeline

        pipeline = Pipeline(db)
        pipeline.register("tick", lambda e, d: None)  # dummy handler

        pipeline.start_scheduler(interval=0.1)
        # Process first tick to advance cursor, allowing second tick
        await asyncio.sleep(0.15)
        await pipeline.tick()
        await asyncio.sleep(0.15)
        pipeline.stop_scheduler()

        rows = await db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = ? AND source = ?",
            ("tick", "scheduler"))
        assert len(rows) >= 2
