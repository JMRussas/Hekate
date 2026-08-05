"""Tests for Tyche budget gate enforcement.

Verifies that make_tyche_budget_gate creates a gate that blocks dispatch
when daily or per-project spend limits are exceeded.
"""

import json
import time

import pytest
import pytest_asyncio
import aiosqlite

from conftest import SqliteDB
from gods.pipeline import Event, Emit, GateResult
from gods.handlers.tyche import make_tyche_budget_gate, tyche_record_spend


@pytest_asyncio.fixture
async def budget_db():
    """SQLite DB with relay table for budget gate tests."""
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
    """)
    await conn.commit()
    yield SqliteDB(conn)
    await conn.close()


async def _seed_spend(db, project_id: str, cost_usd: float, created_at: float = None):
    """Insert a budget_spent event into the relay table."""
    if created_at is None:
        created_at = time.time()
    payload = json.dumps({
        "task_id": "t-fake",
        "project_id": project_id,
        "cost_usd": cost_usd,
    })
    await db.execute_write(
        "INSERT INTO god_relay_events (event_type, source, payload, severity, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("budget_spent", "tyche", payload, "info", created_at),
    )


class TestTycheBudgetGate:
    @pytest.mark.asyncio
    async def test_blocks_when_daily_limit_exceeded(self, budget_db):
        """Gate should block dispatch when daily spend exceeds limit."""
        # Seed $4.50 in daily spend (limit is $5.00)
        await _seed_spend(budget_db, "proj-1", 4.50)

        gate = make_tyche_budget_gate({"daily_limit_usd": 5.0})
        event = Event("project_tick", {
            "project_id": "proj-1",
            "estimated_cost": 1.00,  # Would push over $5
        }, "odin")
        emits = [Emit("dispatch_command", {"task_id": "t1"}, "odin")]

        result = await gate(event, emits, budget_db)
        assert not result.passed
        assert "budget" in result.reason.lower() or "insufficient" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_allows_when_under_limit(self, budget_db):
        """Gate should allow dispatch when spend is under limit."""
        await _seed_spend(budget_db, "proj-1", 1.00)

        gate = make_tyche_budget_gate({"daily_limit_usd": 5.0})
        event = Event("project_tick", {
            "project_id": "proj-1",
            "estimated_cost": 0.50,
        }, "odin")
        emits = [Emit("dispatch_command", {"task_id": "t1"}, "odin")]

        result = await gate(event, emits, budget_db)
        assert result.passed

    @pytest.mark.asyncio
    async def test_blocks_when_project_limit_exceeded(self, budget_db):
        """Gate should block when per-project spend exceeds project limit."""
        await _seed_spend(budget_db, "proj-1", 8.00)

        gate = make_tyche_budget_gate({
            "daily_limit_usd": 50.0,  # High daily limit
            "per_project_limit_usd": 10.0,  # Low project limit
        })
        event = Event("project_tick", {
            "project_id": "proj-1",
            "estimated_cost": 3.00,  # Would push project over $10
        }, "odin")
        emits = [Emit("dispatch_command", {"task_id": "t1"}, "odin")]

        result = await gate(event, emits, budget_db)
        assert not result.passed

    @pytest.mark.asyncio
    async def test_project_isolation(self, budget_db):
        """Project A at limit should not block project B."""
        await _seed_spend(budget_db, "proj-a", 9.50)  # Near limit
        await _seed_spend(budget_db, "proj-b", 1.00)  # Well under

        gate = make_tyche_budget_gate({
            "daily_limit_usd": 50.0,
            "per_project_limit_usd": 10.0,
        })

        # Project B should still be allowed
        event = Event("project_tick", {
            "project_id": "proj-b",
            "estimated_cost": 0.50,
        }, "odin")
        emits = [Emit("dispatch_command", {"task_id": "t1"}, "odin")]

        result = await gate(event, emits, budget_db)
        assert result.passed

    @pytest.mark.asyncio
    async def test_allows_with_no_prior_spend(self, budget_db):
        """Gate should allow dispatch when there's no prior spend."""
        gate = make_tyche_budget_gate({"daily_limit_usd": 5.0})
        event = Event("project_tick", {
            "project_id": "proj-new",
            "estimated_cost": 0.10,
        }, "odin")
        emits = [Emit("dispatch_command", {"task_id": "t1"}, "odin")]

        result = await gate(event, emits, budget_db)
        assert result.passed

    @pytest.mark.asyncio
    async def test_ignores_old_spend(self, budget_db):
        """Gate should not count spend from previous days."""
        yesterday = time.time() - 86400 - 100  # More than 1 day ago
        await _seed_spend(budget_db, "proj-1", 100.00, created_at=yesterday)

        gate = make_tyche_budget_gate({"daily_limit_usd": 5.0})
        event = Event("project_tick", {
            "project_id": "proj-1",
            "estimated_cost": 1.00,
        }, "odin")
        emits = [Emit("dispatch_command", {"task_id": "t1"}, "odin")]

        result = await gate(event, emits, budget_db)
        assert result.passed


class TestTycheRecordSpend:
    @pytest.mark.asyncio
    async def test_emits_budget_spent_on_completion(self):
        """tyche_record_spend should emit budget_spent for completed tasks."""
        from conftest import FakeDB
        db = FakeDB()
        event = Event("worker_event", {
            "task_id": "t1",
            "project_id": "proj-1",
            "status": "completed",
            "cost_usd": 0.15,
            "model_used": "claude-sonnet-4",
        }, "hermes")

        emits = await tyche_record_spend(event, db)
        assert emits is not None
        assert len(emits) == 1
        assert emits[0].event_type == "budget_spent"
        assert emits[0].payload["cost_usd"] == 0.15

    @pytest.mark.asyncio
    async def test_skips_failed_tasks(self):
        """Should not emit for failed tasks."""
        from conftest import FakeDB
        db = FakeDB()
        event = Event("worker_event", {
            "task_id": "t1",
            "status": "failed",
            "cost_usd": 0.05,
        }, "hermes")

        emits = await tyche_record_spend(event, db)
        assert emits is None

    @pytest.mark.asyncio
    async def test_skips_zero_cost(self):
        """Should not emit for zero-cost tasks."""
        from conftest import FakeDB
        db = FakeDB()
        event = Event("worker_event", {
            "task_id": "t1",
            "status": "completed",
            "cost_usd": 0.0,
        }, "hermes")

        emits = await tyche_record_spend(event, db)
        assert emits is None
