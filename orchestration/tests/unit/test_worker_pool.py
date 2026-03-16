"""Unit tests for WorkerPool — sentinel worker abstraction layer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.sentinel.worker_pool import WorkerPool, WorkerStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    return MagicMock()


@pytest.fixture
def semaphore():
    return asyncio.Semaphore(4)


@pytest.fixture
def progress():
    return MagicMock()


@pytest.fixture
def pool(db, semaphore, progress):
    return WorkerPool(db=db, semaphore=semaphore, progress_manager=progress)


def _dummy_kwargs(task_row=None):
    """Minimal kwargs expected by execute_task (all mocked)."""
    return {
        "task_row": task_row or {"id": "t1", "project_id": "p1"},
        "budget": MagicMock(),
        "tool_registry": MagicMock(),
        "http_client": MagicMock(),
        "client": MagicMock(),
        "dispatched": set(),
        "retry_after": {},
    }


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_pool_stores_dependencies(self, db, semaphore, progress):
        pool = WorkerPool(db=db, semaphore=semaphore, progress_manager=progress)
        assert pool._db is db
        assert pool._semaphore is semaphore
        assert pool._progress is progress

    def test_pool_starts_empty(self, pool: WorkerPool):
        assert pool._tasks == {}
        assert pool._statuses == {}


# ---------------------------------------------------------------------------
# available_slots
# ---------------------------------------------------------------------------

class TestAvailableSlots:
    def test_initial_slots(self, pool: WorkerPool):
        assert pool.available_slots == 4

    @pytest.mark.anyio
    async def test_slots_decrease_under_semaphore(self, pool: WorkerPool):
        async with pool._semaphore:
            assert pool.available_slots == 3


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    @pytest.mark.anyio
    async def test_dispatch_returns_task(self, pool: WorkerPool):
        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            new_callable=AsyncMock,
            return_value={"status": "completed"},
        ):
            handle = pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            assert isinstance(handle, asyncio.Task)
            await handle

    @pytest.mark.anyio
    async def test_dispatch_sets_status_to_running(self, pool: WorkerPool):
        started = asyncio.Event()
        finished = asyncio.Event()

        async def _slow(**kw):
            started.set()
            await finished.wait()
            return {"status": "completed"}

        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            side_effect=_slow,
        ):
            pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            await started.wait()
            assert pool._statuses["t1"] == WorkerStatus.RUNNING
            finished.set()

    @pytest.mark.anyio
    async def test_dispatch_duplicate_raises(self, pool: WorkerPool):
        started = asyncio.Event()
        finished = asyncio.Event()

        async def _slow(**kw):
            started.set()
            await finished.wait()

        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            side_effect=_slow,
        ):
            pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            await started.wait()
            with pytest.raises(RuntimeError, match="already dispatched"):
                pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            finished.set()

    @pytest.mark.anyio
    async def test_dispatch_forwards_kwargs_to_execute_task(self, pool: WorkerPool):
        mock_exec = AsyncMock(return_value={"status": "completed"})
        kwargs = _dummy_kwargs()

        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            mock_exec,
        ):
            handle = pool.dispatch("t1", "haiku", **kwargs)
            await handle

        mock_exec.assert_called_once()
        call_kwargs = mock_exec.call_args.kwargs
        # Pool injects its own db, semaphore, progress
        assert call_kwargs["db"] is pool._db
        assert call_kwargs["semaphore"] is pool._semaphore
        assert call_kwargs["progress"] is pool._progress
        # Forwarded kwargs preserved
        assert call_kwargs["task_row"] == kwargs["task_row"]
        assert call_kwargs["budget"] is kwargs["budget"]

    @pytest.mark.anyio
    async def test_completed_task_has_completed_status(self, pool: WorkerPool):
        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            new_callable=AsyncMock,
            return_value={"status": "completed"},
        ):
            handle = pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            await handle

        # Allow done callback to fire
        await asyncio.sleep(0)
        assert pool._statuses["t1"] == WorkerStatus.COMPLETED

    @pytest.mark.anyio
    async def test_failed_task_has_failed_status(self, pool: WorkerPool):
        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            handle = pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            with pytest.raises(RuntimeError):
                await handle

        await asyncio.sleep(0)
        assert pool._statuses["t1"] == WorkerStatus.FAILED

    @pytest.mark.anyio
    async def test_redispatch_after_completion(self, pool: WorkerPool):
        """A task can be re-dispatched once the previous run finishes."""
        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            new_callable=AsyncMock,
            return_value={"status": "completed"},
        ):
            handle = pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            await handle
            await asyncio.sleep(0)

            # Should not raise — previous task is done
            handle2 = pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            await handle2


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------

class TestCancel:
    @pytest.mark.anyio
    async def test_cancel_running_task(self, pool: WorkerPool):
        started = asyncio.Event()

        async def _hang(**kw):
            started.set()
            await asyncio.sleep(999)

        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            side_effect=_hang,
        ):
            pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            await started.wait()

            assert pool.cancel("t1") is True
            assert pool._statuses["t1"] == WorkerStatus.CANCELLED

    def test_cancel_unknown_task(self, pool: WorkerPool):
        assert pool.cancel("nonexistent") is False

    @pytest.mark.anyio
    async def test_cancel_already_done(self, pool: WorkerPool):
        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            new_callable=AsyncMock,
            return_value={"status": "completed"},
        ):
            handle = pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            await handle

        assert pool.cancel("t1") is False


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

class TestGetStatus:
    def test_unknown_task(self, pool: WorkerPool):
        info = pool.get_status("nope")
        assert info["status"] == "unknown"
        assert info["task_id"] == "nope"

    @pytest.mark.anyio
    async def test_running_task(self, pool: WorkerPool):
        started = asyncio.Event()
        finished = asyncio.Event()

        async def _slow(**kw):
            started.set()
            await finished.wait()

        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            side_effect=_slow,
        ):
            pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            await started.wait()

            info = pool.get_status("t1")
            assert info["status"] == "running"
            assert info["done"] is False
            finished.set()

    @pytest.mark.anyio
    async def test_completed_task(self, pool: WorkerPool):
        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            new_callable=AsyncMock,
            return_value={"status": "completed"},
        ):
            handle = pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            await handle
            await asyncio.sleep(0)

        info = pool.get_status("t1")
        assert info["status"] == "completed"
        assert info["done"] is True

    @pytest.mark.anyio
    async def test_failed_task_includes_error(self, pool: WorkerPool):
        with patch(
            "backend.services.sentinel.worker_pool.execute_task",
            new_callable=AsyncMock,
            side_effect=ValueError("bad input"),
        ):
            handle = pool.dispatch("t1", "sonnet", **_dummy_kwargs())
            with pytest.raises(ValueError):
                await handle
            await asyncio.sleep(0)

        info = pool.get_status("t1")
        assert info["status"] == "failed"
        assert "bad input" in info["error"]
