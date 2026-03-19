"""Tests for async hermes execution.

Hermes splits into two parts:
  1. hermes_execute: receives dispatch_command, launches background task, returns immediately
  2. HermesRunner: manages in-flight tasks, monitors subprocesses, writes results to relay

Test categories:
  - Launch: immediate return, DB state, in-flight tracking, dedup
  - Monitor: completion → relay write, failure → relay write, timeout, narration
  - Concurrency: max slots, slot release, multi-provider parallel
  - Cleanup: cancellation, shutdown, background crash recovery
  - Integration: full pipeline chain with async hermes
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
    """Full schema DB for async hermes tests."""
    for ddl in [
        """CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT, requirements TEXT,
            status TEXT DEFAULT 'executing', repo_path TEXT,
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
            model_used TEXT, started_at REAL, completed_at REAL, updated_at REAL
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


async def _seed(db, task_id="t1", project_id="proj-1", status="pending",
                model_tier="claude_code", num_tasks=1):
    now = time.time()
    await db.execute_write(
        "INSERT OR REPLACE INTO projects (id, name, status, repo_path, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, "Test", "executing", "/tmp/repo", now),
    )
    for i in range(num_tasks):
        tid = task_id if num_tasks == 1 else f"t{i+1}"
        await db.execute_write(
            "INSERT OR REPLACE INTO tasks (id, project_id, title, description, task_type, "
            "status, model_tier, wave, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, project_id, f"Task {tid}", f"Do thing {tid}", "code",
             status, model_tier, 0, now),
        )


# ===========================================================================
# 1. LAUNCH — immediate return, DB state, in-flight tracking
# ===========================================================================

class TestAsyncHermesLaunch:
    @pytest.mark.asyncio
    async def test_returns_task_running_immediately(self, db):
        """dispatch_command → returns task_running emit, NOT worker_event."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        # Mock _run_cli to simulate a slow subprocess
        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            # Don't resolve immediately — simulates long-running CLI
            async def _hang(**kw):
                await asyncio.sleep(999)
            mock.side_effect = _hang
            emits = await runner.handle_dispatch(event)

        # Should return task_running, not worker_event
        assert len(emits) >= 1
        running = next((e for e in emits if e.event_type == "task_running"), None)
        assert running is not None
        assert running.payload["task_id"] == "t1"

        # Should NOT have worker_event (that comes later from monitor)
        worker = next((e for e in emits if e.event_type == "worker_event"), None)
        assert worker is None

        await runner.shutdown(timeout=0.1)

    @pytest.mark.asyncio
    async def test_task_set_to_running_in_db(self, db):
        """Task status should be 'running' in DB before return."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            async def _hang(**kw):
                await asyncio.sleep(999)
            mock.side_effect = _hang
            await runner.handle_dispatch(event)

        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "running"

        await runner.shutdown(timeout=0.1)

    @pytest.mark.asyncio
    async def test_in_flight_tracking(self, db):
        """Task should be tracked in in_flight set after launch."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            async def _hang(**kw):
                await asyncio.sleep(999)
            mock.side_effect = _hang
            await runner.handle_dispatch(event)

        assert "t1" in runner.in_flight

        await runner.shutdown(timeout=0.1)

    @pytest.mark.asyncio
    async def test_duplicate_dispatch_rejected(self, db):
        """Second dispatch for same task → rejected, not launched twice."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            async def _hang(**kw):
                await asyncio.sleep(999)
            mock.side_effect = _hang
            emits1 = await runner.handle_dispatch(event)
            emits2 = await runner.handle_dispatch(event)

        # First should launch
        running1 = next((e for e in emits1 if e.event_type == "task_running"), None)
        assert running1 is not None

        # Second should be rejected
        dup = next((e for e in emits2 if e.event_type == "task_already_running"), None)
        assert dup is not None

        await runner.shutdown(timeout=0.1)

    @pytest.mark.asyncio
    async def test_non_pending_task_skipped(self, db):
        """Task not in pending status → skip."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db, status="completed")

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        emits = await runner.handle_dispatch(event)

        skipped = next((e for e in emits if e.payload.get("status") == "skipped"), None)
        assert skipped is not None

        await runner.shutdown(timeout=0.1)

    @pytest.mark.asyncio
    async def test_missing_task_returns_error(self, db):
        """Dispatch for nonexistent task → hermes_error."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)

        event = Event("dispatch_command", {
            "task_id": "ghost", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        emits = await runner.handle_dispatch(event)

        error = next((e for e in emits if e.event_type == "hermes_error"), None)
        assert error is not None

        await runner.shutdown(timeout=0.1)


# ===========================================================================
# 2. MONITOR — background task writes results to relay
# ===========================================================================

class TestAsyncHermesMonitor:
    @pytest.mark.asyncio
    async def test_completion_writes_worker_event_to_relay(self, db):
        """Background task should write worker_event:completed to relay table."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "output": "Implemented feature. Wrote test_feature.py. All tests pass.",
                "cost_usd": 0.05, "prompt_tokens": 500, "completion_tokens": 200,
                "model_used": "claude-sonnet-4", "narration": [],
            }
            await runner.handle_dispatch(event)

            # Wait for background task to complete
            await asyncio.sleep(0.1)
            await runner.shutdown(timeout=2.0)

        # Check relay table for worker_event
        rows = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = ?",
            ("worker_event",))
        assert len(rows) >= 1
        payload = json.loads(rows[0][0] if len(rows[0]) == 1 else rows[0][1])
        assert payload["status"] == "completed"
        assert payload["task_id"] == "t1"
        assert payload["cost_usd"] == 0.05

    @pytest.mark.asyncio
    async def test_failure_writes_worker_event_to_relay(self, db):
        """CLI crash → worker_event:failed written to relay."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            mock.side_effect = RuntimeError("CLI exited 0xC0000005: access violation")
            await runner.handle_dispatch(event)
            await asyncio.sleep(0.1)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = ?",
            ("worker_event",))
        assert len(rows) >= 1
        payload = json.loads(rows[0][0] if len(rows[0]) == 1 else rows[0][1])
        assert payload["status"] == "failed"
        assert "access violation" in payload["error"]

    @pytest.mark.asyncio
    async def test_timeout_writes_failed_with_flag(self, db):
        """CLI timeout → worker_event:failed with timeout=true."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            mock.side_effect = asyncio.TimeoutError("600s timeout")
            await runner.handle_dispatch(event)
            await asyncio.sleep(0.1)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = ?",
            ("worker_event",))
        assert len(rows) >= 1
        payload = json.loads(rows[0][0] if len(rows[0]) == 1 else rows[0][1])
        assert payload["status"] == "failed"
        assert payload["timeout"] is True

    @pytest.mark.asyncio
    async def test_db_updated_after_completion(self, db):
        """Task row should have output, cost, tokens after background completes."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "output": "Done.", "cost_usd": 0.12, "prompt_tokens": 3000,
                "completion_tokens": 1500, "model_used": "claude-opus-4",
                "narration": [],
            }
            await runner.handle_dispatch(event)
            await asyncio.sleep(0.1)
            await runner.shutdown(timeout=2.0)

        row = await db.fetchone(
            "SELECT status, output_text, cost_usd, prompt_tokens, completion_tokens, model_used "
            "FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "completed"
        assert row[1] == "Done."
        assert row[2] == 0.12
        assert row[3] == 3000
        assert row[4] == 1500
        assert row[5] == "claude-opus-4"

    @pytest.mark.asyncio
    async def test_in_flight_cleared_after_completion(self, db):
        """Task removed from in_flight after background completes."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "output": "Done.", "cost_usd": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0, "model_used": "claude", "narration": [],
            }
            await runner.handle_dispatch(event)
            assert "t1" in runner.in_flight

            await asyncio.sleep(0.1)
            await runner.shutdown(timeout=2.0)

        assert "t1" not in runner.in_flight

    @pytest.mark.asyncio
    async def test_empty_output_writes_failed(self, db):
        """CLI returns empty output → worker_event:failed."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "output": "", "cost_usd": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0, "model_used": "claude", "narration": [],
            }
            await runner.handle_dispatch(event)
            await asyncio.sleep(0.1)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = ?",
            ("worker_event",))
        assert len(rows) >= 1
        payload = json.loads(rows[0][0] if len(rows[0]) == 1 else rows[0][1])
        assert payload["status"] == "failed"
        assert "empty" in payload["error"].lower()

    @pytest.mark.asyncio
    async def test_narration_written_to_relay_during_execution(self, db):
        """Narration events should be written to relay as they happen."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "output": "Done.", "cost_usd": 0.01, "prompt_tokens": 100,
                "completion_tokens": 50, "model_used": "claude",
                "narration": [
                    {"type": "narration", "text": "Reading existing code..."},
                    {"type": "tool_call", "tool": "Read", "args": {"path": "main.py"}},
                    {"type": "narration", "text": "Writing implementation..."},
                ],
            }
            await runner.handle_dispatch(event)
            await asyncio.sleep(0.1)
            await runner.shutdown(timeout=2.0)

        rows = await db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = ?",
            ("narration",))
        assert len(rows) >= 2


# ===========================================================================
# 3. CONCURRENCY — max slots, slot release, multi-provider
# ===========================================================================

class TestAsyncHermesConcurrency:
    @pytest.mark.asyncio
    async def test_max_concurrent_enforced(self, db):
        """With max_concurrent=2 and 3 dispatches → 2 launch, 1 rejected."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=2)
        await _seed(db, num_tasks=3)

        async def hang_forever(**kwargs):
            await asyncio.sleep(999)
            return {"output": "", "cost_usd": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "model_used": "", "narration": []}

        with patch.object(runner, "_run_cli_background", side_effect=hang_forever):
            emits1 = await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
            }, "odin"))
            emits2 = await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t2", "project_id": "proj-1", "provider": "gemini_cli",
            }, "odin"))
            emits3 = await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t3", "project_id": "proj-1", "provider": "claude_code",
            }, "odin"))

        # First two should launch
        assert any(e.event_type == "task_running" for e in emits1)
        assert any(e.event_type == "task_running" for e in emits2)

        # Third should be rejected — slots full
        assert any(e.event_type == "slots_full" for e in emits3)

        assert len(runner.in_flight) == 2

        await runner.shutdown(timeout=0.5)

    @pytest.mark.asyncio
    async def test_slot_freed_after_completion(self, db):
        """After task completes, slot opens for next dispatch."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=1)
        await _seed(db, num_tasks=2)

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "output": "Done.", "cost_usd": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0, "model_used": "claude", "narration": [],
            }

            # First dispatch — fills the slot
            emits1 = await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
            }, "odin"))

            # Wait for completion
            await asyncio.sleep(0.2)

            # Second dispatch — slot should be free
            emits2 = await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t2", "project_id": "proj-1", "provider": "claude_code",
            }, "odin"))

        assert any(e.event_type == "task_running" for e in emits1)
        assert any(e.event_type == "task_running" for e in emits2)

        await runner.shutdown(timeout=2.0)

    @pytest.mark.asyncio
    async def test_different_providers_run_parallel(self, db):
        """Tasks with different providers can run simultaneously."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db, num_tasks=3)

        providers_called = []

        async def track_provider(*, provider, **kwargs):
            providers_called.append(provider)
            return {
                "output": "Done.", "cost_usd": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0, "model_used": provider, "narration": [],
            }

        with patch.object(runner, "_run_cli_background", side_effect=track_provider):
            await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
            }, "odin"))
            await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t2", "project_id": "proj-1", "provider": "gemini_cli",
            }, "odin"))
            await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t3", "project_id": "proj-1", "provider": "ollama",
            }, "odin"))

            await asyncio.sleep(0.2)
            await runner.shutdown(timeout=2.0)

        assert set(providers_called) == {"claude_code", "gemini_cli", "ollama"}


# ===========================================================================
# 4. CLEANUP — cancellation, shutdown, crash recovery
# ===========================================================================

class TestAsyncHermesCleanup:
    @pytest.mark.asyncio
    async def test_cancel_task_writes_failed_event(self, db):
        """cancel(task_id) should kill the subprocess and write failed."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        async def hang_forever(**kwargs):
            await asyncio.sleep(999)
            return {"output": "", "cost_usd": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "model_used": "", "narration": []}

        # Keep patch active through cancel — don't exit the with block early
        patcher = patch.object(runner, "_run_cli_background", side_effect=hang_forever)
        patcher.start()

        await runner.handle_dispatch(Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin"))

        assert "t1" in runner.in_flight

        # Cancel it — wait for handler to complete
        await runner.cancel("t1")
        await asyncio.sleep(0.2)

        patcher.stop()
        await runner.shutdown(timeout=1.0)

        assert "t1" not in runner.in_flight

        # Task should be marked failed in DB
        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "failed"

        # Should have written failed event to relay
        rows = await db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = ?",
            ("worker_event",))
        assert len(rows) >= 1
        payload = json.loads(rows[0][0] if len(rows[0]) == 1 else rows[0][1])
        assert payload["status"] == "failed"
        assert "cancel" in payload["error"].lower()

    @pytest.mark.asyncio
    async def test_shutdown_awaits_in_flight(self, db):
        """shutdown() should wait for in-flight tasks to complete."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        completed = []

        async def slow_cli(**kwargs):
            await asyncio.sleep(0.3)
            completed.append(True)
            return {
                "output": "Done.", "cost_usd": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0, "model_used": "claude", "narration": [],
            }

        with patch.object(runner, "_run_cli_background", side_effect=slow_cli):
            await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
            }, "odin"))

            # Shutdown should wait for the 0.3s task
            await runner.shutdown(timeout=5.0)

        assert len(completed) == 1

    @pytest.mark.asyncio
    async def test_background_crash_still_writes_failed(self, db):
        """If the monitor coroutine itself crashes, task still gets failed event."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            # Simulate an unexpected crash in the background
            mock.side_effect = Exception("Unexpected internal error in monitor")
            await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
            }, "odin"))

            await asyncio.sleep(0.2)
            await runner.shutdown(timeout=2.0)

        # Task should never be stuck in running
        row = await db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "failed"

        # Should have written to relay
        rows = await db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = ?",
            ("worker_event",))
        assert len(rows) >= 1
        payload = json.loads(rows[0][0] if len(rows[0]) == 1 else rows[0][1])
        assert payload["status"] == "failed"

    @pytest.mark.asyncio
    async def test_shutdown_timeout_forces_cleanup(self, db):
        """If tasks don't finish in time, shutdown still completes."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            hang = asyncio.Future()  # Never resolves
            mock.return_value = hang

            await runner.handle_dispatch(Event("dispatch_command", {
                "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
            }, "odin"))

            # Shutdown with short timeout — should not hang
            await runner.shutdown(timeout=0.5)

        # Should have cleaned up in-flight
        assert len(runner.in_flight) == 0


# ===========================================================================
# 5. INTEGRATION — full pipeline chain with async hermes
# ===========================================================================

class TestAsyncHermesIntegration:
    @pytest.mark.asyncio
    async def test_pipeline_continues_during_execution(self, db):
        """Pipeline tick should return while hermes task runs in background."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        pipeline = Pipeline(db)

        # Register hermes as async handler
        pipeline.register("dispatch_command", runner.handle_dispatch)

        # Also register a simple handler to prove pipeline isn't blocked
        tick_count = []

        async def count_ticks(event, db):
            tick_count.append(1)
            return None

        pipeline.register("tick", count_ticks)

        await _seed(db)

        # Inject dispatch_command
        await db.execute_write(
            "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("dispatch_command", "odin",
             json.dumps({"task_id": "t1", "project_id": "proj-1", "provider": "claude_code"}),
             "info", time.time()),
        )

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            # Simulate slow CLI
            async def slow():
                await asyncio.sleep(1.0)
                return {
                    "output": "Done.", "cost_usd": 0.01, "prompt_tokens": 100,
                    "completion_tokens": 50, "model_used": "claude", "narration": [],
                }
            mock.side_effect = slow

            # First tick — dispatches hermes
            await pipeline.tick()

            # Inject a tick event
            await db.execute_write(
                "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("tick", "test", "{}", "info", time.time()),
            )

            # Second tick — should process tick event even though hermes task is running
            await pipeline.tick()

        assert len(tick_count) >= 1, "Pipeline was blocked by hermes execution"

        await runner.shutdown(timeout=2.0)

    @pytest.mark.asyncio
    async def test_completed_task_picked_up_by_next_tick(self, db):
        """After hermes background completes, worker_event appears in relay for mimir."""
        from gods.handlers.hermes_async import HermesRunner

        runner = HermesRunner(db=db, max_concurrent=4)
        await _seed(db)

        event = Event("dispatch_command", {
            "task_id": "t1", "project_id": "proj-1", "provider": "claude_code",
        }, "odin")

        with patch.object(runner, "_run_cli_background", new_callable=AsyncMock) as mock:
            mock.return_value = {
                "output": "Done.", "cost_usd": 0.01, "prompt_tokens": 100,
                "completion_tokens": 50, "model_used": "claude", "narration": [],
            }
            await runner.handle_dispatch(event)
            await asyncio.sleep(0.2)
            await runner.shutdown(timeout=2.0)

        # worker_event should be in relay
        rows = await db.fetchall(
            "SELECT event_type, source FROM god_relay_events", ())
        event_types = [r[0] for r in rows]
        assert "worker_event" in event_types

        # Payload should be complete
        worker_rows = await db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = ?",
            ("worker_event",))
        payload = json.loads(worker_rows[0][0] if len(worker_rows[0]) == 1 else worker_rows[0][1])
        assert payload["task_id"] == "t1"
        assert payload["status"] == "completed"
