#  Reasoner Context
#
#  Unified data source wrapper for the 5-Whys reasoner. Provides async
#  methods to query task history, past decisions, resource health, and
#  similar incidents — everything the reasoner needs to gather evidence
#  at each investigation step.
#
#  All methods degrade gracefully: they return empty results on failure
#  so the reasoning chain can continue with partial evidence.
#
#  Depends on: db/connection.py, sentinel/context_client.py,
#              resource_monitor.py, sentinel/models.py

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.db.connection import Database
    from backend.services.resource_monitor import ResourceMonitor
    from backend.services.sentinel.context_client import SentinelContextClient
    from backend.services.sentinel.models import SentinelObservation

logger = logging.getLogger(__name__)


class ReasonerContext:
    """Unified data source wrapper for the sentinel reasoner.

    Wraps the database, context client, and resource monitor behind
    a small set of query methods that the 5-Whys loop can call at
    each investigation step.  Every method returns a dict (or list)
    and never raises — callers always get *something* back.
    """

    def __init__(
        self,
        db: Database | None = None,
        context_client: SentinelContextClient | None = None,
        resource_monitor: ResourceMonitor | None = None,
    ) -> None:
        self._db = db
        self._context_client = context_client
        self._resource_monitor = resource_monitor

    # ------------------------------------------------------------------
    # Task history
    # ------------------------------------------------------------------

    async def query_task_history(
        self,
        project_id: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch task details and past failure info.

        Returns a dict with:
          - task: row dict for the specific task (if task_id given)
          - recent_failures: list of recent failed tasks in the project
        """
        result: dict[str, Any] = {"task": None, "recent_failures": []}

        if not self._db:
            return result

        try:
            # Fetch the specific task if requested
            if task_id:
                row = await self._db.fetchone(
                    "SELECT id, title, status, model_tier, model_used, "
                    "retry_count, error, output, wave, created_at, updated_at "
                    "FROM tasks WHERE id = ? AND project_id = ?",
                    (task_id, project_id),
                )
                if row:
                    result["task"] = dict(row)

            # Fetch recent failures in the project (last 20)
            rows = await self._db.fetchall(
                "SELECT id, title, status, error, retry_count, model_used, wave, updated_at "
                "FROM tasks "
                "WHERE project_id = ? AND status = 'failed' "
                "ORDER BY updated_at DESC LIMIT 20",
                (project_id,),
            )
            result["recent_failures"] = [dict(r) for r in rows]
        except Exception:
            logger.debug(
                "Failed to query task history for project=%s task=%s",
                project_id, task_id, exc_info=True,
            )

        return result

    # ------------------------------------------------------------------
    # Decision history
    # ------------------------------------------------------------------

    async def query_decision_history(
        self,
        project_id: str,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch past sentinel decisions for this project.

        Optionally filters by category (matched against the reasoning
        text or details_json).  Returns up to 20 most recent decisions.
        """
        if not self._db:
            return []

        try:
            if category:
                rows = await self._db.fetchall(
                    "SELECT id, command, reasoning, confidence, outcome, "
                    "details_json, timestamp "
                    "FROM sentinel_decisions "
                    "WHERE project_id = ? "
                    "AND (reasoning LIKE ? OR details_json LIKE ?) "
                    "ORDER BY timestamp DESC LIMIT 20",
                    (project_id, f"%{category}%", f"%{category}%"),
                )
            else:
                rows = await self._db.fetchall(
                    "SELECT id, command, reasoning, confidence, outcome, "
                    "details_json, timestamp "
                    "FROM sentinel_decisions "
                    "WHERE project_id = ? "
                    "ORDER BY timestamp DESC LIMIT 20",
                    (project_id,),
                )
            return [dict(r) for r in rows]
        except Exception:
            logger.debug(
                "Failed to query decision history for project=%s category=%s",
                project_id, category, exc_info=True,
            )
            return []

    # ------------------------------------------------------------------
    # Resource health
    # ------------------------------------------------------------------

    async def query_resource_health(self) -> list[dict[str, Any]]:
        """Return current health state for all monitored resources.

        Uses the resource monitor's cached states (no I/O) so this
        is effectively free to call.
        """
        if not self._resource_monitor:
            return []

        try:
            states = self._resource_monitor.get_all()
            return [
                {
                    "id": s.id,
                    "name": s.name,
                    "status": s.status.value,
                    "category": s.category,
                    "response_time_ms": s.response_time_ms,
                }
                for s in states
            ]
        except Exception:
            logger.debug("Failed to query resource health", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Similar incidents (semantic search)
    # ------------------------------------------------------------------

    async def query_similar_incidents(
        self,
        observation: SentinelObservation,
        max_results: int = 5,
    ) -> list[dict[str, Any]]:
        """Semantic search for past observations similar to the given one.

        Delegates to SentinelContextClient.search_similar_observations()
        which uses pgvector similarity under the hood.
        """
        if not self._context_client:
            return []

        try:
            return await self._context_client.search_similar_observations(
                observation, max_results=max_results,
            )
        except Exception:
            logger.debug(
                "Failed to search similar incidents for %s",
                observation.observation_id, exc_info=True,
            )
            return []
