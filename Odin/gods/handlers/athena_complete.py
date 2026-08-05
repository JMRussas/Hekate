"""Athena completion handlers.

Two handlers:
  athena_bubble_up   — handles plan_node_complete, propagates completion up the tree
  athena_materialize — handles plan_node_executable, creates task rows from L5 nodes

Completion propagation:
  When a node completes, check if all its siblings are complete.
  If yes, mark the parent complete and emit plan_node_complete for the parent.
  When all L0 (epic) nodes are complete, emit project_planned.

Materialization:
  L5 nodes contain executable specs. Each L5 node becomes one task row
  in the tasks table, ready for Odin to dispatch.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from gods.pipeline import Event, Emit

logger = logging.getLogger("gods.handlers.athena_complete")


# ---------------------------------------------------------------------------
# athena_bubble_up
# ---------------------------------------------------------------------------

async def athena_bubble_up(event: Event, db) -> list[Emit]:
    """Handle plan_node_complete — propagate completion up the node tree.

    When all siblings of a node are complete, mark the parent complete
    and emit plan_node_complete for the parent. When all L0 nodes are
    complete, emit project_planned.
    """
    payload = event.payload
    project_id = payload.get("project_id")
    node_id = payload.get("node_id")
    index_path = payload.get("index_path")
    level = payload.get("level", 0)
    parent_index = payload.get("parent_index")

    if not project_id or not index_path:
        logger.error("athena_bubble_up: missing project_id or index_path")
        return []

    # Mark this node complete
    now = time.time()
    await db.execute_write(
        "UPDATE plan_nodes SET status = $1, updated_at = $2 WHERE id = $3",
        ("complete", now, node_id),
    )

    if parent_index is None:
        # This is a root (L0) node — check if all L0 nodes for this project are complete
        incomplete = await db.fetchone(
            "SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND level = 0 AND status != 'complete'",
            (project_id,),
        )
        if incomplete and incomplete["cnt"] == 0:
            logger.info("athena_bubble_up: all epics complete for %s — project planned", project_id[:8])
            # Get plan_id from any node
            plan_row = await db.fetchone(
                "SELECT plan_id FROM plan_nodes WHERE project_id = $1 LIMIT 1",
                (project_id,),
            )
            plan_id = plan_row["plan_id"] if plan_row else None

            # Update project status
            await db.execute_write(
                "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
                ("planned", now, project_id),
            )
            if plan_id:
                await db.execute_write(
                    "UPDATE plans SET status = $1 WHERE id = $2",
                    ("approved", plan_id),
                )

            return [Emit("project_planned", {
                "project_id": project_id,
                "plan_id": plan_id,
                "level": "L3",
                "source": "node_tree",
            }, source="athena_complete")]
        else:
            remaining = incomplete["cnt"] if incomplete else "?"
            logger.info(
                "athena_bubble_up: epic %s complete for %s (%s epics remaining)",
                index_path, project_id[:8], remaining,
            )
            return []

    # Non-root node — check if all siblings are complete
    incomplete_siblings = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND parent_index = $2 AND status NOT IN ('complete', 'failed')",
        (project_id, parent_index),
    )

    if not incomplete_siblings or incomplete_siblings["cnt"] > 0:
        logger.info(
            "athena_bubble_up: node %s complete for %s (%s siblings still pending)",
            index_path, project_id[:8],
            incomplete_siblings["cnt"] if incomplete_siblings else "?",
        )
        return []

    # All siblings complete — mark parent complete and bubble up
    parent_row = await db.fetchone(
        "SELECT id, index_path, level, parent_index FROM plan_nodes\n        WHERE project_id = $1 AND index_path = $2",
        (project_id, parent_index),
    )
    if not parent_row:
        logger.warning("athena_bubble_up: parent node %s not found for project %s", parent_index, project_id[:8])
        return []

    logger.info(
        "athena_bubble_up: all children of %s complete — bubbling up for %s",
        parent_index, project_id[:8],
    )

    return [Emit("plan_node_complete", {
        "project_id": project_id,
        "node_id": parent_row["id"],
        "index_path": parent_row["index_path"],
        "level": parent_row["level"],
        "parent_index": parent_row["parent_index"],
    }, source="athena_complete")]


# ---------------------------------------------------------------------------
# athena_materialize
# ---------------------------------------------------------------------------

async def athena_materialize(event: Event, db) -> list[Emit]:
    """Handle plan_node_executable — create a task row from an L5 node.

    L5 nodes contain the full executable spec. Each becomes one task row
    in the tasks table. Dependencies are resolved by index_path references.
    """
    payload = event.payload
    project_id = payload.get("project_id")
    node_id = payload.get("node_id")
    index_path = payload.get("index_path")

    if not project_id or not node_id:
        logger.error("athena_materialize: missing project_id or node_id")
        return []

    node = await db.fetchone(
        "SELECT * FROM plan_nodes WHERE id = $1",
        (node_id,),
    )
    if not node:
        logger.error("athena_materialize: node %s not found", node_id)
        return []

    content = json.loads(node["content_json"] or "{}")
    now = time.time()

    # Idempotency: skip if a task already exists for this plan node
    existing = await db.fetchone(
        "SELECT id FROM tasks WHERE project_id = $1 AND context_json LIKE $2",
        (project_id, f'%"plan_node_id": "{node_id}"%'),
    )
    if existing:
        logger.info(
            "athena_materialize: task already exists for node %s, skipping",
            node_id,
        )
        return []

    task_id = uuid.uuid4().hex[:12]

    # Determine wave from level depth (number of dots in index_path = nesting depth)
    # Wave 0 = root-level tasks, wave N = tasks nested N levels deep
    wave = index_path.count(".")

    # Resolve depends_on: content may have depends_on_paths as index paths
    depends_on_paths = content.get("depends_on_paths", [])
    dep_task_ids: list[str] = []
    for dep_path in depends_on_paths:
        dep_node = await db.fetchone(
            "SELECT id FROM plan_nodes WHERE project_id = $1 AND index_path = $2",
            (project_id, dep_path),
        )
        if dep_node:
            # Find the task row for this node
            dep_task = await db.fetchone(
                "SELECT id FROM tasks WHERE project_id = $1 AND context_json LIKE $2",
                (project_id, f'%"plan_node_id": "{dep_node["id"]}"%'),
            )
            if dep_task:
                dep_task_ids.append(dep_task["id"])

    # Build context for the executor
    context = {
        "plan_node_id": node_id,
        "index_path": index_path,
        "changes": content.get("changes", []),
        "implementation_notes": content.get("implementation_notes", ""),
        "test_strategy": content.get("test_strategy", ""),
    }

    # Get plan_id
    plan_id = node["plan_id"]

    await db.execute_write(
        "INSERT OR IGNORE INTO tasks "
        "(id, project_id, plan_id, title, description, task_type, priority, status, "
        "model_tier, context_json, created_at, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
        (
            task_id, project_id, plan_id,
            content.get("title", node["title"] or f"Task {index_path}"),
            content.get("description", ""),
            content.get("task_type", "code"),
            wave,  # priority = wave number
            "pending",
            "claude_code",
            json.dumps(context),
            now, now,
        ),
    )

    # Write task dependencies
    for dep_id in dep_task_ids:
        await db.execute_write(
            "INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES ($1, $2)",
            (task_id, dep_id),
        )

    # Mark node complete
    await db.execute_write(
        "UPDATE plan_nodes SET status = $1, updated_at = $2 WHERE id = $3",
        ("complete", now, node_id),
    )

    logger.info(
        "athena_materialize: created task %s from node %s for project %s",
        task_id[:8], index_path, project_id[:8],
    )

    # Emit plan_node_complete to trigger bubble-up
    return [Emit("plan_node_complete", {
        "project_id": project_id,
        "node_id": node_id,
        "index_path": index_path,
        "level": node["level"],
        "parent_index": node["parent_index"],
    }, source="athena_materialize")]
