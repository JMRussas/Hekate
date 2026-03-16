#  Worker Pool
#
#  Abstraction layer over task dispatch.  Wraps execute_task from
#  task_lifecycle.py so that sentinel-as-orchestrator can dispatch,
#  cancel, and inspect tasks through a single surface.
#
#  Phase 0: skeleton — does NOT replace the executor.

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

from backend.services.task_lifecycle import execute_task

log = logging.getLogger(__name__)


class WorkerStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkerPool:
    """Thin wrapper around execute_task for sentinel-driven dispatch.

    Parameters
    ----------
    db : Database
        Async SQLite connection.
    semaphore : asyncio.Semaphore
        Global concurrency gate.
    progress_manager : ProgressManager
        SSE event broadcaster.
    """

    def __init__(self, db, semaphore: asyncio.Semaphore, progress_manager):
        self._db = db
        self._semaphore = semaphore
        self._progress = progress_manager

        # task_id → asyncio.Task handle
        self._tasks: dict[str, asyncio.Task] = {}
        # task_id → WorkerStatus
        self._statuses: dict[str, WorkerStatus] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def available_slots(self) -> int:
        """Number of free semaphore slots (approximate)."""
        # Semaphore._value is not part of the public API but is the
        # standard CPython way to inspect remaining capacity.
        return self._semaphore._value  # type: ignore[attr-defined]

    def dispatch(self, task_id: str, model_tier: str, **kwargs: Any) -> asyncio.Task:
        """Dispatch a task for execution.

        Required keyword arguments are forwarded directly to
        ``execute_task`` (e.g. ``task_row``, ``budget``, ``client``,
        ``tool_registry``, ``http_client``, ``dispatched``,
        ``retry_after``).  ``db``, ``semaphore``, and ``progress``
        are injected automatically from the pool's own references.

        Returns the ``asyncio.Task`` wrapping execution.
        """
        if task_id in self._tasks and not self._tasks[task_id].done():
            raise RuntimeError(f"Task {task_id} is already dispatched")

        handle = asyncio.create_task(
            self._run(task_id, model_tier, **kwargs),
            name=f"worker-pool-{task_id}",
        )
        self._tasks[task_id] = handle
        self._statuses[task_id] = WorkerStatus.PENDING
        handle.add_done_callback(lambda _t: self._on_done(task_id, _t))
        return handle

    def cancel(self, task_id: str) -> bool:
        """Cancel a running task.  Returns True if cancellation was requested."""
        handle = self._tasks.get(task_id)
        if handle is None or handle.done():
            return False
        handle.cancel()
        self._statuses[task_id] = WorkerStatus.CANCELLED
        log.info("worker_pool: cancelled task %s", task_id)
        return True

    def get_status(self, task_id: str) -> dict[str, Any]:
        """Return status info for a tracked task."""
        status = self._statuses.get(task_id)
        if status is None:
            return {"task_id": task_id, "status": "unknown"}

        handle = self._tasks.get(task_id)
        result: dict[str, Any] = {
            "task_id": task_id,
            "status": status.value,
            "done": handle.done() if handle else True,
        }

        if handle and handle.done() and not handle.cancelled():
            exc = handle.exception() if not handle.cancelled() else None
            if exc:
                result["error"] = str(exc)

        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(self, task_id: str, model_tier: str, **kwargs: Any):
        """Execute a task through the existing lifecycle function."""
        self._statuses[task_id] = WorkerStatus.RUNNING
        log.info("worker_pool: dispatching task %s (tier=%s)", task_id, model_tier)

        return await execute_task(
            db=self._db,
            semaphore=self._semaphore,
            progress=self._progress,
            **kwargs,
        )

    def _on_done(self, task_id: str, handle: asyncio.Task) -> None:
        """Callback fired when a task completes or fails."""
        if handle.cancelled():
            self._statuses[task_id] = WorkerStatus.CANCELLED
        elif handle.exception():
            self._statuses[task_id] = WorkerStatus.FAILED
            log.warning(
                "worker_pool: task %s failed: %s",
                task_id,
                handle.exception(),
            )
        else:
            self._statuses[task_id] = WorkerStatus.COMPLETED
