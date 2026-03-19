"""Tests for gods/pipeline.py — the simplified event-driven function pipeline."""

import asyncio
import json
import time

import pytest

from gods.pipeline import Pipeline, Event, Emit


# ---------------------------------------------------------------------------
# Basic handler registration and dispatch
# ---------------------------------------------------------------------------

class TestPipelineBasics:
    @pytest.mark.asyncio
    async def test_handler_called_on_matching_event(self, sqlite_db):
        calls = []

        async def my_handler(event, db):
            calls.append(event)
            return None

        p = Pipeline(sqlite_db)
        p.register("test_event", my_handler)

        # Inject an event directly
        await p.emit("test_event", {"key": "val"}, source="test")
        p._last_seen_id = 0  # look back
        await p.tick()

        assert len(calls) == 1
        assert calls[0].payload["key"] == "val"

    @pytest.mark.asyncio
    async def test_handler_not_called_for_wrong_type(self, sqlite_db):
        calls = []

        async def my_handler(event, db):
            calls.append(event)
            return None

        p = Pipeline(sqlite_db)
        p.register("type_a", my_handler)

        await p.emit("type_b", {}, source="test")
        p._last_seen_id = 0
        await p.tick()

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_filter_narrows_handler(self, sqlite_db):
        calls = []

        async def my_handler(event, db):
            calls.append(event)
            return None

        p = Pipeline(sqlite_db)
        p.register(
            "worker_event", my_handler,
            filter=lambda e: e.payload.get("status") == "completed",
        )

        await p.emit("worker_event", {"status": "started"}, source="test")
        await p.emit("worker_event", {"status": "completed"}, source="test")
        p._last_seen_id = 0
        await p.tick()

        assert len(calls) == 1
        assert calls[0].payload["status"] == "completed"

    @pytest.mark.asyncio
    async def test_no_handlers_is_fine(self, sqlite_db):
        p = Pipeline(sqlite_db)
        await p.tick()  # no crash


# ---------------------------------------------------------------------------
# Handler emitting new events
# ---------------------------------------------------------------------------

class TestPipelineEmit:
    @pytest.mark.asyncio
    async def test_handler_emits_trigger_next_tick(self, sqlite_db):
        """Handler A emits an event that handler B picks up on next tick."""
        b_calls = []

        async def handler_a(event, db):
            return [Emit("step_2", {"from": "a"}, source="handler_a")]

        async def handler_b(event, db):
            b_calls.append(event)
            return None

        p = Pipeline(sqlite_db)
        p.register("step_1", handler_a)
        p.register("step_2", handler_b)

        # Seed step_1
        await p.emit("step_1", {"init": True}, source="test")
        p._last_seen_id = 0

        # Multiple ticks — handler_a emits step_2, handler_b picks it up
        for _ in range(5):
            await p.tick()

        assert len(b_calls) == 1
        assert b_calls[0].payload["from"] == "a"

    @pytest.mark.asyncio
    async def test_handler_emits_multiple(self, sqlite_db):
        async def fan_out(event, db):
            return [
                Emit("output_a", {"i": 1}, source="fan"),
                Emit("output_b", {"i": 2}, source="fan"),
            ]

        a_calls = []
        b_calls = []

        async def catch_a(event, db):
            a_calls.append(event)
            return None

        async def catch_b(event, db):
            b_calls.append(event)
            return None

        p = Pipeline(sqlite_db)
        p.register("trigger", fan_out)
        p.register("output_a", catch_a)
        p.register("output_b", catch_b)

        await p.emit("trigger", {}, source="test")
        p._last_seen_id = 0

        for _ in range(5):
            await p.tick()

        assert len(a_calls) == 1
        assert len(b_calls) == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestPipelineErrors:
    @pytest.mark.asyncio
    async def test_handler_error_doesnt_crash_pipeline(self, sqlite_db):
        good_calls = []

        async def bad_handler(event, db):
            raise RuntimeError("boom")

        async def good_handler(event, db):
            good_calls.append(event)
            return None

        p = Pipeline(sqlite_db)
        p.register("test", bad_handler, name="bad")
        p.register("test", good_handler, name="good")

        await p.emit("test", {}, source="test")
        p._last_seen_id = 0
        await p.tick()

        # Good handler still ran despite bad handler crashing
        assert len(good_calls) == 1

    @pytest.mark.asyncio
    async def test_handler_error_emits_error_event(self, sqlite_db):
        async def bad_handler(event, db):
            raise ValueError("bad input")

        error_calls = []

        async def error_watcher(event, db):
            error_calls.append(event)
            return None

        p = Pipeline(sqlite_db)
        p.register("test", bad_handler, name="bad")
        p.register("handler_error", error_watcher)

        await p.emit("test", {}, source="test")
        p._last_seen_id = 0

        await p.tick()  # bad_handler fails, emits handler_error
        await p.tick()  # error_watcher picks it up

        assert len(error_calls) == 1
        assert error_calls[0].payload["handler"] == "bad"
        assert "bad input" in error_calls[0].payload["error"]


# ---------------------------------------------------------------------------
# Cursor / dedup
# ---------------------------------------------------------------------------

class TestPipelineCursor:
    @pytest.mark.asyncio
    async def test_events_only_processed_once(self, sqlite_db):
        calls = []

        async def handler(event, db):
            calls.append(event)
            return None

        p = Pipeline(sqlite_db)
        p.register("x", handler)

        await p.emit("x", {"n": 1}, source="test")
        p._last_seen_id = 0

        await p.tick()
        await p.tick()
        await p.tick()

        assert len(calls) == 1  # not 3

    @pytest.mark.asyncio
    async def test_new_events_after_tick_are_picked_up(self, sqlite_db):
        calls = []

        async def handler(event, db):
            calls.append(event)
            return None

        p = Pipeline(sqlite_db)
        p.register("x", handler)
        p._last_seen_id = 0

        await p.emit("x", {"n": 1}, source="test")
        await p.tick()

        await p.emit("x", {"n": 2}, source="test")
        await p.tick()

        assert len(calls) == 2
        assert calls[0].payload["n"] == 1
        assert calls[1].payload["n"] == 2


# ---------------------------------------------------------------------------
# Multi-handler chain (simulates the full gods flow)
# ---------------------------------------------------------------------------

class TestPipelineChain:
    @pytest.mark.asyncio
    async def test_plan_dispatch_execute_verify_chain(self, sqlite_db):
        """Simulate the full flow: plan → dispatch → execute → verify → done."""
        log = []

        async def athena_plan(event, db):
            log.append("athena: planning")
            return [Emit("project_planned", {
                "project_id": event.payload["project_id"],
                "task_count": 2,
            }, source="athena")]

        async def odin_start(event, db):
            log.append("odin: starting project")
            return [
                Emit("dispatch_command", {
                    "task_id": "t1",
                    "provider": "claude",
                }, source="odin"),
                Emit("dispatch_command", {
                    "task_id": "t2",
                    "provider": "gemini",
                }, source="odin"),
            ]

        async def hermes_execute(event, db):
            tid = event.payload["task_id"]
            log.append(f"hermes: executing {tid}")
            return [Emit("worker_event", {
                "task_id": tid,
                "status": "completed",
            }, source="hermes")]

        async def mimir_verify(event, db):
            tid = event.payload["task_id"]
            log.append(f"mimir: verifying {tid}")
            return [Emit("task_verified", {
                "task_id": tid,
                "verdict": "passed",
            }, source="mimir")]

        async def odin_wave_check(event, db):
            log.append(f"odin: task {event.payload['task_id']} verified")
            return None

        p = Pipeline(sqlite_db)
        p.register("project_created", athena_plan)
        p.register("project_planned", odin_start)
        p.register("dispatch_command", hermes_execute)
        p.register("worker_event", mimir_verify,
                    filter=lambda e: e.payload.get("status") == "completed")
        p.register("task_verified", odin_wave_check)

        # Seed
        await p.emit("project_created", {"project_id": "p1"}, source="api")
        p._last_seen_id = 0

        # Run enough ticks to cascade through all handlers
        # Each emit is picked up on the NEXT tick, so we need:
        # tick 1: athena plans
        # tick 2: odin starts (dispatches 2)
        # tick 3: hermes executes both
        # tick 4: mimir verifies both
        # tick 5: odin sees both verified
        for _ in range(10):
            await p.tick()

        assert "athena: planning" in log
        assert "odin: starting project" in log
        assert "hermes: executing t1" in log
        assert "hermes: executing t2" in log
        assert "mimir: verifying t1" in log
        assert "mimir: verifying t2" in log
        assert "odin: task t1 verified" in log
        assert "odin: task t2 verified" in log

    @pytest.mark.asyncio
    async def test_error_doesnt_break_chain(self, sqlite_db):
        """A handler failure in the middle doesn't stop later handlers."""
        log = []

        async def step_1(event, db):
            log.append("step_1")
            return [Emit("step_2", {}, source="s1")]

        async def step_2_bad(event, db):
            log.append("step_2_bad")
            raise RuntimeError("oops")

        async def step_2_good(event, db):
            log.append("step_2_good")
            return [Emit("step_3", {}, source="s2")]

        async def step_3(event, db):
            log.append("step_3")
            return None

        p = Pipeline(sqlite_db)
        p.register("start", step_1)
        p.register("step_2", step_2_bad, name="bad")
        p.register("step_2", step_2_good, name="good")
        p.register("step_3", step_3)

        await p.emit("start", {}, source="test")
        p._last_seen_id = 0

        for _ in range(10):
            await p.tick()

        assert "step_1" in log
        assert "step_2_bad" in log
        assert "step_2_good" in log
        assert "step_3" in log


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

class TestPipelineIntrospection:
    def test_handlers_list(self, sqlite_db):
        async def a(e, db): return None
        async def b(e, db): return None

        p = Pipeline(sqlite_db)
        p.register("x", a)
        p.register("y", b, filter=lambda e: True)

        h = p.handlers
        assert len(h) == 2
        assert h[0]["event_type"] == "x"
        assert h[0]["has_filter"] is False
        assert h[1]["has_filter"] is True

    def test_status(self, sqlite_db):
        p = Pipeline(sqlite_db)
        p.register("x", lambda e, db: None)

        s = p.status
        assert s["running"] is False
        assert s["handler_count"] == 1
        assert "x" in s["subscribed_events"]
