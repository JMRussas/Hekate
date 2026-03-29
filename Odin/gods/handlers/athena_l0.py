"""Athena L0 handler — epic generation.

Handles project_created events for projects using the node tree planner.
Generates L0 epic stubs, runs a gap check in the same conversation,
then emits plan_node_created for each epic.

Flow:
  project_created
    → generate epics (LLM turn 1)
    → gap check (LLM turn 2, same conversation)
    → write plan_nodes rows (level=0, status=stub)
    → emit plan_node_created per epic
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from gods.pipeline import Event, Emit
from gods import safe_json
from gods.handlers.athena_leveled import _call_gateway  # noqa: E402
from gods.providers.response_validator import extract_json  # noqa: E402

logger = logging.getLogger("gods.handlers.athena_l0")

GATEWAY_URL = "http://localhost:5210"

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_EPIC_SYSTEM = """\
You are a senior software architect. Your job is to decompose a project into epics.

An epic is a high-level theme of work (e.g. "Observability", "Reliability", "Security").
Epics should be:
- Independently deliverable
- Meaningful enough to warrant multiple tasks
- Named concisely (2-4 words)

Output ONLY a JSON array of epics, no prose:
[
  {"title": "...", "description": "...", "rationale": "..."},
  ...
]
"""

_GAP_CHECK = """\
You just generated the above epics. Before we proceed:
1. Are there any important epics you missed?
2. Are any epics redundant and should be merged?
3. Are any epics too broad and should be split?

If you have additions or changes, output a JSON array of new/replacement epics.
If the list is complete, output an empty array: []

Output ONLY a JSON array, no prose.
"""

# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def athena_l0(event: Event, db) -> list[Emit]:
    """Handle project_created → generate L0 epic stubs → emit plan_node_created per epic.

    Only handles projects with use_node_tree_planner=True in config_json.
    """
    project_id = event.payload.get("project_id")
    if not project_id:
        logger.error("athena_l0: missing project_id in payload")
        return []

    # Load project
    row = await db.fetchone(
        "SELECT id, name, requirements, status, config_json FROM projects WHERE id = $1",
        (project_id,),
    )
    if not row:
        logger.error("athena_l0: project %s not found", project_id)
        return []

    # Check feature flag
    config = json.loads(row["config_json"] or "{}")
    if not config.get("use_node_tree_planner", False):
        return []  # Let old athena_plan_leveled handle it

    status = row["status"]
    if status not in ("draft", "failed"):
        logger.info("athena_l0: project %s already in status %s, skipping", project_id, status)
        return []

    project_name = row["name"]
    requirements = row["requirements"] or ""
    project_context = f"Project: {project_name}\n\nRequirements:\n{requirements}"

    # Update status → planning
    now = time.time()
    await db.execute_write(
        "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
        ("planning", now, project_id),
    )

    # Create a plan row to anchor the node tree
    plan_id = uuid.uuid4().hex[:12]
    await db.execute_write(
        "INSERT OR IGNORE INTO plans (id, project_id, plan_json, level, created_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        (plan_id, project_id, "{}", "L0", now),
    )

    logger.info("athena_l0: generating epics for project %s (%s)", project_id[:8], project_name)

    try:
        # Turn 1: generate epics
        user_msg = f"{project_context}\n\nDecompose this project into epics."
        text1 = await _call_gateway(
            provider="claude",
            system_prompt=_EPIC_SYSTEM,
            user_message=user_msg,
            gateway_url=GATEWAY_URL,
        )

        epics = extract_json(text1)
        if not isinstance(epics, list):
            epics = []
        if not epics:
            raise RuntimeError(f"LLM returned no epics: {text1[:200]}")

        logger.info("athena_l0: generated %d epics for %s", len(epics), project_id[:8])

        # Turn 2: gap check in same conversation
        text2 = await _call_gateway(
            provider="claude",
            system_prompt=_EPIC_SYSTEM,
            user_message=_GAP_CHECK,
            gateway_url=GATEWAY_URL,
            history=[
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": text1},
            ],
        )

        additions = extract_json(text2)
        if isinstance(additions, list) and additions:
            logger.info("athena_l0: gap check added %d epics for %s", len(additions), project_id[:8])
            epics.extend(additions)

        # Write plan_nodes rows
        emits: list[Emit] = []
        for i, epic in enumerate(epics, start=1):
            node_id = uuid.uuid4().hex[:12]
            index_path = str(i)
            title = epic.get("title", f"Epic {i}")
            content = {
                "title": title,
                "description": epic.get("description", ""),
                "rationale": epic.get("rationale", ""),
            }
            await db.execute_write(
                "INSERT OR IGNORE INTO plan_nodes "
                "(id, plan_id, project_id, index_path, level, status, title, content_json, "
                "project_context, parent_index, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                (node_id, plan_id, project_id, index_path, 0, "stub",
                 title, json.dumps(content), project_context, None, now, now),
            )
            emits.append(Emit("plan_node_created", {
                "project_id": project_id,
                "node_id": node_id,
                "index_path": index_path,
                "level": 0,
                "plan_id": plan_id,
            }, source="athena_l0"))
            logger.info("athena_l0: created epic node %s '%s' for %s", index_path, title, project_id[:8])

        return emits

    except Exception as e:
        logger.error("athena_l0: epic generation failed for %s: %s", project_id, e, exc_info=True)
        await db.execute_write(
            "UPDATE projects SET status = $1, updated_at = $2 WHERE id = $3",
            ("failed", time.time(), project_id),
        )
        return [Emit("planning_failed", {
            "project_id": project_id,
            "error": str(e),
            "phase": "l0_epics",
        }, source="athena_l0")]
