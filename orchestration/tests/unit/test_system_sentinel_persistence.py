#  Tests for SystemSentinel observation persistence
#
#  Verifies: Health state changes, contention events, and Plan Sentinel
#  lifecycle events are persisted via SentinelContextClient.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from backend.models.enums import ResourceStatus
from backend.services.sentinel.bus import SentinelBus
from backend.services.sentinel.models import (
    HealthState,
    SentinelObservation,
    Severity,
)
from backend.services.sentinel.system_sentinel import (
    SYSTEM_SENTINEL_PARENT,
    SystemSentinel,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_resource_state(resource_id: str, status: ResourceStatus, response_time_ms: float | None = None):
    """Simulate a ResourceState from ResourceMonitor.check_all()."""
    state = MagicMock()
    state.id = resource_id
    state.status = status
    state.response_time_ms = response_time_ms
    return state


def _make_row(project_id: str, model_tier: str) -> dict:
    return {"project_id": project_id, "model_tier": model_tier}


def _fake_resource_monitor(states=None) -> MagicMock:
    rm = MagicMock()
    rm.check_all = AsyncMock(return_value=states or [])
    return rm


def _fake_context_client() -> MagicMock:
    cc = MagicMock()
    cc.save_observation = AsyncMock(return_value="obs-123")
    cc.close = AsyncMock()
    return cc


def _fake_db(rows: list[dict]) -> MagicMock:
    db = MagicMock()
    db.fetchall = AsyncMock(return_value=rows)
    return db


# ---------------------------------------------------------------------------
# Health state change persistence
# ---------------------------------------------------------------------------

class TestHealthStatePersistence:
    """Health state transitions persist sentinel_observation nodes."""

    @pytest.mark.asyncio
    async def test_state_change_persists_observation(self):
        """When a resource transitions from HEALTHY to DOWN, an observation is saved."""
        context_client = _fake_context_client()

        # Resource starts offline → after enough samples, state changes
        states = [_make_resource_state("ollama", ResourceStatus.OFFLINE)]
        rm = _fake_resource_monitor(states)

        sentinel = SystemSentinel(
            resource_monitor=rm,
            context_client=context_client,
            window_size=1,  # single sample triggers state change immediately
        )

        await sentinel._tick()

        # Verify save_observation was called
        context_client.save_observation.assert_called_once()
        obs_arg = context_client.save_observation.call_args[0][0]
        assert isinstance(obs_arg, SentinelObservation)
        assert obs_arg.category == "health_state_change"
        assert obs_arg.severity == Severity.CRITICAL  # DOWN → critical

        # System-level observation uses SYSTEM_SENTINEL_PARENT
        parent_arg = context_client.save_observation.call_args[1].get("parent_id", SYSTEM_SENTINEL_PARENT)

    @pytest.mark.asyncio
    async def test_no_state_change_no_persistence(self):
        """When health state doesn't change, no observation is saved."""
        context_client = _fake_context_client()

        # Resource is online — stays HEALTHY
        states = [_make_resource_state("ollama", ResourceStatus.ONLINE, response_time_ms=50.0)]
        rm = _fake_resource_monitor(states)

        sentinel = SystemSentinel(
            resource_monitor=rm,
            context_client=context_client,
            window_size=5,
        )

        # Initial tick — state starts HEALTHY, stays HEALTHY
        await sentinel._tick()
        context_client.save_observation.assert_not_called()

    @pytest.mark.asyncio
    async def test_persistence_failure_does_not_raise(self):
        """If the context client fails, the tick still completes."""
        context_client = _fake_context_client()
        context_client.save_observation = AsyncMock(side_effect=Exception("connection refused"))

        states = [_make_resource_state("ollama", ResourceStatus.OFFLINE)]
        rm = _fake_resource_monitor(states)

        sentinel = SystemSentinel(
            resource_monitor=rm,
            context_client=context_client,
            window_size=1,
        )

        # Should not raise
        await sentinel._tick()

    @pytest.mark.asyncio
    async def test_no_context_client_skips_persistence(self):
        """When no context client is provided, persistence is silently skipped."""
        states = [_make_resource_state("ollama", ResourceStatus.OFFLINE)]
        rm = _fake_resource_monitor(states)

        sentinel = SystemSentinel(
            resource_monitor=rm,
            context_client=None,
            window_size=1,
        )

        # Should not raise — just skips persistence
        await sentinel._tick()


# ---------------------------------------------------------------------------
# Plan Sentinel lifecycle persistence
# ---------------------------------------------------------------------------

class TestLifecyclePersistence:
    """Plan Sentinel spawn/teardown events persist observations."""

    @pytest.mark.asyncio
    async def test_spawn_persists_observation(self):
        """Spawning a Plan Sentinel persists a lifecycle observation."""
        context_client = _fake_context_client()
        rm = _fake_resource_monitor()

        sentinel = SystemSentinel(
            resource_monitor=rm,
            context_client=context_client,
        )

        await sentinel.spawn_plan_sentinel("proj-42")

        context_client.save_observation.assert_called_once()
        obs = context_client.save_observation.call_args[0][0]
        assert obs.category == "lifecycle"
        assert obs.project_id == "proj-42"
        assert "spawned" in obs.message.lower()
        assert obs.details["event"] == "plan_sentinel_spawned"

        # Clean up
        await sentinel.teardown_plan_sentinel("proj-42")

    @pytest.mark.asyncio
    async def test_teardown_persists_observation(self):
        """Tearing down a Plan Sentinel persists a lifecycle observation."""
        context_client = _fake_context_client()
        rm = _fake_resource_monitor()

        sentinel = SystemSentinel(
            resource_monitor=rm,
            context_client=context_client,
        )

        await sentinel.spawn_plan_sentinel("proj-42")
        context_client.save_observation.reset_mock()

        await sentinel.teardown_plan_sentinel("proj-42")

        context_client.save_observation.assert_called_once()
        obs = context_client.save_observation.call_args[0][0]
        assert obs.category == "lifecycle"
        assert obs.project_id == "proj-42"
        assert "torn down" in obs.message.lower()
        assert obs.details["event"] == "plan_sentinel_teardown"

    @pytest.mark.asyncio
    async def test_teardown_nonexistent_no_persistence(self):
        """Tearing down a nonexistent sentinel doesn't persist anything."""
        context_client = _fake_context_client()
        rm = _fake_resource_monitor()

        sentinel = SystemSentinel(
            resource_monitor=rm,
            context_client=context_client,
        )

        result = await sentinel.teardown_plan_sentinel("nonexistent")
        assert result is False
        context_client.save_observation.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_spawn_no_extra_persistence(self):
        """Spawning the same project twice doesn't persist a second observation."""
        context_client = _fake_context_client()
        rm = _fake_resource_monitor()

        sentinel = SystemSentinel(
            resource_monitor=rm,
            context_client=context_client,
        )

        await sentinel.spawn_plan_sentinel("proj-42")
        context_client.save_observation.reset_mock()

        # Second spawn returns existing — no new persistence
        await sentinel.spawn_plan_sentinel("proj-42")
        context_client.save_observation.assert_not_called()

        # Clean up
        await sentinel.teardown_plan_sentinel("proj-42")


# ---------------------------------------------------------------------------
# Contention persistence
# ---------------------------------------------------------------------------

class TestContentionPersistence:
    """Contention events persist observations to context store."""

    @pytest.mark.asyncio
    async def test_semaphore_contention_persists_observation(self):
        """Semaphore contention saves a resource_contention observation."""
        context_client = _fake_context_client()
        bus = SentinelBus()
        rows = [
            _make_row("proj-a", "claude_code"),
            _make_row("proj-a", "claude_code"),
            _make_row("proj-b", "claude_code"),
        ]
        db = _fake_db(rows)

        sentinel = SystemSentinel(
            resource_monitor=_fake_resource_monitor(),
            bus=bus,
            context_client=context_client,
            db=db,
            max_concurrent_tasks=3,
        )
        sentinel._plan_sentinels["proj-a"] = MagicMock()
        sentinel._plan_sentinels["proj-b"] = MagicMock()

        await sentinel._detect_contention()

        # Find the contention observation call
        calls = context_client.save_observation.call_args_list
        contention_obs = [
            c[0][0] for c in calls
            if isinstance(c[0][0], SentinelObservation)
            and c[0][0].category == "resource_contention"
            and c[0][0].details.get("kind") == "semaphore"
        ]
        assert len(contention_obs) == 1
        obs = contention_obs[0]
        assert obs.severity == Severity.WARNING
        assert "semaphore" in obs.message.lower()
        assert obs.details["utilization"] == 1.0

    @pytest.mark.asyncio
    async def test_tier_contention_persists_observation(self):
        """Model tier contention saves a resource_contention observation."""
        context_client = _fake_context_client()
        bus = SentinelBus()
        rows = [
            _make_row("proj-a", "ollama"),
            _make_row("proj-b", "ollama"),
        ]
        db = _fake_db(rows)

        sentinel = SystemSentinel(
            resource_monitor=_fake_resource_monitor(),
            bus=bus,
            context_client=context_client,
            db=db,
            max_concurrent_tasks=10,
        )
        sentinel._plan_sentinels["proj-a"] = MagicMock()
        sentinel._plan_sentinels["proj-b"] = MagicMock()

        await sentinel._detect_contention()

        calls = context_client.save_observation.call_args_list
        tier_obs = [
            c[0][0] for c in calls
            if isinstance(c[0][0], SentinelObservation)
            and c[0][0].category == "resource_contention"
            and c[0][0].details.get("kind") == "model_tier"
        ]
        assert len(tier_obs) == 1
        obs = tier_obs[0]
        assert obs.details["tier"] == "ollama"
        assert sorted(obs.details["contending_projects"]) == ["proj-a", "proj-b"]

    @pytest.mark.asyncio
    async def test_dedup_contention_no_repeat_persistence(self):
        """Repeated ticks with same contention don't persist duplicate observations."""
        context_client = _fake_context_client()
        bus = SentinelBus()
        rows = [
            _make_row("proj-a", "ollama"),
            _make_row("proj-b", "ollama"),
        ]
        db = _fake_db(rows)

        sentinel = SystemSentinel(
            resource_monitor=_fake_resource_monitor(),
            bus=bus,
            context_client=context_client,
            db=db,
            max_concurrent_tasks=10,
        )
        sentinel._plan_sentinels["proj-a"] = MagicMock()
        sentinel._plan_sentinels["proj-b"] = MagicMock()

        await sentinel._detect_contention()
        first_count = context_client.save_observation.call_count

        await sentinel._detect_contention()
        assert context_client.save_observation.call_count == first_count
