"""Tests for gods/relay.py — event relay, sinks, gate, polling."""

import asyncio
import json
import time

import pytest

from gods.relay import (
    Event, EventRelay, Gate,
    LogSink, HttpSink, FileSink, CallbackSink, PostgresSink,
    poll_events,
)


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

class TestEvent:
    def test_to_dict(self):
        e = Event("test", {"k": "v"}, "god:odin", 1000.0, "warning")
        d = e.to_dict()
        assert d["event_type"] == "test"
        assert d["source"] == "god:odin"
        assert d["payload"] == {"k": "v"}
        assert d["timestamp"] == 1000.0
        assert d["severity"] == "warning"

    def test_default_timestamp(self):
        before = time.time()
        e = Event("x", {}, "src")
        after = time.time()
        assert before <= e.timestamp <= after

    def test_default_severity(self):
        e = Event("x", {}, "src")
        assert e.severity == "info"


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------

class TestLogSink:
    @pytest.mark.asyncio
    async def test_writes_without_error(self):
        sink = LogSink()
        e = Event("test", {"big": "payload" * 100}, "src")
        await sink.write(e)  # should not raise


class TestCallbackSink:
    @pytest.mark.asyncio
    async def test_receives_event(self):
        received = []
        sink = CallbackSink(lambda e: asyncio.coroutine(lambda: received.append(e))())

        # Use a proper async callback
        events = []

        async def cb(event):
            events.append(event)

        sink2 = CallbackSink(cb)
        e = Event("x", {"v": 1}, "src")
        await sink2.write(e)
        assert len(events) == 1
        assert events[0].event_type == "x"


class TestFileSink:
    @pytest.mark.asyncio
    async def test_appends_jsonl(self, tmp_path):
        path = str(tmp_path / "events.jsonl")
        sink = FileSink(path)

        await sink.write(Event("a", {"n": 1}, "src"))
        await sink.write(Event("b", {"n": 2}, "src"))

        lines = open(path).readlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event_type"] == "a"
        assert json.loads(lines[1])["event_type"] == "b"


class TestPostgresSink:
    @pytest.mark.asyncio
    async def test_writes_to_relay_table(self, sqlite_db):
        sink = PostgresSink(sqlite_db)
        t = time.time()

        await sink.write(Event("dispatch_command", {"task": "t1"}, "god:odin", t))

        row = await sqlite_db.fetchone(
            "SELECT event_type, source, payload FROM god_relay_events"
        )
        assert row["event_type"] == "dispatch_command"
        assert row["source"] == "god:odin"
        assert json.loads(row["payload"])["task"] == "t1"

    @pytest.mark.asyncio
    async def test_writes_multiple(self, sqlite_db):
        sink = PostgresSink(sqlite_db)
        t = time.time()

        for i in range(5):
            await sink.write(Event(f"event_{i}", {"i": i}, "src", t + i))

        row = await sqlite_db.fetchone(
            "SELECT COUNT(*) FROM god_relay_events"
        )
        assert row["COUNT(*)"] == 5


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class TestGate:
    def test_blocks_by_type(self):
        gate = Gate(blocked_types={"heartbeat", "debug"})
        assert not gate.check(Event("heartbeat", {}, "src"))
        assert not gate.check(Event("debug", {}, "src"))
        assert gate.check(Event("dispatch_command", {}, "src"))

    def test_filters_by_severity(self):
        gate = Gate(min_severity="warning")
        assert not gate.check(Event("x", {}, "src", severity="info"))
        assert not gate.check(Event("x", {}, "src", severity="debug"))
        assert gate.check(Event("x", {}, "src", severity="warning"))
        assert gate.check(Event("x", {}, "src", severity="error"))

    def test_rate_limiting(self):
        gate = Gate(max_events_per_second=3)
        e = Event("x", {}, "src")
        assert gate.check(e)  # 1
        assert gate.check(e)  # 2
        assert gate.check(e)  # 3
        assert not gate.check(e)  # 4 — rejected

    def test_no_limits_by_default(self):
        gate = Gate()
        e = Event("x", {}, "src")
        for _ in range(100):
            assert gate.check(e)


# ---------------------------------------------------------------------------
# EventRelay
# ---------------------------------------------------------------------------

class TestEventRelay:
    @pytest.mark.asyncio
    async def test_emits_to_all_sinks(self):
        events_a = []
        events_b = []

        async def sink_a(e):
            events_a.append(e)

        async def sink_b(e):
            events_b.append(e)

        relay = EventRelay(sinks=[CallbackSink(sink_a), CallbackSink(sink_b)])
        await relay.emit("test", {"v": 1}, source="src")

        assert len(events_a) == 1
        assert len(events_b) == 1

    @pytest.mark.asyncio
    async def test_gate_blocks_event(self):
        events = []

        async def capture(e):
            events.append(e)

        relay = EventRelay(
            sinks=[CallbackSink(capture)],
            gate=Gate(blocked_types={"blocked"}),
        )
        await relay.emit("blocked", {}, source="src")
        await relay.emit("allowed", {}, source="src")

        assert len(events) == 1
        assert events[0].event_type == "allowed"

    @pytest.mark.asyncio
    async def test_sink_failure_doesnt_propagate(self):
        async def bad_sink(e):
            raise RuntimeError("sink exploded")

        events = []

        async def good_sink(e):
            events.append(e)

        relay = EventRelay(sinks=[
            CallbackSink(bad_sink),
            CallbackSink(good_sink),
        ])
        # Should not raise despite bad_sink
        await relay.emit("test", {}, source="src")
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_make_callback(self):
        events = []

        async def capture(e):
            events.append(e)

        relay = EventRelay(sinks=[CallbackSink(capture)])
        cb = relay.make_callback("demigod:hc:abc")

        await cb("step", {"action": "inspect"})

        assert events[0].source == "demigod:hc:abc"
        assert events[0].event_type == "step"
        assert events[0].payload["action"] == "inspect"

    @pytest.mark.asyncio
    async def test_from_config_postgres_sink(self, sqlite_db):
        relay = EventRelay.from_config({
            "sinks": [{"type": "postgres"}, {"type": "log"}],
            "_db": sqlite_db,
        })
        assert len(relay._sinks) == 2

        await relay.emit("test", {"k": "v"}, source="test")

        row = await sqlite_db.fetchone(
            "SELECT COUNT(*) FROM god_relay_events"
        )
        assert row["COUNT(*)"] == 1

    def test_from_config_without_db_skips_postgres(self):
        relay = EventRelay.from_config({
            "sinks": [{"type": "postgres"}, {"type": "log"}],
        })
        # Only log sink should be registered (postgres skipped)
        assert len(relay._sinks) == 1

    def test_default_has_log_sink(self):
        relay = EventRelay.default()
        assert len(relay._sinks) == 1


# ---------------------------------------------------------------------------
# poll_events
# ---------------------------------------------------------------------------

class TestPollEvents:
    @pytest.mark.asyncio
    async def test_returns_matching_events(self, sqlite_db):
        t = time.time()
        sink = PostgresSink(sqlite_db)
        await sink.write(Event("dispatch_command", {"t": 1}, "odin", t))
        await sink.write(Event("worker_event", {"t": 2}, "hermes", t + 1))
        await sink.write(Event("budget_warning", {"t": 3}, "tyche", t + 2))

        events, cursor = await poll_events(
            sqlite_db, ["dispatch_command", "worker_event"], t - 1
        )
        assert len(events) == 2
        assert events[0].event_type == "dispatch_command"
        assert events[1].event_type == "worker_event"

    @pytest.mark.asyncio
    async def test_cursor_advances(self, sqlite_db):
        t = time.time()
        sink = PostgresSink(sqlite_db)
        await sink.write(Event("x", {}, "src", t))
        await sink.write(Event("x", {}, "src", t + 1))

        events, cursor = await poll_events(sqlite_db, ["x"], t - 1)
        assert len(events) == 2
        assert cursor >= t + 1

        # Second poll with advanced cursor — nothing new
        events2, cursor2 = await poll_events(sqlite_db, ["x"], cursor)
        assert len(events2) == 0
        assert cursor2 == cursor

    @pytest.mark.asyncio
    async def test_empty_subscriptions(self, sqlite_db):
        events, cursor = await poll_events(sqlite_db, [], 0)
        assert events == []

    @pytest.mark.asyncio
    async def test_no_matching_events(self, sqlite_db):
        t = time.time()
        sink = PostgresSink(sqlite_db)
        await sink.write(Event("other_type", {}, "src", t))

        events, cursor = await poll_events(
            sqlite_db, ["dispatch_command"], t - 1
        )
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_payload_parsed_as_dict(self, sqlite_db):
        t = time.time()
        sink = PostgresSink(sqlite_db)
        await sink.write(Event("x", {"nested": {"a": 1}}, "src", t))

        events, _ = await poll_events(sqlite_db, ["x"], t - 1)
        assert events[0].payload["nested"]["a"] == 1

    @pytest.mark.asyncio
    async def test_respects_limit(self, sqlite_db):
        t = time.time()
        sink = PostgresSink(sqlite_db)
        for i in range(10):
            await sink.write(Event("x", {"i": i}, "src", t + i))

        events, _ = await poll_events(sqlite_db, ["x"], t - 1, limit=3)
        assert len(events) == 3
        # Should be oldest first
        assert events[0].payload["i"] == 0
        assert events[2].payload["i"] == 2
