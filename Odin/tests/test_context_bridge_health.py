"""Tests for context bridge health warning events.

Verifies that context_bridge handlers emit context_store_unreachable
events when the context store is down, instead of silently swallowing errors.
"""

import time
from unittest.mock import patch, AsyncMock

import pytest

from gods.pipeline import Event, Emit
from gods.handlers.context_bridge import (
    context_bridge_plan,
    context_bridge_task_verified,
    context_bridge_project_complete,
    _health_warning,
)
from conftest import FakeDB


class TestHealthWarningHelper:
    def test_creates_correct_emit(self):
        """_health_warning should produce a context_store_unreachable Emit."""
        emit = _health_warning("test_handler", "connection refused")
        assert emit.event_type == "context_store_unreachable"
        assert emit.payload["handler"] == "test_handler"
        assert emit.payload["error"] == "connection refused"
        assert emit.severity == "warning"
        assert emit.source == "context_bridge"

    def test_idempotency_key_5min_bucket(self):
        """Two calls within same 5-min window should produce same key."""
        with patch("gods.handlers.context_bridge.time") as mock_time:
            mock_time.time.return_value = 900.0  # 900 // 300 = 3
            emit1 = _health_warning("handler_a", "err1")
            mock_time.time.return_value = 1100.0  # 1100 // 300 = 3 (same bucket)
            emit2 = _health_warning("handler_a", "err2")

        assert emit1.idempotency_key == emit2.idempotency_key

    def test_idempotency_key_different_buckets(self):
        """Calls in different 5-min windows should produce different keys."""
        with patch("gods.handlers.context_bridge.time") as mock_time:
            mock_time.time.return_value = 1000.0
            emit1 = _health_warning("handler_a", "err1")
            mock_time.time.return_value = 1400.0  # 400s later, different bucket
            emit2 = _health_warning("handler_a", "err2")

        assert emit1.idempotency_key != emit2.idempotency_key


class TestContextBridgePlanHealth:
    @pytest.mark.asyncio
    async def test_emits_warning_when_store_unreachable(self):
        """context_bridge_plan should emit health warning when context store is down."""
        db = FakeDB()
        event = Event("project_planned", {"project_id": "p1", "plan_id": "plan-1"}, "athena")

        with patch("gods.handlers.context_bridge._ensure_project", new_callable=AsyncMock) as mock:
            mock.return_value = None  # Context store unreachable
            result = await context_bridge_plan(event, db)

        assert result is not None
        assert len(result) == 1
        assert result[0].event_type == "context_store_unreachable"
        assert result[0].payload["handler"] == "context_bridge_plan"

    @pytest.mark.asyncio
    async def test_returns_none_on_missing_project_id(self):
        """Should return None early if no project_id (not a health issue)."""
        db = FakeDB()
        event = Event("project_planned", {}, "athena")

        result = await context_bridge_plan(event, db)
        assert result is None


class TestContextBridgeTaskVerifiedHealth:
    @pytest.mark.asyncio
    async def test_emits_warning_when_store_unreachable(self):
        """context_bridge_task_verified should emit health warning."""
        db = FakeDB()
        event = Event("task_verified", {"task_id": "t1", "project_id": "p1"}, "mimir")

        with patch("gods.handlers.context_bridge._ensure_project", new_callable=AsyncMock) as mock:
            mock.return_value = None
            result = await context_bridge_task_verified(event, db)

        assert result is not None
        assert len(result) == 1
        assert result[0].event_type == "context_store_unreachable"
        assert result[0].payload["handler"] == "context_bridge_task_verified"


class TestContextBridgeProjectCompleteHealth:
    @pytest.mark.asyncio
    async def test_emits_warning_when_store_unreachable(self):
        """context_bridge_project_complete should emit health warning."""
        db = FakeDB()
        event = Event("project_complete", {"project_id": "p1"}, "odin")

        with patch("gods.handlers.context_bridge._ensure_project", new_callable=AsyncMock) as mock:
            mock.return_value = None
            result = await context_bridge_project_complete(event, db)

        assert result is not None
        assert len(result) == 1
        assert result[0].event_type == "context_store_unreachable"
        assert result[0].payload["handler"] == "context_bridge_project_complete"


class TestGracefulDegradation:
    @pytest.mark.asyncio
    async def test_handler_does_not_raise(self):
        """Health warnings should be returned, not raised — pipeline must not block."""
        db = FakeDB()
        event = Event("project_planned", {"project_id": "p1"}, "athena")

        with patch("gods.handlers.context_bridge._ensure_project", new_callable=AsyncMock) as mock:
            mock.return_value = None
            # Should not raise
            result = await context_bridge_plan(event, db)

        assert isinstance(result, list)
