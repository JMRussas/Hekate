#  Orchestration Engine - Plan Sync Service
#
#  Synchronises orchestration plans and task status to the context store.
#  Creates context store nodes for each task in a plan and returns a mapping
#  of orchestration task IDs → context store node IDs.
#
#  Depends on: services/context_store_client.py, db/connection.py
#  Used by:    routes/projects.py (after decomposition), services/task_lifecycle.py

import json
import logging
import time

from backend.services.context_store_client import ContextStoreClient

logger = logging.getLogger("orchestration.plan_sync")

# Context store node types
_PLAN_NODE_TYPE = "orchestration_plan"
_TASK_NODE_TYPE = "orchestration_task"

# Shared client — lazy-initialized
_cs_client: ContextStoreClient | None = None


def _get_client() -> ContextStoreClient:
    global _cs_client
    if _cs_client is None:
        _cs_client = ContextStoreClient()
    return _cs_client


class PlanSyncService:
    """Sync orchestration plans to the context store as node trees.

    sync_plan() creates:
      1. A parent plan node under the project
      2. A child task node for each task in the plan
    Returns a dict mapping {task_id: context_store_node_id} and
    persists the mapping to the plans.node_mapping column.
    """

    def __init__(self, *, db, cs_client: ContextStoreClient | None = None):
        self._db = db
        self._cs = cs_client or _get_client()

    async def sync_plan(self, project_id: str, plan_id: str) -> dict[str, str]:
        """Create context store nodes for a plan and its tasks.

        Args:
            project_id: The project this plan belongs to.
            plan_id: The plan to sync.

        Returns:
            Mapping of orchestration task_id → context store node_id.
            Empty dict if context store is unavailable.
        """
        db = self._db

        # Load plan
        plan_row = await db.fetchone(
            "SELECT plan_json, version FROM plans WHERE id = ?", (plan_id,)
        )
        if not plan_row:
            logger.warning("Plan %s not found — skipping sync", plan_id)
            return {}

        plan_data = json.loads(plan_row["plan_json"])

        # Load tasks for this plan
        task_rows = await db.fetchall(
            "SELECT id, title, task_type, status, wave, phase "
            "FROM tasks WHERE plan_id = ? ORDER BY priority",
            (plan_id,),
        )
        if not task_rows:
            logger.info("Plan %s has no tasks — skipping sync", plan_id)
            return {}

        # Create parent plan node under the project
        plan_node_id = await self._cs.create_node(project_id, {
            "id": f"plan-{plan_id}",
            "type": _PLAN_NODE_TYPE,
            "label": f"Plan v{plan_row['version']}: {plan_data.get('summary', '')[:100]}",
            "attributes": {
                "plan_id": plan_id,
                "project_id": project_id,
                "version": plan_row["version"],
                "task_count": len(task_rows),
                "synced_at": time.time(),
            },
        })

        if plan_node_id is None:
            logger.info("Context store unavailable — skipping plan sync for %s", plan_id)
            return {}

        # Create a child node for each task
        mapping: dict[str, str] = {}
        for task in task_rows:
            task_id = task["id"]
            task_node_id_candidate = f"task-{task_id}"

            node_id = await self._cs.create_node(plan_node_id, {
                "id": task_node_id_candidate,
                "type": _TASK_NODE_TYPE,
                "label": task["title"],
                "attributes": {
                    "task_id": task_id,
                    "plan_id": plan_id,
                    "project_id": project_id,
                    "task_type": task["task_type"],
                    "status": task["status"],
                    "wave": task["wave"],
                    "phase": task["phase"] or "",
                    "synced_at": time.time(),
                },
            })

            if node_id is not None:
                mapping[task_id] = node_id

        # Persist the mapping to the plans table
        if mapping:
            await db.execute_write(
                "UPDATE plans SET node_mapping = ? WHERE id = ?",
                (json.dumps(mapping), plan_id),
            )
            logger.info(
                "Plan %s synced to context store: %d/%d task nodes created",
                plan_id, len(mapping), len(task_rows),
            )

        return mapping

    async def get_node_mapping(self, plan_id: str) -> dict[str, str]:
        """Load the persisted node mapping for a plan.

        Returns:
            Mapping of orchestration task_id → context store node_id.
            Empty dict if no mapping exists.
        """
        row = await self._db.fetchone(
            "SELECT node_mapping FROM plans WHERE id = ?", (plan_id,)
        )
        if not row or not row["node_mapping"]:
            return {}
        try:
            return json.loads(row["node_mapping"])
        except (json.JSONDecodeError, TypeError):
            return {}
