#  Orchestration Engine - Progress Subscribe Tests
#
#  Tests for ProgressManager.subscribe() async generator.
#
#  Depends on: backend/services/progress.py, backend/db/connection.py
#  Used by:    pytest

import asyncio
import json

import pytest

from backend.services.progress import ProgressManager
from tests.conftest import create_test_project, create_test_task


# ---------------------------------------------------------------------------
# TestProgressSubscribe
# ---------------------------------------------------------------------------

class TestProgressSubscribe:

    async def test_yields_event_after_push(self, tmp_db):
        """subscribe() yields SSE-formatted event after push_event."""
        await create_test_project(tmp_db, "proj_001")
        await create_test_task(tmp_db, "t1", "proj_001")
        pm = ProgressManager(db=tmp_db)

        gen = pm.subscribe("proj_001")
        # Start consuming in a task
        task = asyncio.create_task(gen.__anext__())
        # Let the generator register its queue
        await asyncio.sleep(0.05)

        # Push an event
        await pm.push_event("proj_001", "task_start", "Starting task A", task_id="t1")

        chunk = await asyncio.wait_for(task, timeout=2.0)
        assert "event: task_start" in chunk
        assert "Starting task A" in chunk

        await gen.aclose()

    async def test_keepalive_on_timeout(self, tmp_db):
        """subscribe() yields keepalive when no events arrive within timeout."""
        pm = ProgressManager(db=tmp_db)

        # Patch the timeout to be very short so test doesn't wait 30s
        async def fast_subscribe(project_id):
            queue = asyncio.Queue(maxsize=100)
            pm._subscribers.setdefault(project_id, []).append(queue)
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.1)
                        yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                        if event.get("type") in ("project_complete", "project_failed"):
                            break
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                subs = pm._subscribers.get(project_id, [])
                if queue in subs:
                    subs.remove(queue)
                if not subs and project_id in pm._subscribers:
                    del pm._subscribers[project_id]

        gen = fast_subscribe("proj_002")
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert chunk == ": keepalive\n\n"

        await gen.aclose()

    async def test_terminal_event_breaks_generator(self, tmp_db):
        """subscribe() stops after project_complete event."""
        await create_test_project(tmp_db, "proj_003")
        pm = ProgressManager(db=tmp_db)

        gen = pm.subscribe("proj_003")
        task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0.05)

        await pm.push_event("proj_003", "project_complete", "All done")
        chunk = await asyncio.wait_for(task, timeout=2.0)
        assert "project_complete" in chunk

        # Generator should be exhausted
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    async def test_cleanup_removes_subscriber(self, tmp_db):
        """Closing the generator removes the subscriber."""
        pm = ProgressManager(db=tmp_db)

        gen = pm.subscribe("proj_004")
        # Let it register
        task = asyncio.create_task(gen.__anext__())
        await asyncio.sleep(0.05)

        assert "proj_004" in pm._subscribers
        assert len(pm._subscribers["proj_004"]) == 1

        # Cancel and close
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        await gen.aclose()

        # Subscriber should be cleaned up
        assert "proj_004" not in pm._subscribers

    async def test_concurrent_subscribers(self, tmp_db):
        """Two subscribers on the same project both receive events."""
        await create_test_project(tmp_db, "proj_005")
        pm = ProgressManager(db=tmp_db)

        gen1 = pm.subscribe("proj_005")
        gen2 = pm.subscribe("proj_005")

        t1 = asyncio.create_task(gen1.__anext__())
        t2 = asyncio.create_task(gen2.__anext__())
        await asyncio.sleep(0.05)

        await pm.push_event("proj_005", "task_start", "Starting")

        chunk1 = await asyncio.wait_for(t1, timeout=2.0)
        chunk2 = await asyncio.wait_for(t2, timeout=2.0)

        assert "task_start" in chunk1
        assert "task_start" in chunk2

        await gen1.aclose()
        await gen2.aclose()
