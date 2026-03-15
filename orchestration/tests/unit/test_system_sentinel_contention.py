#  Tests for SystemSentinel contention detection
#
#  Verifies: Two projects requesting the same scarce resource triggers
#  a contention advisory message on the event bus.

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.sentinel.bus import SentinelBus
from backend.services.sentinel.models import SentinelMessage
from backend.services.sentinel.system_sentinel import SystemSentinel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(project_id: str, model_tier: str) -> dict:
    """Simulate a DB row dict."""
    return {"project_id": project_id, "model_tier": model_tier}


def _fake_resource_monitor() -> MagicMock:
    rm = MagicMock()
    rm.check_all = AsyncMock(return_value=[])
    return rm


def _fake_db(rows: list[dict]) -> MagicMock:
    db = MagicMock()
    db.fetchall = AsyncMock(return_value=rows)
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSemaphoreContention:
    """Semaphore (slot) contention across projects."""

    @pytest.mark.asyncio
    async def test_two_projects_saturating_slots_triggers_advisory(self):
        """Two projects using >=80% of slots emits contention_advisory."""
        bus = SentinelBus()
        collected: list[SentinelMessage] = []
        bus.on("contention_advisory", AsyncMock(side_effect=lambda m: collected.append(m)))

        # 3 slots total, 3 tasks across 2 projects → 100% utilization
        rows = [
            _make_row("proj-a", "claude_code"),
            _make_row("proj-a", "claude_code"),
            _make_row("proj-b", "claude_code"),
        ]
        db = _fake_db(rows)

        sentinel = SystemSentinel(
            resource_monitor=_fake_resource_monitor(),
            bus=bus,
            db=db,
            max_concurrent_tasks=3,
        )

        # Register plan sentinels so contention detection runs
        sentinel._plan_sentinels["proj-a"] = MagicMock()
        sentinel._plan_sentinels["proj-b"] = MagicMock()

        await sentinel._detect_contention()

        # Should get one advisory per project
        assert len(collected) == 2
        payloads = {m.payload["project_id"] for m in collected}
        assert payloads == {"proj-a", "proj-b"}
        assert all(m.payload["kind"] == "semaphore" for m in collected)
        assert all(m.payload["utilization"] == 1.0 for m in collected)

    @pytest.mark.asyncio
    async def test_below_threshold_no_advisory(self):
        """Usage below 80% does not trigger advisory."""
        bus = SentinelBus()
        collected: list[SentinelMessage] = []
        bus.on("contention_advisory", AsyncMock(side_effect=lambda m: collected.append(m)))

        # 2/5 slots = 40% — below threshold
        rows = [
            _make_row("proj-a", "claude_code"),
            _make_row("proj-b", "claude_code"),
        ]
        db = _fake_db(rows)

        sentinel = SystemSentinel(
            resource_monitor=_fake_resource_monitor(),
            bus=bus,
            db=db,
            max_concurrent_tasks=5,
        )
        sentinel._plan_sentinels["proj-a"] = MagicMock()
        sentinel._plan_sentinels["proj-b"] = MagicMock()

        await sentinel._detect_contention()
        assert len(collected) == 0

    @pytest.mark.asyncio
    async def test_single_project_no_cross_plan_contention(self):
        """One project using all slots is not cross-plan contention."""
        bus = SentinelBus()
        collected: list[SentinelMessage] = []
        bus.on("contention_advisory", AsyncMock(side_effect=lambda m: collected.append(m)))

        rows = [
            _make_row("proj-a", "claude_code"),
            _make_row("proj-a", "claude_code"),
            _make_row("proj-a", "claude_code"),
        ]
        db = _fake_db(rows)

        sentinel = SystemSentinel(
            resource_monitor=_fake_resource_monitor(),
            bus=bus,
            db=db,
            max_concurrent_tasks=3,
        )
        sentinel._plan_sentinels["proj-a"] = MagicMock()

        await sentinel._detect_contention()
        assert len(collected) == 0


class TestTierContention:
    """Model tier contention across projects."""

    @pytest.mark.asyncio
    async def test_two_projects_same_tier_triggers_advisory(self):
        """Two projects with tasks on the same model tier emit tier contention."""
        bus = SentinelBus()
        collected: list[SentinelMessage] = []
        bus.on("contention_advisory", AsyncMock(side_effect=lambda m: collected.append(m)))

        rows = [
            _make_row("proj-a", "ollama"),
            _make_row("proj-b", "ollama"),
        ]
        db = _fake_db(rows)

        sentinel = SystemSentinel(
            resource_monitor=_fake_resource_monitor(),
            bus=bus,
            db=db,
            max_concurrent_tasks=10,  # high so semaphore doesn't fire
        )
        sentinel._plan_sentinels["proj-a"] = MagicMock()
        sentinel._plan_sentinels["proj-b"] = MagicMock()

        await sentinel._detect_contention()

        tier_msgs = [m for m in collected if m.payload.get("kind") == "model_tier"]
        assert len(tier_msgs) == 2
        assert {m.payload["project_id"] for m in tier_msgs} == {"proj-a", "proj-b"}
        assert all(m.payload["tier"] == "ollama" for m in tier_msgs)

    @pytest.mark.asyncio
    async def test_different_tiers_no_contention(self):
        """Projects on different tiers don't trigger tier contention."""
        bus = SentinelBus()
        collected: list[SentinelMessage] = []
        bus.on("contention_advisory", AsyncMock(side_effect=lambda m: collected.append(m)))

        rows = [
            _make_row("proj-a", "ollama"),
            _make_row("proj-b", "claude_code"),
        ]
        db = _fake_db(rows)

        sentinel = SystemSentinel(
            resource_monitor=_fake_resource_monitor(),
            bus=bus,
            db=db,
            max_concurrent_tasks=10,
        )
        sentinel._plan_sentinels["proj-a"] = MagicMock()
        sentinel._plan_sentinels["proj-b"] = MagicMock()

        await sentinel._detect_contention()
        assert len(collected) == 0

    @pytest.mark.asyncio
    async def test_dedup_same_contention_set(self):
        """Repeated ticks with same contending set don't re-fire advisories."""
        bus = SentinelBus()
        collected: list[SentinelMessage] = []
        bus.on("contention_advisory", AsyncMock(side_effect=lambda m: collected.append(m)))

        rows = [
            _make_row("proj-a", "ollama"),
            _make_row("proj-b", "ollama"),
        ]
        db = _fake_db(rows)

        sentinel = SystemSentinel(
            resource_monitor=_fake_resource_monitor(),
            bus=bus,
            db=db,
            max_concurrent_tasks=10,
        )
        sentinel._plan_sentinels["proj-a"] = MagicMock()
        sentinel._plan_sentinels["proj-b"] = MagicMock()

        await sentinel._detect_contention()
        first_count = len(collected)
        assert first_count > 0

        # Second tick with same state — no new messages
        await sentinel._detect_contention()
        assert len(collected) == first_count

    @pytest.mark.asyncio
    async def test_contention_clears_when_resolved(self):
        """When contention resolves (one project finishes), state is cleared."""
        bus = SentinelBus()
        collected: list[SentinelMessage] = []
        bus.on("contention_advisory", AsyncMock(side_effect=lambda m: collected.append(m)))

        # First tick: contention
        rows_contention = [
            _make_row("proj-a", "ollama"),
            _make_row("proj-b", "ollama"),
        ]
        db = _fake_db(rows_contention)

        sentinel = SystemSentinel(
            resource_monitor=_fake_resource_monitor(),
            bus=bus,
            db=db,
            max_concurrent_tasks=10,
        )
        sentinel._plan_sentinels["proj-a"] = MagicMock()
        sentinel._plan_sentinels["proj-b"] = MagicMock()

        await sentinel._detect_contention()
        assert len(collected) == 2

        # Second tick: proj-b done, only proj-a remains
        db.fetchall = AsyncMock(return_value=[_make_row("proj-a", "ollama")])
        await sentinel._detect_contention()

        # No new messages, but internal state cleared
        assert sentinel._last_contention.get("ollama") is None
