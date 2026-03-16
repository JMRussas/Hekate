#  Orchestration Engine - Plan Sync Service
#
#  Syncs orchestration plans to the context store as plan node trees.
#  Plans become queryable across all conversations via semantic search.
#
#  Mapping:
#    plan_data (JSON)  →  context store nodes
#    ─────────────────────────────────────────
#    root              →  plan (root node)
#    phases[]          →  plan_phase (children of plan)
#    tasks[]           →  task (children of phase or plan)
#    open_questions[]  →  question (children of plan)
#    risk_assessment[] →  risk (children of plan)
#
#  Depends on: services/context_store_client.py
#  Used by:    routes/projects.py (after plan generation)

import logging

from backend.services.context_store_client import ContextStoreClient

logger = logging.getLogger("orchestration.plan_sync")


class PlanSyncService:
    """Syncs orchestration plans to the context store as node trees."""

    def __init__(self, context_client: ContextStoreClient):
        self._ctx = context_client

    async def sync_plan(
        self,
        project_name: str,
        repo_path: str | None,
        plan_data: dict,
        plan_id: str,
        project_id: str,
    ) -> str | None:
        """Write a plan to the context store as a node tree.

        Returns the context store plan node ID, or None if sync failed.
        Fire-and-forget safe — never raises, logs errors.
        """
        try:
            return await self._sync(project_name, repo_path, plan_data, plan_id, project_id)
        except Exception as exc:
            logger.warning("Plan sync to context store failed: %s", exc)
            return None

    async def _sync(
        self,
        project_name: str,
        repo_path: str | None,
        plan_data: dict,
        plan_id: str,
        project_id: str,
    ) -> str | None:
        # Ensure the project exists in context store
        cs_project_id = await self._ctx.ensure_project(project_name, repo_path)
        if not cs_project_id:
            logger.debug("Could not ensure project in context store, skipping plan sync")
            return None

        summary = plan_data.get("summary", "")

        # Create root plan node
        plan_node_id = await self._ctx.create_root_node(cs_project_id, {
            "nodeType": "plan",
            "name": project_name,
            "value": summary,
            "attributes": {
                "plan_type": "orchestration",
                "status": "draft",
                "orch_plan_id": plan_id,
                "orch_project_id": project_id,
            },
        })
        if not plan_node_id:
            logger.debug("Could not create plan root node, skipping plan sync")
            return None

        # Sync phases and tasks
        phases = plan_data.get("phases")
        if phases and isinstance(phases, list):
            await self._sync_phases(plan_node_id, phases)
        else:
            # Flat plan (L1) — tasks directly under plan
            tasks = plan_data.get("tasks", [])
            for task in tasks:
                await self._sync_task(plan_node_id, task)

        # Sync epics (L0 roadmap)
        epics = plan_data.get("epics")
        if epics and isinstance(epics, list):
            for epic in epics:
                if not isinstance(epic, dict):
                    continue
                await self._ctx.create_node(plan_node_id, {
                    "nodeType": "plan_phase",
                    "name": epic.get("title", "Unnamed Epic"),
                    "value": epic.get("description", ""),
                    "attributes": {
                        "status": "pending",
                        "scope": epic.get("scope", ""),
                        "estimated_complexity": epic.get("estimated_complexity", ""),
                        "success_criteria": epic.get("success_criteria", ""),
                    },
                })

        # Sync open questions
        questions = plan_data.get("open_questions", [])
        for q in questions:
            if not isinstance(q, dict):
                continue
            await self._ctx.create_node(plan_node_id, {
                "nodeType": "question",
                "name": q.get("question", "")[:200],
                "value": q.get("question", ""),
                "attributes": {
                    "status": "open",
                    "proposed_answer": q.get("proposed_answer", ""),
                    "impact": q.get("impact", ""),
                },
            })

        # Sync risks (L3)
        risks = plan_data.get("risk_assessment", [])
        for r in risks:
            if not isinstance(r, dict):
                continue
            await self._ctx.create_node(plan_node_id, {
                "nodeType": "risk",
                "name": r.get("risk", "")[:200],
                "value": r.get("risk", ""),
                "attributes": {
                    "likelihood": r.get("likelihood", ""),
                    "impact": r.get("impact", ""),
                    "mitigation": r.get("mitigation", ""),
                },
            })

        logger.info(
            "Plan synced to context store: plan_node=%s, project=%s",
            plan_node_id, cs_project_id,
        )
        return plan_node_id

    async def _sync_phases(self, plan_node_id: str, phases: list) -> None:
        """Create phase nodes with their tasks."""
        for phase in phases:
            if not isinstance(phase, dict):
                continue

            phase_name = phase.get("name", "Unnamed Phase")
            phase_desc = phase.get("description", "")

            phase_node_id = await self._ctx.create_node(plan_node_id, {
                "nodeType": "plan_phase",
                "name": phase_name,
                "value": phase_desc,
                "attributes": {
                    "status": "pending",
                },
            })
            if not phase_node_id:
                continue

            for task in phase.get("tasks", []):
                await self._sync_task(phase_node_id, task)

    async def _sync_task(self, parent_id: str, task: dict) -> None:
        """Create a task node under a parent (phase or plan)."""
        if not isinstance(task, dict):
            return

        title = task.get("title", "Unnamed Task")
        description = task.get("description", "")
        task_type = task.get("task_type", "code")
        complexity = task.get("complexity", "medium")

        attrs = {
            "status": "pending",
            "task_type": task_type,
            "complexity": complexity,
        }

        # Preserve verification criteria
        criteria = task.get("verification_criteria")
        if criteria:
            attrs["verification_criteria"] = criteria

        # Preserve affected files
        files = task.get("affected_files", [])
        if files:
            attrs["affected_files"] = ", ".join(files)

        # Preserve requirement traceability
        req_ids = task.get("requirement_ids", [])
        if req_ids:
            attrs["requirement_ids"] = ", ".join(req_ids)

        await self._ctx.create_node(parent_id, {
            "nodeType": "task",
            "name": title,
            "value": description,
            "attributes": attrs,
        })

    async def update_task_status(
        self,
        orch_project_id: str,
        task_title: str,
        new_status: str,
    ) -> bool:
        """Update a task node's status in the context store.

        Searches for the task by title within the project's plan.
        Returns True if updated, False otherwise.
        """
        # This will be wired up when we have a mapping table between
        # orchestration task IDs and context store node IDs.
        # For now, status sync is manual / future work.
        return False
