"""Tests for per-task override precedence and pipeline delay integration.

Covers:
  1. Precedence: registry default < plan-level config < task-level override
  2. Pipeline gate delay: _run_with_gate sleeps using task's RetryPolicy
  3. Mimir retry delay: asyncio.sleep called with computed delay before re-queue
  4. Relay event time_until_retry: gate_failed/task_rejected include correct delay
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
import aiosqlite

from conftest import FakeDB, SqliteDB
from gods.pipeline import Pipeline, Event, Emit, GateResult, Registration
from gods.task_definition import (
    RetryLogic,
    RetryPolicy,
    TaskDefinition,
    apply_defaults,
    merge_with_plan_config,
    get_registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def full_db():
    """SQLite DB with god tables + tasks for override tests."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE god_relay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            payload TEXT,
            severity TEXT DEFAULT 'info',
            idempotency_key TEXT UNIQUE,
            created_at REAL NOT NULL
        );
        CREATE INDEX idx_relay_type_created
            ON god_relay_events (event_type, created_at);

        CREATE TABLE god_registry (
            name TEXT PRIMARY KEY,
            port INTEGER,
            status TEXT DEFAULT 'unknown',
            last_heartbeat REAL,
            config TEXT,
            updated_at REAL,
            last_seen_id INTEGER DEFAULT 0
        );

        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            plan_id TEXT,
            title TEXT,
            description TEXT,
            task_type TEXT DEFAULT 'code',
            status TEXT DEFAULT 'pending',
            wave INTEGER DEFAULT 0,
            model_tier TEXT DEFAULT 'claude_code',
            context_json TEXT DEFAULT '{}',
            output_text TEXT,
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            verification_status TEXT,
            verification_notes TEXT,
            error TEXT,
            started_at REAL,
            completed_at REAL,
            updated_at REAL
        );
    """)
    await conn.commit()
    db = SqliteDB(conn)
    yield db
    await conn.close()


# ===================================================================
# 1. Precedence tests
# ===================================================================

class TestOverridePrecedence:
    """Registry default < plan-level config < task-level override."""

    def test_registry_default_for_code_medium(self):
        """apply_defaults('code', 'medium') should produce EXPONENTIAL_BACKOFF."""
        td = apply_defaults("code", "medium")
        assert td.retry_policy is not None
        assert td.retry_policy.strategy == RetryLogic.EXPONENTIAL_BACKOFF

    def test_plan_config_overrides_registry_default(self):
        """Plan-level retry_policy should win over registry default."""
        td = apply_defaults("code", "medium")

        plan_config = {
            "retry_policy": {
                "strategy": "FIXED",
                "base_delay_seconds": 10,
            },
        }
        merged = merge_with_plan_config(td, plan_config, task_type="code", complexity="medium")

        assert merged.retry_policy is not None
        assert merged.retry_policy.strategy == RetryLogic.FIXED
        assert merged.retry_policy.base_delay_seconds == 10

    def test_task_level_wins_over_plan_config(self):
        """Explicit task-level retry_policy should survive merge_with_plan_config."""
        task_policy = RetryPolicy(
            strategy=RetryLogic.LINEAR_BACKOFF,
            base_delay_seconds=5,
        )
        td = TaskDefinition.from_dict({
            "retry_count": 3,
            "retry_delay_seconds": 60,
            "retry_policy": task_policy.to_dict(),
        })

        # Sanity: task-level was parsed
        assert td.retry_policy is not None
        assert td.retry_policy.strategy == RetryLogic.LINEAR_BACKOFF
        assert td.retry_policy.base_delay_seconds == 5

        plan_config = {
            "retry_policy": {
                "strategy": "FIXED",
                "base_delay_seconds": 10,
            },
        }
        merged = merge_with_plan_config(td, plan_config, task_type="code", complexity="medium")

        # Task-level policy should NOT be clobbered by plan config
        assert merged.retry_policy.strategy == RetryLogic.LINEAR_BACKOFF
        assert merged.retry_policy.base_delay_seconds == 5

    def test_plan_config_does_not_override_non_default_scalar(self):
        """Plan config only overrides scalars when they match registry defaults."""
        td = apply_defaults("code", "medium")
        # Manually set non-default retry_count
        import dataclasses
        td = dataclasses.replace(td, retry_count=99)

        plan_config = {"max_retries": 7}
        merged = merge_with_plan_config(td, plan_config, task_type="code", complexity="medium")

        # Should keep 99 because it differs from registry default
        assert merged.retry_count == 99

    def test_empty_plan_config_is_noop(self):
        """None or empty plan_config should return task_def unchanged."""
        td = apply_defaults("code", "medium")
        assert merge_with_plan_config(td, None) is td
        assert merge_with_plan_config(td, {}) is td


# ===================================================================
# 2. Pipeline gate delay — _run_with_gate uses task's RetryPolicy
# ===================================================================

class TestPipelineGateDelay:
    @pytest.mark.asyncio
    async def test_gate_retry_uses_task_retry_policy(self, full_db):
        """_run_with_gate should sleep using the task's RetryPolicy delay."""
        task_id = "task-delay-1"
        # Store a task with a FIXED RetryPolicy (base_delay=2, no jitter)
        td = TaskDefinition(
            retry_policy=RetryPolicy(
                strategy=RetryLogic.FIXED,
                base_delay_seconds=2,
                jitter=False,
            ),
        )
        ctx = json.dumps({"task_definition": td.to_dict()})
        await full_db.execute_write(
            "INSERT INTO tasks (id, project_id, title, status, context_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, "p1", "Test task", "pending", ctx),
        )

        call_count = 0

        async def failing_then_passing_handler(event, db):
            nonlocal call_count
            call_count += 1
            return [Emit("some_output", {"task_id": task_id}, "test")]

        gate_calls = 0

        async def fail_once_gate(event, emits, db):
            nonlocal gate_calls
            gate_calls += 1
            if gate_calls == 1:
                return GateResult(False, "First attempt fails")
            return GateResult(True, "Passed on retry")

        pipeline = Pipeline(full_db)
        reg = Registration(
            event_type="dispatch_command",
            handler=failing_then_passing_handler,
            name="test_handler",
            gate=fail_once_gate,
            max_retries=1,
        )
        pipeline._handlers.append(reg)

        event = Event("dispatch_command", {"task_id": task_id}, "test")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await pipeline._run_with_gate(reg, event)

        # Gate failed once then passed → handler called twice
        assert call_count == 2
        assert gate_calls == 2
        assert result is not None

        # asyncio.sleep should have been called with the FIXED delay of 2s
        mock_sleep.assert_called_once()
        actual_delay = mock_sleep.call_args[0][0]
        assert actual_delay == 2.0  # FIXED, no jitter → exactly 2.0

    @pytest.mark.asyncio
    async def test_gate_retry_default_delay_when_no_task(self, full_db):
        """When event has no task_id, should use default 30s delay."""
        call_count = 0

        async def handler(event, db):
            nonlocal call_count
            call_count += 1
            return [Emit("output", {}, "test")]

        gate_calls = 0

        async def fail_once_gate(event, emits, db):
            nonlocal gate_calls
            gate_calls += 1
            if gate_calls == 1:
                return GateResult(False, "fail")
            return GateResult(True, "pass")

        pipeline = Pipeline(full_db)
        reg = Registration(
            event_type="some_event",
            handler=handler,
            name="no_task_handler",
            gate=fail_once_gate,
            max_retries=1,
        )
        pipeline._handlers.append(reg)

        event = Event("some_event", {}, "test")  # no task_id

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await pipeline._run_with_gate(reg, event)

        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == 30  # default


# ===================================================================
# 3. Mimir retry delay — asyncio.sleep called before re-queue
# ===================================================================

class TestMimirRetryDelay:
    @pytest.mark.asyncio
    async def test_heuristic_failure_sleeps_with_task_delay(self, full_db):
        """Mimir._handle_heuristic_failure should sleep using task's retry policy."""
        from gods.handlers.mimir import MimirRunner

        task_id = "task-mimir-1"
        td = TaskDefinition(
            retry_policy=RetryPolicy(
                strategy=RetryLogic.FIXED,
                base_delay_seconds=7,
                jitter=False,
            ),
        )
        ctx = json.dumps({"task_definition": td.to_dict()})
        await full_db.execute_write(
            "INSERT INTO tasks (id, project_id, title, description, status, "
            "output_text, retry_count, max_retries, context_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "p1", "Test", "Desc", "running", "x", 0, 3, ctx),
        )

        runner = MimirRunner(db=full_db)

        with patch("gods.handlers.mimir.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            emits = await runner._handle_heuristic_failure(
                task_id=task_id,
                project_id="p1",
                reason="Output is empty",
                retry_count=0,
                max_retries=3,
            )

        # Should have slept with FIXED delay of 7s
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == 7.0

        # Should emit task_rejected
        assert any(e.event_type == "task_rejected" for e in emits)
        rejected = next(e for e in emits if e.event_type == "task_rejected")
        assert rejected.payload["retry_delay_seconds"] == 7.0

    @pytest.mark.asyncio
    async def test_gaps_found_sleeps_with_task_delay(self, full_db):
        """Mimir._verify_task should sleep on gaps_found using task's retry policy."""
        from gods.handlers.mimir import MimirRunner

        task_id = "task-mimir-2"
        td = TaskDefinition(
            retry_policy=RetryPolicy(
                strategy=RetryLogic.FIXED,
                base_delay_seconds=4,
                jitter=False,
            ),
        )
        ctx = json.dumps({"task_definition": td.to_dict()})
        await full_db.execute_write(
            "INSERT INTO tasks (id, project_id, title, description, status, "
            "output_text, retry_count, max_retries, context_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "p1", "Test task", "Description", "running",
             "Some real output text that is long enough", 0, 3, ctx),
        )

        runner = MimirRunner(db=full_db)

        gaps_result = {"verdict": "gaps_found", "confidence": 0.3, "feedback": "missing tests"}

        with patch("gods.handlers.mimir._call_verifier", return_value=gaps_result), \
             patch("gods.handlers.mimir.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await runner._verify_task(
                task_id=task_id,
                project_id="p1",
                title="Test task",
                description="Description",
                output_text="Some real output text that is long enough",
                retry_count=0,
                max_retries=3,
            )

        # Should have slept with FIXED delay of 4s
        mock_sleep.assert_called_once()
        assert mock_sleep.call_args[0][0] == 4.0

        # Should have written task_rejected relay event
        rows = await full_db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = 'task_rejected'"
        )
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload"])
        assert payload["retry_delay_seconds"] == 4.0


# ===================================================================
# 4. Relay event time_until_retry — gate_failed and task_rejected payloads
# ===================================================================

class TestRelayEventTimeUntilRetry:
    @pytest.mark.asyncio
    async def test_gate_failed_includes_time_until_retry(self, full_db):
        """gate_failed event should include time_until_retry_seconds from task's policy."""
        task_id = "task-relay-1"
        td = TaskDefinition(
            retry_policy=RetryPolicy(
                strategy=RetryLogic.FIXED,
                base_delay_seconds=15,
                jitter=False,
            ),
        )
        ctx = json.dumps({"task_definition": td.to_dict()})
        await full_db.execute_write(
            "INSERT INTO tasks (id, project_id, title, status, context_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, "p1", "Relay test", "pending", ctx),
        )

        async def handler(event, db):
            return [Emit("output", {"task_id": task_id}, "test")]

        async def always_fail_gate(event, emits, db):
            return GateResult(False, "Always fails for test")

        pipeline = Pipeline(full_db)
        reg = Registration(
            event_type="dispatch_command",
            handler=handler,
            name="relay_handler",
            gate=always_fail_gate,
            max_retries=0,  # no retries → gate_failed then gate_exhausted
        )
        pipeline._handlers.append(reg)

        event = Event("dispatch_command", {"task_id": task_id}, "test")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await pipeline._run_with_gate(reg, event)

        # Read gate_failed event from relay
        rows = await full_db.fetchall(
            "SELECT event_type, payload FROM god_relay_events WHERE event_type = 'gate_failed'"
        )
        assert len(rows) >= 1
        payload = json.loads(rows[0]["payload"])
        assert "time_until_retry_seconds" in payload
        assert payload["time_until_retry_seconds"] == 15.0

    @pytest.mark.asyncio
    async def test_gate_failed_with_exponential_backoff(self, full_db):
        """gate_failed delay should increase with exponential backoff across attempts."""
        task_id = "task-relay-2"
        td = TaskDefinition(
            retry_policy=RetryPolicy(
                strategy=RetryLogic.EXPONENTIAL_BACKOFF,
                base_delay_seconds=10,
                max_delay_seconds=300,
                jitter=False,
                backoff_rate=2.0,
            ),
        )
        ctx = json.dumps({"task_definition": td.to_dict()})
        await full_db.execute_write(
            "INSERT INTO tasks (id, project_id, title, status, context_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_id, "p1", "Expo test", "pending", ctx),
        )

        async def handler(event, db):
            return [Emit("output", {"task_id": task_id}, "test")]

        async def always_fail_gate(event, emits, db):
            return GateResult(False, "Fail for test")

        pipeline = Pipeline(full_db)
        reg = Registration(
            event_type="dispatch_command",
            handler=handler,
            name="expo_handler",
            gate=always_fail_gate,
            max_retries=2,  # 3 total attempts
        )
        pipeline._handlers.append(reg)

        event = Event("dispatch_command", {"task_id": task_id}, "test")

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await pipeline._run_with_gate(reg, event)

        # Read all gate_failed events
        rows = await full_db.fetchall(
            "SELECT payload FROM god_relay_events WHERE event_type = 'gate_failed' "
            "ORDER BY id ASC"
        )
        # 3 attempts → 3 gate_failed events
        assert len(rows) == 3

        delays = [json.loads(r["payload"])["time_until_retry_seconds"] for r in rows]
        # Exponential: attempt 0 → 10*2^0=10, attempt 1 → 10*2^1=20, attempt 2 → 10*2^2=40
        assert delays[0] == 10.0
        assert delays[1] == 20.0
        assert delays[2] == 40.0

    @pytest.mark.asyncio
    async def test_task_rejected_includes_retry_delay(self, full_db):
        """task_rejected from mimir should include retry_delay_seconds."""
        from gods.handlers.mimir import MimirRunner

        task_id = "task-relay-3"
        td = TaskDefinition(
            retry_policy=RetryPolicy(
                strategy=RetryLogic.FIXED,
                base_delay_seconds=12,
                jitter=False,
            ),
        )
        ctx = json.dumps({"task_definition": td.to_dict()})
        await full_db.execute_write(
            "INSERT INTO tasks (id, project_id, title, description, status, "
            "output_text, retry_count, max_retries, context_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, "p1", "Test", "Desc", "running", "x", 0, 3, ctx),
        )

        runner = MimirRunner(db=full_db)

        with patch("gods.handlers.mimir.asyncio.sleep", new_callable=AsyncMock):
            emits = await runner._handle_heuristic_failure(
                task_id=task_id,
                project_id="p1",
                reason="Output is empty",
                retry_count=0,
                max_retries=3,
            )

        rejected = next(e for e in emits if e.event_type == "task_rejected")
        assert "retry_delay_seconds" in rejected.payload
        assert rejected.payload["retry_delay_seconds"] == 12.0
