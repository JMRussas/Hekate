"""Tests for gods/repair.py — repair handler module.

RED PHASE: gods/repair.py does not exist yet. These tests define the
contract. They should FAIL until the module is implemented.
"""

import json
import time

import pytest

from gods.pipeline import Pipeline, Event, Emit, GateResult


# ---------------------------------------------------------------------------
# Import repair handlers — these DON'T EXIST YET (RED)
# ---------------------------------------------------------------------------

from gods.repair import (
    retry_with_fix_handler,
    change_provider_handler,
    make_repair_router,
)


# ---------------------------------------------------------------------------
# retry_with_fix_handler
# ---------------------------------------------------------------------------

class TestRetryWithFix:
    @pytest.mark.asyncio
    async def test_re_emits_original_event_with_guidance(self):
        event = Event("repair_command", {
            "strategy": "retry_with_fix",
            "handler": "hermes",
            "original_event_type": "dispatch_command",
            "original_payload": {"task_id": "t1", "provider": "claude"},
            "fix_detail": {"guidance": "Check your imports"},
        }, "diagnosis")

        emits = await retry_with_fix_handler(event, None)

        assert len(emits) == 1
        assert emits[0].event_type == "dispatch_command"
        assert emits[0].payload["task_id"] == "t1"
        assert emits[0].payload["_fix_guidance"] == "Check your imports"
        assert emits[0].payload["_repair_attempt"] is True

    @pytest.mark.asyncio
    async def test_preserves_original_payload(self):
        event = Event("repair_command", {
            "strategy": "retry_with_fix",
            "handler": "athena",
            "original_event_type": "project_created",
            "original_payload": {"project_id": "p1", "custom_field": "keep_me"},
            "fix_detail": {"guidance": "Add error handling"},
        }, "diagnosis")

        emits = await retry_with_fix_handler(event, None)

        assert emits[0].payload["project_id"] == "p1"
        assert emits[0].payload["custom_field"] == "keep_me"

    @pytest.mark.asyncio
    async def test_no_original_event_type_returns_none(self):
        event = Event("repair_command", {
            "strategy": "retry_with_fix",
            "handler": "hermes",
            # missing original_event_type
        }, "diagnosis")

        emits = await retry_with_fix_handler(event, None)
        assert emits is None


# ---------------------------------------------------------------------------
# change_provider_handler
# ---------------------------------------------------------------------------

class TestChangeProvider:
    @pytest.mark.asyncio
    async def test_re_emits_with_new_provider(self):
        event = Event("repair_command", {
            "strategy": "change_provider",
            "handler": "hermes",
            "original_event_type": "dispatch_command",
            "original_payload": {"task_id": "t1", "provider": "claude"},
            "fix_detail": {"new_provider": "gemini", "old_provider": "claude"},
        }, "diagnosis")

        emits = await change_provider_handler(event, None)

        assert len(emits) == 1
        assert emits[0].event_type == "dispatch_command"
        assert emits[0].payload["provider"] == "gemini"
        assert emits[0].payload["_repair_attempt"] is True

    @pytest.mark.asyncio
    async def test_no_new_provider_returns_none(self):
        event = Event("repair_command", {
            "strategy": "change_provider",
            "handler": "hermes",
            "original_event_type": "dispatch_command",
            "original_payload": {"task_id": "t1"},
            "fix_detail": {},  # no new_provider
        }, "diagnosis")

        emits = await change_provider_handler(event, None)
        assert emits is None


# ---------------------------------------------------------------------------
# make_repair_router — convenience that registers all repair handlers
# ---------------------------------------------------------------------------

class TestRepairRouter:
    @pytest.mark.asyncio
    async def test_routes_retry_with_fix(self, sqlite_db):
        log = []

        async def capture(event, db):
            log.append(event.payload.get("_fix_guidance"))
            return None

        p = Pipeline(sqlite_db)
        make_repair_router(p)
        p.register("dispatch_command", capture, name="final_handler")

        # Emit a repair_command with retry_with_fix strategy
        await p.emit("repair_command", {
            "strategy": "retry_with_fix",
            "handler": "hermes",
            "original_event_type": "dispatch_command",
            "original_payload": {"task_id": "t1"},
            "fix_detail": {"guidance": "Fix imports"},
        }, source="diagnosis")
        p._last_seen_id = 0

        for _ in range(5):
            await p.tick()

        assert "Fix imports" in log

    @pytest.mark.asyncio
    async def test_routes_change_provider(self, sqlite_db):
        log = []

        async def capture(event, db):
            log.append(event.payload.get("provider"))
            return None

        p = Pipeline(sqlite_db)
        make_repair_router(p)
        p.register("dispatch_command", capture, name="final_handler")

        await p.emit("repair_command", {
            "strategy": "change_provider",
            "handler": "hermes",
            "original_event_type": "dispatch_command",
            "original_payload": {"task_id": "t1", "provider": "claude"},
            "fix_detail": {"new_provider": "gemini"},
        }, source="diagnosis")
        p._last_seen_id = 0

        for _ in range(5):
            await p.tick()

        assert "gemini" in log

    @pytest.mark.asyncio
    async def test_routes_escalate(self, sqlite_db):
        p = Pipeline(sqlite_db)
        make_repair_router(p)

        await p.emit("repair_command", {
            "strategy": "escalate",
            "handler": "hermes",
            "root_cause": "all failed",
            "why_chain": ["tried everything"],
        }, source="diagnosis")
        p._last_seen_id = 0

        for _ in range(5):
            await p.tick()

        rows = await sqlite_db.fetchall(
            "SELECT event_type FROM god_relay_events WHERE event_type = 'human_intervention_needed'"
        )
        assert len(rows) >= 1


# ---------------------------------------------------------------------------
# DB gate tests — gates that query real database state
# ---------------------------------------------------------------------------

class TestDBGates:
    @pytest.mark.asyncio
    async def test_check_plan_created_with_real_db(self, sqlite_db):
        from gods.gates import check_plan_created

        # Create plans table
        await sqlite_db.execute_write("""
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY,
                plan_json TEXT,
                project_id TEXT,
                version INTEGER,
                model_used TEXT,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                status TEXT DEFAULT 'draft',
                node_mapping_json TEXT,
                created_at REAL
            )
        """)

        # Insert a real plan
        await sqlite_db.execute_write(
            "INSERT INTO plans (id, plan_json, created_at) VALUES (?, ?, ?)",
            ("plan-1", json.dumps({"summary": "test", "tasks": [{"title": "t1"}]}), time.time()),
        )

        # Gate should pass
        emits = [Emit("plan_generated", {"plan_id": "plan-1"}, source="athena")]
        result = await check_plan_created(None, emits, sqlite_db)
        assert result.passed

    @pytest.mark.asyncio
    async def test_check_plan_created_missing_plan(self, sqlite_db):
        from gods.gates import check_plan_created

        await sqlite_db.execute_write("""
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, plan_json TEXT, created_at REAL
            )
        """)

        emits = [Emit("plan_generated", {"plan_id": "nonexistent"}, source="athena")]
        result = await check_plan_created(None, emits, sqlite_db)
        assert not result.passed
        assert "not found" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_check_plan_created_empty_plan(self, sqlite_db):
        from gods.gates import check_plan_created

        await sqlite_db.execute_write("""
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, plan_json TEXT, created_at REAL
            )
        """)
        await sqlite_db.execute_write(
            "INSERT INTO plans (id, plan_json, created_at) VALUES (?, ?, ?)",
            ("plan-empty", json.dumps({}), time.time()),
        )

        emits = [Emit("plan_generated", {"plan_id": "plan-empty"}, source="athena")]
        result = await check_plan_created(None, emits, sqlite_db)
        assert not result.passed
        assert "no tasks" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_check_code_written_with_real_db(self, sqlite_db):
        from gods.gates import check_code_written

        await sqlite_db.execute_write("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, output_text TEXT, status TEXT
            )
        """)
        await sqlite_db.execute_write(
            "INSERT INTO tasks (id, output_text, status) VALUES (?, ?, ?)",
            ("t1", "def hello(): return 'world'", "completed"),
        )

        emits = [Emit("worker_event", {
            "task_id": "t1", "status": "completed", "output_len": 30,
        }, source="hermes")]
        result = await check_code_written(None, emits, sqlite_db)
        assert result.passed

    @pytest.mark.asyncio
    async def test_check_code_written_empty_output(self, sqlite_db):
        from gods.gates import check_code_written

        await sqlite_db.execute_write("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, output_text TEXT, status TEXT
            )
        """)
        await sqlite_db.execute_write(
            "INSERT INTO tasks (id, output_text, status) VALUES (?, ?, ?)",
            ("t1", "   \n  ", "completed"),
        )

        emits = [Emit("worker_event", {
            "task_id": "t1", "status": "completed", "output_len": 5,
        }, source="hermes")]
        result = await check_code_written(None, emits, sqlite_db)
        assert not result.passed
        assert "whitespace" in result.reason.lower()


# ---------------------------------------------------------------------------
# Pipeline restart recovery
# ---------------------------------------------------------------------------

class TestPipelineRecovery:
    @pytest.mark.asyncio
    async def test_resumes_from_last_seen_id(self, sqlite_db):
        """Simulate crash + restart: events before crash not reprocessed."""
        calls = []

        async def handler(event, db):
            calls.append(event.payload["n"])
            return None

        # Emit 3 events
        for i in range(3):
            await sqlite_db.execute_write(
                "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("test", "src", json.dumps({"n": i}), "info", time.time()),
            )

        # First pipeline processes all 3
        p1 = Pipeline(sqlite_db)
        p1.register("test", handler)
        p1._last_seen_id = 0
        await p1.tick()
        assert len(calls) == 3

        # Simulate crash — save cursor
        saved_id = p1._last_seen_id

        # Emit 2 more events
        for i in range(3, 5):
            await sqlite_db.execute_write(
                "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("test", "src", json.dumps({"n": i}), "info", time.time()),
            )

        # Second pipeline (restart) — starts from saved cursor
        calls.clear()
        p2 = Pipeline(sqlite_db)
        p2.register("test", handler)
        p2._last_seen_id = saved_id  # resume from where p1 left off
        await p2.tick()

        # Should only process events 3, 4 — not 0, 1, 2
        assert calls == [3, 4]
