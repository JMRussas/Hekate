"""Tests for gods/base.py and gods/registry.py."""

import asyncio
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gods.base import God, DatabaseLike
from gods.registry import discover_gods, get_god_config


# ---------------------------------------------------------------------------
# Fake database that records calls (matches DatabaseLike protocol)
# ---------------------------------------------------------------------------

class FakeDB:
    """In-memory DB stub that records all write calls and query results."""

    def __init__(self):
        self.writes: list[tuple[str, tuple]] = []
        self._fetchone_result = None
        self._fetchall_result: list = []

    async def execute_write(self, sql: str, params: tuple | list = ()) -> str:
        self.writes.append((sql, tuple(params)))
        return "OK"

    async def fetchone(self, sql: str, params: tuple | list = ()):
        return self._fetchone_result

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list:
        return self._fetchall_result


# ---------------------------------------------------------------------------
# FakeDB satisfies DatabaseLike protocol
# ---------------------------------------------------------------------------

def test_fakedb_satisfies_protocol():
    db = FakeDB()
    assert isinstance(db, DatabaseLike)


# ---------------------------------------------------------------------------
# God base class tests
# ---------------------------------------------------------------------------

class TestGodInit:
    def test_defaults(self):
        db = FakeDB()
        god = God("test-god", db)
        assert god.name == "test-god"
        assert god.db is db
        assert god.heartbeat_interval == 30.0
        assert god.uptime_seconds == 0.0
        assert god.tool_stats == {}

    def test_custom_heartbeat_interval(self):
        god = God("test-god", FakeDB(), heartbeat_interval=10.0)
        assert god.heartbeat_interval == 10.0


class TestGodLifecycle:
    @pytest.mark.asyncio
    async def test_start_stop(self):
        db = FakeDB()
        god = God("test-god", db, heartbeat_interval=100.0)

        await god.start()
        assert god._running is True
        assert god._started_at is not None
        assert god.uptime_seconds >= 0

        await god.stop()
        assert god._running is False

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        god = God("test-god", FakeDB(), heartbeat_interval=100.0)
        await god.start()
        task1 = god._heartbeat_task
        await god.start()  # second call should be no-op
        assert god._heartbeat_task is task1
        await god.stop()

    @pytest.mark.asyncio
    async def test_on_start_on_stop_hooks(self):
        calls = []

        class MyGod(God):
            async def on_start(self):
                calls.append("start")

            async def on_stop(self):
                calls.append("stop")

        god = MyGod("test-god", FakeDB(), heartbeat_interval=100.0)
        await god.start()
        await god.stop()
        assert calls == ["start", "stop"]


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_writes_event(self):
        db = FakeDB()
        god = God("pulse-god", db, heartbeat_interval=0.05)

        await god.start()
        # Let at least one heartbeat fire
        await asyncio.sleep(0.15)
        await god.stop()

        # Should have at least one heartbeat write
        heartbeat_writes = [
            (sql, params) for sql, params in db.writes
            if "god_events" in sql and params[1] == "heartbeat"
        ]
        assert len(heartbeat_writes) >= 1

        # Verify INSERT structure
        sql, params = heartbeat_writes[0]
        assert "INSERT INTO god_events" in sql
        assert params[0] == "pulse-god"      # god_name
        assert params[1] == "heartbeat"       # event_type
        assert params[3] == "info"            # severity

        # Verify payload is valid JSON with expected keys
        payload = json.loads(params[2])
        assert "uptime_s" in payload
        assert "tool_stats" in payload
        assert "dependencies" in payload

    @pytest.mark.asyncio
    async def test_heartbeat_includes_dependency_health(self):
        db = FakeDB()
        god = God("dep-god", db, heartbeat_interval=0.05)

        # Register a healthy dependency
        god.register_dependency("postgres", AsyncMock(return_value=None))
        # Register a failing dependency
        god.register_dependency("redis", AsyncMock(side_effect=ConnectionError("down")))

        await god.start()
        await asyncio.sleep(0.15)
        await god.stop()

        heartbeat_writes = [
            (sql, params) for sql, params in db.writes
            if "god_events" in sql and params[1] == "heartbeat"
        ]
        assert len(heartbeat_writes) >= 1

        payload = json.loads(heartbeat_writes[0][1][2])
        deps = payload["dependencies"]
        assert deps["postgres"]["healthy"] is True
        assert deps["redis"]["healthy"] is False
        assert "down" in deps["redis"]["error"]


class TestToolTracking:
    @pytest.mark.asyncio
    async def test_record_tool_call(self):
        db = FakeDB()
        god = God("tool-god", db)

        await god.record_tool_call("get_status", success=True, latency_ms=50.0)
        await god.record_tool_call("get_status", success=True, latency_ms=30.0)
        await god.record_tool_call("get_status", success=False, latency_ms=100.0)

        stats = god.tool_stats
        assert stats["get_status"]["success"] == 2
        assert stats["get_status"]["failure"] == 1
        assert stats["get_status"]["total_calls"] == 3
        assert stats["get_status"]["avg_latency_ms"] == 60.0  # (50+30+100)/3

    @pytest.mark.asyncio
    async def test_track_tool_context_manager_success(self):
        db = FakeDB()
        god = God("ctx-god", db)

        async with god.track_tool("my_tool"):
            await asyncio.sleep(0.05)  # 50ms — reliable on Windows

        stats = god.tool_stats
        assert stats["my_tool"]["success"] == 1
        assert stats["my_tool"]["failure"] == 0
        assert stats["my_tool"]["avg_latency_ms"] > 0

    @pytest.mark.asyncio
    async def test_track_tool_context_manager_failure(self):
        db = FakeDB()
        god = God("ctx-god", db)

        with pytest.raises(ValueError):
            async with god.track_tool("bad_tool"):
                raise ValueError("boom")

        stats = god.tool_stats
        assert stats["bad_tool"]["success"] == 0
        assert stats["bad_tool"]["failure"] == 1

    @pytest.mark.asyncio
    async def test_tool_calls_write_events(self):
        db = FakeDB()
        god = God("event-god", db)

        await god.record_tool_call("fetch", success=True, latency_ms=25.0)

        tool_writes = [
            (sql, params) for sql, params in db.writes
            if "god_events" in sql and params[1] == "tool_call"
        ]
        assert len(tool_writes) == 1
        payload = json.loads(tool_writes[0][1][2])
        assert payload["tool"] == "fetch"
        assert payload["success"] is True
        assert payload["latency_ms"] == 25.0


class TestEventHelpers:
    @pytest.mark.asyncio
    async def test_log_observation(self):
        db = FakeDB()
        god = God("obs-god", db)

        await god.log_observation("task stalled", {"task_id": 42}, severity="warning")

        assert len(db.writes) == 1
        sql, params = db.writes[0]
        assert params[1] == "observation"
        assert params[3] == "warning"
        payload = json.loads(params[2])
        assert payload["summary"] == "task stalled"
        assert payload["task_id"] == 42

    @pytest.mark.asyncio
    async def test_log_decision(self):
        db = FakeDB()
        god = God("dec-god", db)

        await god.log_decision("retry", "task timed out", 0.85, action="retry_task")

        assert len(db.writes) == 1
        payload = json.loads(db.writes[0][1][2])
        assert payload["type"] == "retry"
        assert payload["confidence"] == 0.85
        assert payload["action"] == "retry_task"

    @pytest.mark.asyncio
    async def test_log_error(self):
        db = FakeDB()
        god = God("err-god", db)

        await god.log_error("connection lost", {"host": "db.local"})

        sql, params = db.writes[0]
        assert params[1] == "error"
        assert params[3] == "error"
        payload = json.loads(params[2])
        assert payload["error"] == "connection lost"
        assert payload["host"] == "db.local"


class TestWriteEvent:
    @pytest.mark.asyncio
    async def test_sql_uses_postgres_style_placeholders(self):
        """Verify we use $N placeholders (codebase standard)."""
        db = FakeDB()
        god = God("sql-god", db)

        await god._write_event("test", {"key": "val"})

        sql, _ = db.writes[0]
        assert "$1" in sql
        assert "$2" in sql
        assert "$3" in sql
        assert "$4" in sql

    @pytest.mark.asyncio
    async def test_null_payload(self):
        db = FakeDB()
        god = God("null-god", db)

        await god._write_event("heartbeat", None)

        _, params = db.writes[0]
        assert params[2] is None  # payload should be None, not "null"


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class TestDiscoverGods:
    def test_discovers_valid_god_json(self, tmp_path: Path):
        # Create two gods
        odin_dir = tmp_path / "odin"
        odin_dir.mkdir()
        (odin_dir / "god.json").write_text(json.dumps({
            "name": "odin",
            "port": 5220,
            "tag": "-eye",
        }))

        ares_dir = tmp_path / "ares"
        ares_dir.mkdir()
        (ares_dir / "god.json").write_text(json.dumps({
            "name": "ares",
            "port": 5221,
        }))

        gods = discover_gods(tmp_path)
        assert len(gods) == 2
        names = {g["name"] for g in gods}
        assert names == {"ares", "odin"}

    def test_skips_malformed_json(self, tmp_path: Path):
        bad_dir = tmp_path / "bad-god"
        bad_dir.mkdir()
        (bad_dir / "god.json").write_text("{invalid json")

        gods = discover_gods(tmp_path)
        assert len(gods) == 0

    def test_infers_name_from_directory(self, tmp_path: Path):
        god_dir = tmp_path / "hermes"
        god_dir.mkdir()
        (god_dir / "god.json").write_text(json.dumps({"port": 5222}))

        gods = discover_gods(tmp_path)
        assert len(gods) == 1
        assert gods[0]["name"] == "hermes"

    def test_adds_path_and_config_path(self, tmp_path: Path):
        god_dir = tmp_path / "zeus"
        god_dir.mkdir()
        (god_dir / "god.json").write_text(json.dumps({"name": "zeus"}))

        gods = discover_gods(tmp_path)
        assert gods[0]["path"] == str(god_dir)
        assert gods[0]["config_path"] == str(god_dir / "god.json")

    def test_empty_directory_returns_empty(self, tmp_path: Path):
        gods = discover_gods(tmp_path)
        assert gods == []

    def test_ignores_files_not_in_subdirs(self, tmp_path: Path):
        # god.json at root level should not match */god.json
        (tmp_path / "god.json").write_text(json.dumps({"name": "root"}))
        gods = discover_gods(tmp_path)
        assert len(gods) == 0


class TestGetGodConfig:
    def test_loads_by_name(self, tmp_path: Path):
        god_dir = tmp_path / "odin"
        god_dir.mkdir()
        (god_dir / "god.json").write_text(json.dumps({
            "name": "odin",
            "port": 5220,
        }))

        config = get_god_config("odin", tmp_path)
        assert config is not None
        assert config["name"] == "odin"
        assert config["port"] == 5220
        assert config["path"] == str(god_dir)

    def test_returns_none_for_missing(self, tmp_path: Path):
        assert get_god_config("nonexistent", tmp_path) is None

    def test_returns_none_for_malformed(self, tmp_path: Path):
        god_dir = tmp_path / "broken"
        god_dir.mkdir()
        (god_dir / "god.json").write_text("not json")

        assert get_god_config("broken", tmp_path) is None

    def test_infers_name_if_missing(self, tmp_path: Path):
        god_dir = tmp_path / "apollo"
        god_dir.mkdir()
        (god_dir / "god.json").write_text(json.dumps({"port": 5225}))

        config = get_god_config("apollo", tmp_path)
        assert config["name"] == "apollo"
