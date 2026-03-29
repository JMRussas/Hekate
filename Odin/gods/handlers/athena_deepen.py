"""Athena deepen handler — parallel node deepening.

Handles plan_node_created events. Each node is deepened independently,
allowing full parallelism across the tree.

Flow per node:
  plan_node_created (level N)
    → idempotency guard (skip if status != stub)
    → load parent chain for context
    → LLM call: generate N+1 children (gateway for L0-L1, sub-agent for L2+)
    → gap check: same conversation, "did you miss anything?"
    → write child plan_nodes rows
    → emit plan_node_created per child (fan-out)
    → emit plan_node_complete for this node

Level semantics:
  L0 node → generates L1 task stubs
  L1 node → generates L2 specs
  L2 node → generates L3 implementation detail
  L3 node → generates L4 exact changes
  L4 node → generates L5 executable specs
  L5 node → emits plan_node_executable (no children)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from gods.pipeline import Event, Emit
from gods import safe_json

logger = logging.getLogger("gods.handlers.athena_deepen")

from gods.config import GATEWAY_URL
GATEWAY_WS_URL = GATEWAY_URL.replace("http://", "ws://").replace("https://", "wss://")

# Import at module level so tests can patch gods.handlers.athena_deepen._call_gateway
from gods.handlers.athena_leveled import _call_gateway  # noqa: E402
from gods.providers.response_validator import extract_json  # noqa: E402

# Max nodes in-flight (planning sub-agents) at once
MAX_CONCURRENT_NODES = 4

# ---------------------------------------------------------------------------
# Level-specific prompts
# ---------------------------------------------------------------------------

_LEVEL_DESCRIPTIONS = {
    0: "L1 task stubs — concrete tasks within this epic (title, task_type, rough description)",
    1: "L2 detailed specs — for each task: description, affected_files, depends_on, complexity",
    2: "L3 implementation detail — for each task: implementation_notes, test_strategy, edge_cases",
    3: "L4 exact changes — for each task: specific code changes with file, action, name, signature",
    4: "L5 executable specs — for each task: complete code bodies ready for direct execution",
}

_SYSTEM_TEMPLATE = """\
You are a software architect deepening a plan node.

Project context:
{project_context}

Your current node (level {level}):
  Index: {index_path}
  Title: {title}
  Content: {content}

Parent context (ancestors):
{parent_chain}

Your job: Generate {child_level_desc} for this node.

Output ONLY a JSON array of child nodes. Each child must have:
{schema}

No prose, no explanation. JSON array only.
"""

_SCHEMAS = {
    0: '{"title": "...", "task_type": "code|research|test|docs", "description": "..."}',
    1: '{"title": "...", "task_type": "code|research|test|docs", "description": "...", "affected_files": ["..."], "depends_on_indices": [0, 1], "complexity": "simple|medium|complex"}',
    2: '{"title": "...", "description": "...", "affected_files": ["..."], "depends_on_indices": [], "complexity": "...", "implementation_notes": "...", "test_strategy": "...", "edge_cases": ["..."]}',
    3: '{"title": "...", "description": "...", "affected_files": ["..."], "implementation_notes": "...", "changes": [{"file": "...", "action": "add|modify|delete", "name": "...", "signature": "...", "returns": "..."}]}',
    4: '{"title": "...", "description": "...", "affected_files": ["..."], "implementation_notes": "...", "changes": [{"file": "...", "action": "add|modify|delete", "name": "...", "signature": "...", "returns": "...", "body": "..."}]}',
}

_GAP_CHECK_MSG = """\
You just generated the above children for node "{title}".

Review them:
1. Are there any missing children that should be added?
2. Are any children redundant and should be removed?
3. Is any child too broad and should be split?

If you have additions, output a JSON array of new child nodes using the same schema.
If the list is complete and correct, output an empty array: []

JSON array only, no prose.
"""


# ---------------------------------------------------------------------------
# Concurrency tracking (module-level, in-process)
# ---------------------------------------------------------------------------

_in_flight_nodes: set[str] = set()  # node_ids currently being deepened


# ---------------------------------------------------------------------------
# Helper: load parent chain
# ---------------------------------------------------------------------------

async def _load_parent_chain(project_id: str, parent_index: str | None, db) -> str:
    """Walk up the tree and return a text summary of ancestor nodes."""
    if not parent_index:
        return "(no parent — this is a root epic)"

    chain_parts = []
    current = parent_index
    while current:
        row = await db.fetchone(
            "SELECT index_path, level, title, content_json, parent_index "
            "FROM plan_nodes WHERE project_id = $1 AND index_path = $2",
            (project_id, current),
        )
        if not row:
            break
        content = json.loads(row["content_json"] or "{}")
        chain_parts.append(
            f"  [{row['index_path']}] {row['title']}: {content.get('description', '')[:200]}"
        )
        current = row["parent_index"]

    if not chain_parts:
        return "(parent not found)"
    return "\n".join(reversed(chain_parts))


# ---------------------------------------------------------------------------
# Helper: assign index paths to children
# ---------------------------------------------------------------------------

async def _next_child_index(project_id: str, parent_index: str, db) -> int:
    """Return the next available child position under parent_index."""
    row = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM plan_nodes "
        "WHERE project_id = $1 AND parent_index = $2",
        (project_id, parent_index),
    )
    return (row["cnt"] if row else 0) + 1


# ---------------------------------------------------------------------------
# Sub-agent helpers
# ---------------------------------------------------------------------------

# Gap threshold (seconds) — log slow event if no output received for this long
_SLOW_GAP_THRESHOLD = 120


async def _save_plan_children(node_id: str, children: list[dict], db) -> dict:
    """Save child plan_nodes to DB under the given node.

    Returns {"saved": N, "node_ids": [...]} or {"error": "..."}.
    Used both by athena_deepen directly and by the submit_plan_children engine endpoint.
    """
    node = await db.fetchone(
        "SELECT * FROM plan_nodes WHERE id = $1",
        (node_id,),
    )
    if not node:
        return {"error": f"node {node_id} not found"}

    if not children:
        return {"saved": 0, "node_ids": []}

    project_id = node["project_id"]
    plan_id = node["plan_id"]
    index_path = node["index_path"]
    child_level = (node["level"] or 0) + 1
    project_context = node["project_context"] or ""

    # Start numbering after any already-existing children
    cnt_row = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND parent_index = $2",
        (project_id, index_path),
    )
    start_idx = (cnt_row["cnt"] if cnt_row else 0) + 1
    now = time.time()
    node_ids: list[str] = []

    for i, child in enumerate(children, start=start_idx):
        child_id = uuid.uuid4().hex[:12]
        child_index = f"{index_path}.{i}"
        child_title = child.get("title", f"Node {child_index}")

        # Resolve depends_on_indices to sibling index paths
        dep_indices = child.get("depends_on_indices", [])
        dep_paths = [f"{index_path}.{d + 1}" for d in dep_indices if isinstance(d, int)]
        child["depends_on_paths"] = dep_paths

        await db.execute_write(
            "INSERT OR IGNORE INTO plan_nodes "
            "(id, plan_id, project_id, index_path, level, status, title, "
            "content_json, project_context, parent_index, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
            (
                child_id, plan_id, project_id, child_index, child_level,
                "stub", child_title, json.dumps(child),
                project_context, index_path, now, now,
            ),
        )
        node_ids.append(child_id)

    return {"saved": len(node_ids), "node_ids": node_ids}


async def _peer_plan_node(node: dict, db) -> list[dict]:
    """Peer plan a node via the gateway WebSocket conversation runtime.

    Connects to the gateway WS endpoint, sends a planning prompt, receives
    the agent's streamed response (including tool calls to submit_plan_children),
    then sends a gap-check follow-up in the same session.

    Returns list of children rows from DB (already saved by submit_plan_children).
    Falls back to direct gateway HTTP call if WebSocket fails.
    """
    import websockets

    project_id = node["project_id"]
    index_path = node["index_path"]
    level = node["level"]
    title = node["title"]
    content = json.loads(node["content_json"] or "{}")
    project_context = node["project_context"] or ""
    node_id = node["id"]

    child_level = level + 1
    child_level_desc = _LEVEL_DESCRIPTIONS.get(level, f"L{child_level} nodes")
    schema = _SCHEMAS.get(level, "{}")

    planning_prompt = (
        f"You are a peer software architect deepening a plan node. "
        f"Think through this collaboratively — talk through your reasoning step by step.\n\n"
        f"Project context:\n{project_context[:1500]}\n\n"
        f"Current node:\n"
        f"  Index: {index_path}\n"
        f"  Level: {level}\n"
        f"  Title: {title}\n"
        f"  Content: {json.dumps(content)[:500]}\n\n"
        f"Your job: Generate {child_level_desc} for this node.\n"
        f"Each child must follow this schema:\n{schema}\n\n"
        f"When you have your final list, call submit_plan_children(node_id=\"{node_id}\", children=[...]).\n"
        f"Do NOT output raw JSON — use the tool to save results."
    )

    gap_check_prompt = (
        "Review what you just planned. Did you miss anything important? "
        "Are there any gaps — implied work with no task covering it? "
        "If yes, call submit_plan_children again with the additional children. "
        "If the plan is complete, just say so briefly."
    )

    agent_start = time.time()
    ws_url = f"{GATEWAY_WS_URL}/v1/conversation"

    try:
        async with websockets.connect(ws_url) as ws:
            conversation_id = None

            async def send_message(msg: str):
                await ws.send(json.dumps({
                    "type": "message",
                    "content": msg,
                    "model": "claude-sonnet-4-6",
                    "allowed_tools": ["mcp__prometheus__submit_plan_children"],
                }))

            async def drain_until_done():
                """Read events until done, logging slow gaps."""
                last_event_at = time.time()
                async for raw in ws:
                    event = json.loads(raw)
                    now = time.time()
                    gap = now - last_event_at
                    if gap >= _SLOW_GAP_THRESHOLD:
                        elapsed = now - agent_start
                        logger.warning(
                            "athena_deepen: peer agent silent %.0fs (%.0fs total) for node %s",
                            gap, elapsed, index_path,
                        )
                        await db.execute_write(
                            "UPDATE plan_nodes SET agent_slow_at = $1, updated_at = $2 WHERE id = $3",
                            (now, now, node_id),
                        )
                    last_event_at = now

                    etype = event.get("type")
                    if etype == "conversation_id":
                        nonlocal conversation_id
                        conversation_id = event.get("conversation_id")
                    elif etype == "tool_call":
                        logger.info(
                            "athena_deepen: agent called %s for node %s",
                            event.get("name"), index_path,
                        )
                    elif etype == "slow":
                        logger.warning(
                            "athena_deepen: gateway reports slow gap %.0fs for node %s",
                            event.get("gap_s", 0), index_path,
                        )
                    elif etype == "done":
                        break

            # Turn 1: planning
            logger.info(
                "athena_deepen: starting peer planning session for node %s (L%d)",
                index_path, level,
            )
            await send_message(planning_prompt)
            await drain_until_done()

            # Turn 2: gap check (same session, full context)
            await send_message(gap_check_prompt)
            await drain_until_done()

    except Exception as e:
        logger.warning(
            "athena_deepen: WebSocket peer planning failed for node %s: %s — falling back to HTTP gateway",
            index_path, e,
        )
        # Fallback: direct HTTP gateway call
        parent_chain = await _load_parent_chain(project_id, node.get("parent_index"), db)
        system_prompt = _SYSTEM_TEMPLATE.format(
            project_context=project_context[:2000],
            level=level,
            index_path=index_path,
            title=title,
            content=json.dumps(content)[:500],
            parent_chain=parent_chain[:1000],
            child_level_desc=child_level_desc,
            schema=schema,
        )
        user_msg = f"Generate {child_level_desc} for: {title}"
        text = await _call_gateway(
            provider="claude",
            system_prompt=system_prompt,
            user_message=user_msg,
            gateway_url=GATEWAY_URL,
        )
        parsed = extract_json(text)
        if isinstance(parsed, list):
            return parsed
        return []

    agent_duration = time.time() - agent_start
    await db.execute_write(
        "UPDATE plan_nodes SET agent_duration_s = $1, updated_at = $2 WHERE id = $3",
        (agent_duration, time.time(), node_id),
    )

    # Read back children saved by the agent via submit_plan_children
    saved = await db.fetchall(
        "SELECT * FROM plan_nodes WHERE project_id = $1 AND parent_index = $2",
        (project_id, index_path),
    )
    children = list(saved) if saved else []

    if not children:
        logger.warning(
            "athena_deepen: peer agent saved 0 children for node %s (%.0fs) — falling back to HTTP gateway",
            index_path, agent_duration,
        )
        # Fallback
        parent_chain = await _load_parent_chain(project_id, node.get("parent_index"), db)
        system_prompt = _SYSTEM_TEMPLATE.format(
            project_context=project_context[:2000],
            level=level,
            index_path=index_path,
            title=title,
            content=json.dumps(content)[:500],
            parent_chain=parent_chain[:1000],
            child_level_desc=child_level_desc,
            schema=schema,
        )
        user_msg = f"Generate {child_level_desc} for: {title}"
        text = await _call_gateway(
            provider="claude",
            system_prompt=system_prompt,
            user_message=user_msg,
            gateway_url=GATEWAY_URL,
        )
        parsed = extract_json(text)
        return parsed if isinstance(parsed, list) else []

    logger.info(
        "athena_deepen: peer agent saved %d children for node %s (%.0fs)",
        len(children), index_path, agent_duration,
    )
    return children


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------

async def athena_deepen(event: Event, db) -> list[Emit]:
    """Handle plan_node_created — deepen one node, emit children.

    Handles all levels 0-4 (L0 nodes generate L1 children, etc).
    L5 nodes are terminal — emit plan_node_executable instead.
    """
    payload = event.payload
    project_id = payload.get("project_id")
    node_id = payload.get("node_id")
    index_path = payload.get("index_path", "?")
    level = payload.get("level", 0)

    if not project_id or not node_id:
        logger.error("athena_deepen: missing project_id or node_id")
        return []

    # Idempotency: skip if already being deepened.
    # Add node_id to _in_flight_nodes immediately (no await between check and add)
    # so that concurrent calls for the same node see the set update atomically.
    if node_id in _in_flight_nodes:
        logger.debug("athena_deepen: node %s already in-flight, skipping", node_id)
        return []

    # Concurrency gate: check and add atomically (no await between these two lines)
    if len(_in_flight_nodes) >= MAX_CONCURRENT_NODES:
        logger.info(
            "athena_deepen: %d nodes in-flight (max %d), re-queuing node %s",
            len(_in_flight_nodes), MAX_CONCURRENT_NODES, index_path,
        )
        return [Emit("plan_node_created", payload, source="athena_deepen")]
    _in_flight_nodes.add(node_id)

    # Now we own this node — do the DB fetch
    node = await db.fetchone(
        "SELECT * FROM plan_nodes WHERE id = $1",
        (node_id,),
    )
    if not node:
        logger.error("athena_deepen: node %s not found", node_id)
        _in_flight_nodes.discard(node_id)
        return []

    if node["status"] != "stub":
        logger.debug(
            "athena_deepen: node %s status=%s, skipping (idempotent)",
            node_id, node["status"],
        )
        _in_flight_nodes.discard(node_id)
        return []

    # L5 nodes are terminal — no children to generate
    if level >= 5:
        logger.info("athena_deepen: L5 node %s is terminal, emitting executable", index_path)
        _in_flight_nodes.discard(node_id)
        return [Emit("plan_node_executable", {
            "project_id": project_id,
            "node_id": node_id,
            "index_path": index_path,
        }, source="athena_deepen")]
    now = time.time()

    try:
        # Mark as planning
        await db.execute_write(
            "UPDATE plan_nodes SET status = $1, updated_at = $2 WHERE id = $3",
            ("planning", now, node_id),
        )

        title = node["title"] or f"Node {index_path}"
        content = json.loads(node["content_json"] or "{}")
        project_context = node["project_context"] or ""
        parent_index = node["parent_index"]

        # Load parent chain for context
        parent_chain = await _load_parent_chain(project_id, parent_index, db)

        # Build prompt
        child_level = level + 1
        system_prompt = _SYSTEM_TEMPLATE.format(
            project_context=project_context[:2000],
            level=level,
            index_path=index_path,
            title=title,
            content=json.dumps(content)[:500],
            parent_chain=parent_chain[:1000],
            child_level_desc=_LEVEL_DESCRIPTIONS.get(level, f"L{child_level} nodes"),
            schema=_SCHEMAS.get(level, "{}"),
        )
        user_msg = f"Generate {_LEVEL_DESCRIPTIONS.get(level, f'L{child_level} children')} for: {title}"

        logger.info(
            "athena_deepen: deepening node %s (L%d→L%d) for project %s",
            index_path, level, child_level, project_id[:8],
        )

        # children_already_saved: True when L2+ agent wrote them via submit_plan_children
        children_already_saved = False

        if level >= 2:
            # L2+ — peer plan via gateway WebSocket (full conversation, gap check included)
            raw = await _peer_plan_node(node, db)
            if not isinstance(raw, list):
                raw = []
            # If rows have "id" key they're already in DB (agent saved them)
            if raw and "id" in raw[0]:
                children = raw
                children_already_saved = True
            else:
                children = raw
        else:
            # L0-L1 — direct gateway call with gap check
            text1 = await _call_gateway(
                provider="claude",
                system_prompt=system_prompt,
                user_message=user_msg,
                gateway_url=GATEWAY_URL,
            )

            children = extract_json(text1)
            if not isinstance(children, list):
                children = []

            if children:
                # Gap check: same conversation
                await db.execute_write(
                    "UPDATE plan_nodes SET status = $1, updated_at = $2 WHERE id = $3",
                    ("gap_check", time.time(), node_id),
                )

                text2 = await _call_gateway(
                    provider="claude",
                    system_prompt=system_prompt,
                    user_message=_GAP_CHECK_MSG.format(title=title),
                    gateway_url=GATEWAY_URL,
                    history=[
                        {"role": "user", "content": user_msg},
                        {"role": "assistant", "content": text1},
                    ],
                )

                additions = extract_json(text2)
                if isinstance(additions, list) and additions:
                    logger.info(
                        "athena_deepen: gap check added %d children to node %s",
                        len(additions), index_path,
                    )
                    children.extend(additions)

        if not children:
            logger.warning(
                "athena_deepen: no children generated for node %s, marking complete",
                index_path,
            )
            await db.execute_write(
                "UPDATE plan_nodes SET status = $1, updated_at = $2 WHERE id = $3",
                ("complete", time.time(), node_id),
            )
            return [Emit("plan_node_complete", {
                "project_id": project_id,
                "node_id": node_id,
                "index_path": index_path,
                "level": level,
                "parent_index": parent_index,
            }, source="athena_deepen")]

        plan_id = node["plan_id"]
        emits: list[Emit] = []
        now2 = time.time()

        child_node_ids: list[str] = []

        if children_already_saved:
            # Agent already wrote rows — just collect IDs and build emits
            for child_row in children:
                child_id = child_row["id"]
                child_index = child_row["index_path"]
                child_level_actual = child_row.get("level", child_level)
                child_node_ids.append(child_id)
                if child_level_actual >= 5:
                    emits.append(Emit("plan_node_executable", {
                        "project_id": project_id,
                        "node_id": child_id,
                        "index_path": child_index,
                    }, source="athena_deepen"))
                else:
                    emits.append(Emit("plan_node_created", {
                        "project_id": project_id,
                        "node_id": child_id,
                        "index_path": child_index,
                        "level": child_level_actual,
                        "plan_id": plan_id,
                    }, source="athena_deepen"))
        else:
            for i, child in enumerate(children, start=1):
                child_id = uuid.uuid4().hex[:12]
                child_index = f"{index_path}.{i}"
                child_title = child.get("title", f"Node {child_index}")

                # Resolve depends_on_indices to sibling index paths
                dep_indices = child.get("depends_on_indices", [])
                dep_paths = [f"{index_path}.{d + 1}" for d in dep_indices if isinstance(d, int)]
                child["depends_on_paths"] = dep_paths

                await db.execute_write(
                    "INSERT OR IGNORE INTO plan_nodes "
                    "(id, plan_id, project_id, index_path, level, status, title, "
                    "content_json, project_context, parent_index, created_at, updated_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                    (
                        child_id, plan_id, project_id, child_index, child_level,
                        "stub", child_title, json.dumps(child),
                        project_context, index_path, now2, now2,
                    ),
                )
                child_node_ids.append(child_id)

                if child_level >= 5:
                    emits.append(Emit("plan_node_executable", {
                        "project_id": project_id,
                        "node_id": child_id,
                        "index_path": child_index,
                    }, source="athena_deepen"))
                else:
                    emits.append(Emit("plan_node_created", {
                        "project_id": project_id,
                        "node_id": child_id,
                        "index_path": child_index,
                        "level": child_level,
                        "plan_id": plan_id,
                    }, source="athena_deepen"))

        logger.info(
            "athena_deepen: node %s deepened → %d L%d children for project %s",
            index_path, len(children), child_level, project_id[:8],
        )

        # Mark this node complete (its children will bubble up when they complete)
        await db.execute_write(
            "UPDATE plan_nodes SET status = $1, updated_at = $2 WHERE id = $3",
            ("complete", time.time(), node_id),
        )

        # Emit plan_node_complete so bubble-up can propagate
        emits.append(Emit("plan_node_complete", {
            "project_id": project_id,
            "node_id": node_id,
            "index_path": index_path,
            "level": level,
            "parent_index": parent_index,
            "child_count": len(children),
        }, source="athena_deepen"))

        return emits

    except Exception as e:
        logger.error(
            "athena_deepen: failed to deepen node %s for project %s: %s",
            index_path, project_id[:8], e, exc_info=True,
        )
        try:
            await db.execute_write(
                "UPDATE plan_nodes SET status = $1, error = $2, updated_at = $3 WHERE id = $4",
                ("failed", str(e)[:500], time.time(), node_id),
            )
        except Exception as write_err:
            logger.error(
                "athena_deepen: failed to mark node %s as failed: %s",
                node_id, write_err,
            )
        return [Emit("plan_node_complete", {
            "project_id": project_id,
            "node_id": node_id,
            "index_path": index_path,
            "level": level,
            "parent_index": node["parent_index"] if node else None,
            "failed": True,
        }, source="athena_deepen")]

    finally:
        _in_flight_nodes.discard(node_id)
