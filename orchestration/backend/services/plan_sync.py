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
#  Returns a dict mapping task titles → context store node IDs so that
#  task_lifecycle.py can push status updates back to the context store.
#
#  Depends on: services/context_store_client.py, db/connection.py
#  Used by:    routes/projects.py (after plan generation)

import json
import logging

from backend.db.connection import Database
from backend.services.context_store_client import ContextStoreClient

logger = logging.getLogger("orchestration.plan_sync")


class PlanSyncService:
    """Syncs orchestration plans to the context store as node trees."""

    def __init__(self, context_client: ContextStoreClient, db: Database):
        self._ctx = context_client
        self._db = db

    async def sync_plan(
        self,
        project_name: str,
        repo_path: str | None,
        plan_data: dict,
        plan_id: str,
        project_id: str,
    ) -> dict[str, str]:
        """Write a plan to the context store as a node tree.

        Returns a dict mapping task titles to context store node IDs.
        Empty dict if sync failed. Fire-and-forget safe — never raises, logs errors.
        """
        try:
            return await self._sync(project_name, repo_path, plan_data, plan_id, project_id)
        except Exception as exc:
            logger.warning("Plan sync to context store failed: %s", exc)
            return {}

    async def _sync(
        self,
        project_name: str,
        repo_path: str | None,
        plan_data: dict,
        plan_id: str,
        project_id: str,
    ) -> dict[str, str]:
        # Ensure the project exists in context store
        cs_project_id = await self._ctx.ensure_project(project_name, repo_path)
        if not cs_project_id:
            logger.debug("Could not ensure project in context store, skipping plan sync")
            return {}

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
            return {}

        # Collect task title → context store node ID mapping
        # __plan_root__ is a reserved key for the plan root node (used by sync_revision)
        node_mapping: dict[str, str] = {"__plan_root__": plan_node_id}

        # Sync phases and tasks
        phases = plan_data.get("phases")
        if phases and isinstance(phases, list):
            await self._sync_phases(plan_node_id, phases, node_mapping)
        else:
            # Flat plan (L1) — tasks directly under plan
            tasks = plan_data.get("tasks", [])
            for task in tasks:
                await self._sync_task(plan_node_id, task, node_mapping)

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

        # Persist the mapping to the plans table
        if node_mapping:
            await self._db.execute_write(
                "UPDATE plans SET node_mapping = $1 WHERE id = $2",
                (json.dumps(node_mapping), plan_id),
            )

        logger.info(
            "Plan synced to context store: plan_node=%s, project=%s, tasks_mapped=%d",
            plan_node_id, cs_project_id, len(node_mapping),
        )
        return node_mapping

    async def _sync_phases(
        self, plan_node_id: str, phases: list, node_mapping: dict[str, str],
    ) -> None:
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
                await self._sync_task(phase_node_id, task, node_mapping)

    async def _sync_task(
        self, parent_id: str, task: dict, node_mapping: dict[str, str],
    ) -> None:
        """Create a task node under a parent (phase or plan).

        On success, adds title → node_id to node_mapping.
        """
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

        # Preserve rationale (why this approach, alternatives, constraints)
        rationale = task.get("rationale")
        if rationale:
            attrs["rationale"] = rationale

        # Preserve requirement traceability
        req_ids = task.get("requirement_ids", [])
        if req_ids:
            attrs["requirement_ids"] = ", ".join(req_ids)

        node_id = await self._ctx.create_node(parent_id, {
            "nodeType": "task",
            "name": title,
            "value": description,
            "attributes": attrs,
        })
        if node_id:
            node_mapping[title] = node_id

    async def sync_revision(
        self,
        plan_id: str,
        *,
        wave_number: int,
        outcome: str,
        rationale: str,
        delta: dict,
        observation_ids: list[str] | None = None,
        finding_ids: list[str] | None = None,
    ) -> str | None:
        """Create a revision node under the plan and link it to triggering observations/findings.

        Observations get CONSTRAINS edges (they constrain what the plan can do).
        Findings get INFORMS edges (they inform why the plan changed).

        Returns the revision node ID, or None if sync failed.
        """
        try:
            return await self._sync_revision(
                plan_id,
                wave_number=wave_number,
                outcome=outcome,
                rationale=rationale,
                delta=delta,
                observation_ids=observation_ids,
                finding_ids=finding_ids,
            )
        except Exception as exc:
            logger.warning("Revision sync to context store failed: %s", exc)
            return None

    async def _sync_revision(
        self,
        plan_id: str,
        *,
        wave_number: int,
        outcome: str,
        rationale: str,
        delta: dict,
        observation_ids: list[str] | None = None,
        finding_ids: list[str] | None = None,
    ) -> str | None:
        # Look up the plan's root node ID from the node mapping
        row = await self._db.fetchone(
            "SELECT node_mapping FROM plans WHERE id = $1", (plan_id,),
        )
        if not row or not row["node_mapping"]:
            logger.debug("No node mapping for plan %s, cannot sync revision", plan_id)
            return None

        mapping = json.loads(row["node_mapping"])

        # The plan root node ID is stored under the special "__plan_root__" key
        # If not present, look it up from the plan table
        plan_node_id = mapping.get("__plan_root__")
        if not plan_node_id:
            plan_row = await self._db.fetchone(
                "SELECT plan_json FROM plans WHERE id = $1", (plan_id,),
            )
            if not plan_row:
                return None
            # Fall back to using create_revision_node on the context client directly
            # which needs a plan node ID — we can't proceed without one
            logger.debug("No __plan_root__ in node mapping for plan %s", plan_id)
            return None

        # Create the revision node
        revision_id = await self._ctx.create_node(plan_node_id, {
            "nodeType": "revision",
            "name": f"Wave {wave_number} reassessment — {outcome}",
            "value": rationale,
            "attributes": {
                "wave_number": str(wave_number),
                "outcome": outcome,
                "rationale": rationale,
                "delta": json.dumps(delta) if isinstance(delta, dict) else str(delta),
            },
        })
        if not revision_id:
            return None

        # Link revision → observations with CONSTRAINS edges
        for obs_id in (observation_ids or []):
            await self._ctx.create_edge({
                "sourceId": obs_id,
                "targetId": revision_id,
                "type": "CONSTRAINS",
            })

        # Link findings → revision with INFORMS edges
        for finding_id in (finding_ids or []):
            await self._ctx.create_edge({
                "sourceId": finding_id,
                "targetId": revision_id,
                "type": "INFORMS",
            })

        logger.info(
            "Revision synced: revision=%s, plan=%s, wave=%d, observations=%d, findings=%d",
            revision_id, plan_node_id, wave_number,
            len(observation_ids or []), len(finding_ids or []),
        )
        return revision_id

    async def get_node_mapping(self, plan_id: str) -> dict[str, str]:
        """Load the persisted task title → node ID mapping for a plan."""
        row = await self._db.fetchone(
            "SELECT node_mapping FROM plans WHERE id = $1", (plan_id,),
        )
        if not row or not row["node_mapping"]:
            return {}
        return json.loads(row["node_mapping"])
