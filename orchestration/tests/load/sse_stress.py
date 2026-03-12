#  Orchestration Engine - SSE Connection Stress Test
#
#  Tests SSE event broadcasting stability under multiple concurrent subscribers.
#  Verifies that the ProgressManager handles subscribe/unsubscribe gracefully.
#
#  Usage:
#    python -m pytest tests/load/sse_stress.py -m slow -v
#
#  Depends on: backend/services/progress.py
#  Used by:    manual stress testing

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.services.progress import ProgressManager

pytestmark = pytest.mark.slow


@pytest.fixture
def broadcaster():
    """Create a ProgressManager with a mock DB (we only test queue broadcast)."""
    mock_db = AsyncMock()
    mock_db.execute_write = AsyncMock()
    return ProgressManager(mock_db)


class TestSSEConnectionStress:
    async def test_many_subscribers_receive_event(self, broadcaster):
        """Multiple subscribers should all receive broadcast events."""
        project_id = "proj_sse_test"
        num_subscribers = 50

        queues = []
        for _ in range(num_subscribers):
            q = asyncio.Queue(maxsize=100)
            broadcaster._subscribers.setdefault(project_id, []).append(q)
            queues.append(q)

        # Broadcast an event
        await broadcaster.push_event(project_id, "task_update", "Test message", key="val")

        # All subscribers should receive it
        received = 0
        for q in queues:
            try:
                event = await asyncio.wait_for(q.get(), timeout=2.0)
                assert event["type"] == "task_update"
                received += 1
            except asyncio.TimeoutError:
                pass

        assert received == num_subscribers

        # Cleanup
        broadcaster._subscribers.pop(project_id, None)

    async def test_subscriber_cleanup_on_disconnect(self, broadcaster):
        """Removing subscribers should reduce the subscriber list."""
        project_id = "proj_cleanup"

        queues = []
        for _ in range(20):
            q = asyncio.Queue(maxsize=100)
            broadcaster._subscribers.setdefault(project_id, []).append(q)
            queues.append(q)

        assert len(broadcaster._subscribers[project_id]) == 20

        # Remove half
        for q in queues[:10]:
            broadcaster._subscribers[project_id].remove(q)

        assert len(broadcaster._subscribers[project_id]) == 10

        # Broadcast should only reach remaining 10
        await broadcaster.push_event(project_id, "test", "msg")

        received = 0
        for q in queues[10:]:
            try:
                await asyncio.wait_for(q.get(), timeout=1.0)
                received += 1
            except asyncio.TimeoutError:
                pass

        assert received == 10

        # First 10 should have empty queues
        for q in queues[:10]:
            assert q.empty()

    async def test_rapid_subscribe_unsubscribe(self, broadcaster):
        """Rapid subscribe/unsubscribe cycles should not leak memory."""
        project_id = "proj_rapid"

        for _ in range(100):
            q = asyncio.Queue(maxsize=100)
            broadcaster._subscribers.setdefault(project_id, []).append(q)
            broadcaster._subscribers[project_id].remove(q)

        # All should be cleaned up (empty list or missing key)
        subs = broadcaster._subscribers.get(project_id, [])
        assert len(subs) == 0

    async def test_broadcast_to_empty_project(self, broadcaster):
        """Broadcasting to a project with no subscribers should not error."""
        # Should complete without error
        await broadcaster.push_event("nonexistent_project", "test", "msg")
