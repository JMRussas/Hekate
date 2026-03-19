"""Tests for God base class relay integration (subscribe, poll, emit)."""

import json
import time

import pytest

from gods.base import God


# ---------------------------------------------------------------------------
# God relay methods
# ---------------------------------------------------------------------------

class TestGodSubscribe:
    def test_sets_subscriptions(self, fake_db):
        god = God("test", fake_db)
        god.subscribe(["dispatch_command", "worker_event"])
        assert god._relay_subscriptions == ["dispatch_command", "worker_event"]

    def test_sets_cursor_to_now(self, fake_db):
        before = time.time()
        god = God("test", fake_db)
        god.subscribe(["x"])
        after = time.time()
        assert before <= god._relay_cursor <= after


class TestGodEmitRelay:
    @pytest.mark.asyncio
    async def test_writes_to_relay_table(self, sqlite_db):
        god = God("odin", sqlite_db)

        await god.emit_relay("dispatch_command", {"task_id": "t1"})

        row = await sqlite_db.fetchone(
            "SELECT event_type, source, payload, severity FROM god_relay_events"
        )
        assert row["event_type"] == "dispatch_command"
        assert row["source"] == "god:odin"
        payload = json.loads(row["payload"])
        assert payload["task_id"] == "t1"
        assert row["severity"] == "info"

    @pytest.mark.asyncio
    async def test_custom_severity(self, sqlite_db):
        god = God("huginn", sqlite_db)
        await god.emit_relay("stall_notification", {"task": "t1"}, severity="warning")

        row = await sqlite_db.fetchone(
            "SELECT severity FROM god_relay_events"
        )
        assert row["severity"] == "warning"

    @pytest.mark.asyncio
    async def test_source_prefixed_with_god(self, sqlite_db):
        god = God("athena", sqlite_db)
        await god.emit_relay("project_planned", {})

        row = await sqlite_db.fetchone("SELECT source FROM god_relay_events")
        assert row["source"] == "god:athena"


class TestGodPollRelay:
    @pytest.mark.asyncio
    async def test_receives_subscribed_events(self, sqlite_db):
        god = God("hermes", sqlite_db)
        god.subscribe(["dispatch_command"])
        god._relay_cursor = time.time() - 10  # look back

        # Write an event from another god
        odin = God("odin", sqlite_db)
        await odin.emit_relay("dispatch_command", {"task_id": "t1"})

        events = await god.poll_relay()
        assert len(events) == 1
        assert events[0].event_type == "dispatch_command"
        assert events[0].payload["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_cursor_advances_after_poll(self, sqlite_db):
        god = God("mimir", sqlite_db)
        god.subscribe(["worker_event"])
        god._relay_cursor = time.time() - 10

        hermes = God("hermes", sqlite_db)
        await hermes.emit_relay("worker_event", {"status": "completed"})

        events = await god.poll_relay()
        assert len(events) == 1

        # Second poll — cursor advanced, no new events
        events2 = await god.poll_relay()
        assert len(events2) == 0

    @pytest.mark.asyncio
    async def test_ignores_unsubscribed_types(self, sqlite_db):
        god = God("hermes", sqlite_db)
        god.subscribe(["dispatch_command"])
        god._relay_cursor = time.time() - 10

        odin = God("odin", sqlite_db)
        await odin.emit_relay("budget_warning", {"remaining": 0})

        events = await god.poll_relay()
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_no_subscriptions_returns_empty(self, sqlite_db):
        god = God("lonely", sqlite_db)
        # Never called subscribe()
        events = await god.poll_relay()
        assert events == []

    @pytest.mark.asyncio
    async def test_multiple_events_in_order(self, sqlite_db):
        god = God("odin", sqlite_db)
        god.subscribe(["worker_event"])
        god._relay_cursor = time.time() - 10

        hermes = God("hermes", sqlite_db)
        t = time.time()
        await hermes.emit_relay("worker_event", {"task": "t1", "status": "started"})
        await hermes.emit_relay("worker_event", {"task": "t1", "status": "completed"})

        events = await god.poll_relay()
        assert len(events) == 2
        assert events[0].payload["status"] == "started"
        assert events[1].payload["status"] == "completed"


class TestGodRelayRoundTrip:
    """End-to-end: god A emits, god B polls, events match."""

    @pytest.mark.asyncio
    async def test_odin_dispatches_hermes_receives(self, sqlite_db):
        odin = God("odin", sqlite_db)
        hermes = God("hermes", sqlite_db)
        hermes.subscribe(["dispatch_command"])
        hermes._relay_cursor = time.time() - 1

        # Odin dispatches a task
        await odin.emit_relay("dispatch_command", {
            "task_id": "task-42",
            "provider": "claude_code",
            "model": "sonnet",
        })

        # Hermes picks it up
        events = await hermes.poll_relay()
        assert len(events) == 1
        cmd = events[0]
        assert cmd.event_type == "dispatch_command"
        assert cmd.source == "god:odin"
        assert cmd.payload["task_id"] == "task-42"
        assert cmd.payload["provider"] == "claude_code"

    @pytest.mark.asyncio
    async def test_multi_god_fan_out(self, sqlite_db):
        """Hermes emits worker_event, both Odin and Mimir receive it."""
        hermes = God("hermes", sqlite_db)
        odin = God("odin", sqlite_db)
        mimir = God("mimir", sqlite_db)

        odin.subscribe(["worker_event"])
        mimir.subscribe(["worker_event"])
        cursor = time.time() - 1
        odin._relay_cursor = cursor
        mimir._relay_cursor = cursor

        await hermes.emit_relay("worker_event", {
            "task_id": "t1",
            "status": "completed",
            "output_len": 1500,
        })

        odin_events = await odin.poll_relay()
        mimir_events = await mimir.poll_relay()

        assert len(odin_events) == 1
        assert len(mimir_events) == 1
        assert odin_events[0].payload["task_id"] == "t1"
        assert mimir_events[0].payload["task_id"] == "t1"
