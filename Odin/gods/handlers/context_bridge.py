"""Context Bridge — syncs pipeline state to the context store.

Listens to pipeline events and creates/updates nodes in the context store
so that plans, tasks, and their outputs are visible in the graph DB,
searchable via semantic search, and navigable in the context store UI.

Event handlers:
  - project_planned → create plan + task nodes in context store
  - task_verified   → update task node with output + verification status
  - project_complete → mark plan node as completed

Uses context store HTTP API (port 5102), not direct DB access.
"""

import json
import logging
import time

import httpx

from gods.pipeline import Event, Emit

logger = logging.getLogger("gods.handlers.context_bridge")

_CS_URL = "http://localhost:5102"
_TIMEOUT = 10.0

_PIPELINE_PROJECT_NAME = "Hekate Pipeline"
_cs_project_id: str | None = None


async def _ensure_project() -> str | None:
    """Get or create the pipeline project in context store. Returns UUID string."""
    global _cs_project_id
    if _cs_project_id:
        return _cs_project_id

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # Find by name
            resp = await client.get(f"{_CS_URL}/api/projects", params={"name": _PIPELINE_PROJECT_NAME})
            if resp.status_code == 200:
                data = resp.json()
                _cs_project_id = str(data["id"])
                return _cs_project_id

            # Create
            resp = await client.post(f"{_CS_URL}/api/projects", json={
                "name": _PIPELINE_PROJECT_NAME,
                "rootPath": "/pipeline",
            })
            if resp.status_code == 200:
                _cs_project_id = str(resp.json()["id"])
                return _cs_project_id
    except Exception as e:
        logger.debug("Context bridge: cannot reach context store: %s", e)
    return None


async def _create_root_node(project_id: str, node_type: str, name: str,
                            value: str | None = None,
                            attributes: dict | None = None) -> str | None:
    """Create a root-level node in the context store. Returns node ID."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            body: dict = {"nodeType": node_type, "name": name}
            if value:
                body["value"] = value[:10000]
            if attributes:
                body["attributes"] = attributes
            resp = await client.post(f"{_CS_URL}/api/project/{project_id}/nodes", json=body)
            if resp.status_code == 200:
                return str(resp.json()["id"])
    except Exception as e:
        logger.debug("Context bridge: create_root_node error: %s", e)
    return None


async def _create_child_node(parent_id: str, node_type: str, name: str,
                             value: str | None = None,
                             attributes: dict | None = None) -> str | None:
    """Create a child node. Returns node ID."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            body: dict = {"nodeType": node_type, "name": name}
            if value:
                body["value"] = value[:10000]
            if attributes:
                body["attributes"] = attributes
            resp = await client.post(f"{_CS_URL}/api/node/{parent_id}/children", json=body)
            if resp.status_code == 200:
                data = resp.json()
                return str(data.get("id") or data.get("record", {}).get("id", ""))
    except Exception as e:
        logger.debug("Context bridge: create_child_node error: %s", e)
    return None


async def _update_node(node_id: str, name: str | None = None, value: str | None = None):
    """Update a node's name/value."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await client.put(f"{_CS_URL}/api/node/{node_id}", json={
                "name": name, "value": (value or "")[:10000],
            })
    except Exception:
        pass


async def _update_attrs(node_id: str, attributes: dict):
    """Update a node's attributes."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            await client.put(f"{_CS_URL}/api/node/{node_id}/attributes", json={
                "attributes": attributes,
            })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# project_planned → create plan + task nodes
# ---------------------------------------------------------------------------

async def context_bridge_plan(event: Event, db) -> list[Emit] | None:
    """Create a plan node tree in the context store when planning completes."""
    project_id = event.payload.get("project_id")
    plan_id = event.payload.get("plan_id")
    if not project_id:
        return None

    cs_project = await _ensure_project()
    if not cs_project:
        return None

    project = await db.fetchone(
        "SELECT name, requirements FROM projects WHERE id = $1", (project_id,))
    if not project:
        return None

    plan_node_id = await _create_root_node(
        cs_project, "plan", project["name"],
        value=project.get("requirements", ""),
        attributes={
            "engine_project_id": project_id,
            "engine_plan_id": plan_id or "",
            "plan_type": "pipeline",
            "status": "planned",
            "level": event.payload.get("level", "L1"),
        },
    )
    if not plan_node_id:
        return None

    logger.info("Context bridge: plan node %s for project %s", plan_node_id[:8], project_id[:8])

    tasks = await db.fetchall(
        "SELECT id, title, description, task_type, wave, status, model_tier "
        "FROM tasks WHERE project_id = $1 ORDER BY wave, id",
        (project_id,),
    )

    count = 0
    for task in tasks:
        nid = await _create_child_node(
            plan_node_id, "task", task["title"],
            value=task.get("description", ""),
            attributes={
                "engine_task_id": task["id"],
                "task_type": task.get("task_type", "code"),
                "wave": str(task.get("wave", 0)),
                "status": task.get("status", "pending"),
                "model_tier": task.get("model_tier", "claude_code"),
            },
        )
        if nid:
            count += 1

    logger.info("Context bridge: %d task nodes under plan %s", count, plan_node_id[:8])
    return None


# ---------------------------------------------------------------------------
# task_verified → update task node with output
# ---------------------------------------------------------------------------

async def context_bridge_task_verified(event: Event, db) -> list[Emit] | None:
    """Update the task node in context store with output after verification."""
    task_id = event.payload.get("task_id")
    if not task_id:
        return None

    cs_project = await _ensure_project()
    if not cs_project:
        return None

    task = await db.fetchone(
        "SELECT title, output_text, verification_status, verification_notes, cost_usd "
        "FROM tasks WHERE id = $1",
        (task_id,),
    )
    if not task or not task.get("output_text"):
        return None

    # Search context store for the task node by querying nodes
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # Get plan nodes for the project
            resp = await client.get(f"{_CS_URL}/api/plans")
            if resp.status_code != 200:
                return None

            for plan in resp.json():
                # Get plan tree and find our task
                tree_resp = await client.get(f"{_CS_URL}/api/plan/{plan['id']}")
                if tree_resp.status_code != 200:
                    continue
                tree = tree_resp.json()
                # Search children for matching engine_task_id
                for child in tree.get("children", []):
                    attrs = child.get("attributes", {})
                    if attrs.get("engine_task_id") == task_id:
                        node_id = str(child["id"])
                        await _update_node(node_id, value=task["output_text"])
                        await _update_attrs(node_id, {
                            "status": "completed",
                            "verification_status": task.get("verification_status", ""),
                            "cost_usd": str(task.get("cost_usd", 0)),
                        })
                        logger.info("Context bridge: updated task node %s with output", node_id[:8])
                        return None
    except Exception as e:
        logger.debug("Context bridge: task_verified error: %s", e)

    return None


# ---------------------------------------------------------------------------
# project_complete → mark plan node completed
# ---------------------------------------------------------------------------

async def context_bridge_project_complete(event: Event, db) -> list[Emit] | None:
    """Mark the plan node as completed when project finishes."""
    project_id = event.payload.get("project_id")
    if not project_id:
        return None

    cs_project = await _ensure_project()
    if not cs_project:
        return None

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{_CS_URL}/api/plans")
            if resp.status_code != 200:
                return None

            for plan in resp.json():
                tree_resp = await client.get(f"{_CS_URL}/api/plan/{plan['id']}")
                if tree_resp.status_code != 200:
                    continue
                tree = tree_resp.json()
                attrs = tree.get("attributes", {})
                if attrs.get("engine_project_id") == project_id:
                    node_id = str(tree["id"])
                    await _update_attrs(node_id, {
                        "status": "completed",
                        "completed_at": str(time.time()),
                    })
                    logger.info("Context bridge: plan %s marked completed", node_id[:8])
                    return None
    except Exception as e:
        logger.debug("Context bridge: project_complete error: %s", e)

    return None
