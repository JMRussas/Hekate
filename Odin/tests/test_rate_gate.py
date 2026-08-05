"""Tests for Tyche rate gate enforcement.

Verifies that make_tyche_rate_gate creates a gate that blocks dispatch
when provider token buckets are exhausted, emits rate_limit_hit via
the retry handler, sets retry_after in task context, and respects the
tyche_rate_limit feature flag.
"""

import json
import time
from unittest.mock import patch

import pytest
import pytest_asyncio
import aiosqlite

from conftest import FakeDB, SqliteDB
from gods.pipeline import Event, Emit, GateResult
from gods.rate_limit import ProviderRateLimiter
from gods.handlers.tyche import make_tyche_rate_gate, tyche_retry_rate_limited


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def limiter():
    """ProviderRateLimiter with a tight 2-token bucket for claude_code."""
    return ProviderRateLimiter({
        "claude_code": {"rate": 2, "window_seconds": 60, "burst": 2},
    })


def _dispatch_event(provider: str = "claude_code", task_id: str = "t1",
                     project_id: str = "proj-1") -> Event:
    return Event("dispatch_command", {
        "task_id": task_id,
        "project_id": project_id,
        "provider": provider,
    }, "odin")


def _dispatch_emits(task_id: str = "t1") -> list[Emit]:
    return [Emit("worker_event", {"task_id": task_id, "status": "started"}, "hermes")]


# ---------------------------------------------------------------------------
# Test: dispatch passes when tokens available
# ---------------------------------------------------------------------------

class TestRateGatePass:
    @pytest.mark.asyncio
    async def test_dispatch_passes_when_tokens_available(self, limiter):
        """Gate should allow dispatch when the bucket has tokens."""
        gate = make_tyche_rate_gate(limiter)
        event = _dispatch_event()
        emits = _dispatch_emits()

        result = await gate(event, emits, FakeDB())
        assert result.passed
        assert "OK" in result.reason or "ok" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_no_provider_passes(self, limiter):
        """Gate should pass when event has no provider field."""
        gate = make_tyche_rate_gate(limiter)
        event = Event("dispatch_command", {"task_id": "t1"}, "odin")
        emits = _dispatch_emits()

        result = await gate(event, emits, FakeDB())
        assert result.passed

    @pytest.mark.asyncio
    async def test_unknown_provider_passes(self, limiter):
        """Unknown providers have no bucket — should pass through."""
        gate = make_tyche_rate_gate(limiter)
        event = _dispatch_event(provider="unknown_provider")
        emits = _dispatch_emits()

        result = await gate(event, emits, FakeDB())
        assert result.passed


# ---------------------------------------------------------------------------
# Test: dispatch blocked when tokens exhausted
# ---------------------------------------------------------------------------

class TestRateGateBlock:
    @pytest.mark.asyncio
    async def test_blocks_when_tokens_exhausted(self, limiter):
        """Gate should return GateResult(False) after all tokens consumed."""
        gate = make_tyche_rate_gate(limiter)
        event = _dispatch_event()
        emits = _dispatch_emits()

        # Consume both tokens
        await gate(event, emits, FakeDB())
        await gate(event, emits, FakeDB())

        # Third call should be blocked
        result = await gate(event, emits, FakeDB())
        assert not result.passed
        assert "rate limit" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_blocked_result_has_retry_after(self, limiter):
        """Blocked GateResult details should include retry_after."""
        gate = make_tyche_rate_gate(limiter)
        event = _dispatch_event()
        emits = _dispatch_emits()

        # Exhaust tokens
        await gate(event, emits, FakeDB())
        await gate(event, emits, FakeDB())

        result = await gate(event, emits, FakeDB())
        assert not result.passed
        assert "retry_after" in result.details
        assert result.details["retry_after"] > 0
        assert result.details["provider"] == "claude_code"


# ---------------------------------------------------------------------------
# Test: rate_limit_hit event emitted on block
# ---------------------------------------------------------------------------

class TestRateLimitHitEvent:
    @pytest_asyncio.fixture
    async def task_db(self):
        """SQLite DB with tasks table for retry handler tests."""
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                context_json TEXT DEFAULT '{}'
            );
        """)
        await conn.commit()
        yield SqliteDB(conn)
        await conn.close()

    @pytest.mark.asyncio
    async def test_retry_handler_emits_rate_limit_hit(self, task_db):
        """tyche_retry_rate_limited should emit rate_limit_hit event."""
        # Seed a task
        await task_db.execute_write(
            "INSERT INTO tasks (id, status) VALUES (?, ?)", ("t1", "running")
        )

        # Simulate a gate_failed event from the rate gate
        gate_failed_event = Event("gate_failed", {
            "handler": "tyche_rate_gate",
            "retry_after": 5.0,
            "provider": "claude_code",
            "window_seconds": 60,
            "original_payload": {
                "task_id": "t1",
                "project_id": "proj-1",
            },
        }, "pipeline")

        emits = await tyche_retry_rate_limited(gate_failed_event, task_db)
        assert emits is not None
        assert len(emits) == 1
        assert emits[0].event_type == "rate_limit_hit"
        assert emits[0].payload["provider"] == "claude_code"
        assert emits[0].payload["task_id"] == "t1"
        assert emits[0].payload["retry_after_seconds"] == 5.0


# ---------------------------------------------------------------------------
# Test: blocked dispatch gets retry_after in context_json
# ---------------------------------------------------------------------------

class TestRetryAfterInContext:
    @pytest_asyncio.fixture
    async def task_db(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                context_json TEXT DEFAULT '{}'
            );
        """)
        await conn.commit()
        yield SqliteDB(conn)
        await conn.close()

    @pytest.mark.asyncio
    async def test_sets_retry_after_in_context_json(self, task_db):
        """Retry handler should set retry_after timestamp in task context."""
        await task_db.execute_write(
            "INSERT INTO tasks (id, status, context_json) VALUES (?, ?, ?)",
            ("t1", "running", '{"existing": true}')
        )

        gate_failed_event = Event("gate_failed", {
            "retry_after": 10.0,
            "provider": "claude_code",
            "original_payload": {"task_id": "t1", "project_id": "proj-1"},
        }, "pipeline")

        before = time.time()
        await tyche_retry_rate_limited(gate_failed_event, task_db)
        after = time.time()

        row = await task_db.fetchone("SELECT status, context_json FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "pending"  # Reset to pending for re-dispatch

        ctx = json.loads(row["context_json"])
        assert "retry_after" in ctx
        # retry_after should be ~10 seconds from now
        assert ctx["retry_after"] >= before + 10.0
        assert ctx["retry_after"] <= after + 10.0 + 1.0
        # Existing context preserved
        assert ctx["existing"] is True

    @pytest.mark.asyncio
    async def test_skips_when_no_retry_after(self, task_db):
        """Retry handler should skip gate_failed events without retry_after."""
        event = Event("gate_failed", {
            "handler": "check_plan_created",
            "original_payload": {"task_id": "t1"},
        }, "pipeline")

        result = await tyche_retry_rate_limited(event, task_db)
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_when_no_task_id(self, task_db):
        """Retry handler should skip when original_payload has no task_id."""
        event = Event("gate_failed", {
            "retry_after": 5.0,
            "original_payload": {},
        }, "pipeline")

        result = await tyche_retry_rate_limited(event, task_db)
        assert result is None


# ---------------------------------------------------------------------------
# Test: feature flag tyche_rate_limit=False bypasses the gate
# ---------------------------------------------------------------------------

class TestFeatureFlagBypass:
    @pytest.mark.asyncio
    async def test_flag_disabled_bypasses_gate(self, limiter):
        """Gate should pass unconditionally when tyche_rate_limit flag is False."""
        gate = make_tyche_rate_gate(limiter)
        event = _dispatch_event()
        emits = _dispatch_emits()

        # Exhaust all tokens first
        await gate(event, emits, FakeDB())
        await gate(event, emits, FakeDB())

        # Disable the flag — gate should now pass despite exhausted tokens
        with patch("gods.flags.is_enabled", return_value=False):
            result = await gate(event, emits, FakeDB())
            assert result.passed
            assert "disabled" in result.reason.lower() or "flag" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_flag_enabled_enforces_gate(self, limiter):
        """Gate should enforce rate limits when tyche_rate_limit flag is True."""
        gate = make_tyche_rate_gate(limiter)
        event = _dispatch_event()
        emits = _dispatch_emits()

        # Exhaust tokens
        await gate(event, emits, FakeDB())
        await gate(event, emits, FakeDB())

        with patch("gods.flags.is_enabled", return_value=True):
            result = await gate(event, emits, FakeDB())
            assert not result.passed
