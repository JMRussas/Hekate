#  Plan Sentinel
#
#  Per-plan async monitor that tracks execution health for a single project.
#  Spawned and torn down by SystemSentinel.
#
#  Depends on: sentinel/bus.py, sentinel/models.py
#  Used by:    system_sentinel.py

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.sentinel.bus import SentinelBus

logger = logging.getLogger(__name__)


class PlanSentinel:
    """Monitors execution health for a single project/plan.

    Lifecycle managed by SystemSentinel — do not instantiate directly.
    """

    def __init__(self, project_id: str, bus: SentinelBus) -> None:
        self._project_id = project_id
        self._bus = bus
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the plan sentinel. Idempotent."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(), name=f"plan_sentinel:{self._project_id}",
        )
        logger.info("Plan Sentinel started for project %s", self._project_id)

    async def stop(self) -> None:
        """Stop the plan sentinel and clean up."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Plan Sentinel stopped for project %s", self._project_id)

    async def _run_loop(self) -> None:
        """Placeholder tick loop for future per-plan monitoring."""
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Plan Sentinel tick error (project %s)", self._project_id,
                )
            await asyncio.sleep(10.0)

    async def _tick(self) -> None:
        """Single plan sentinel tick — placeholder for future phases."""
