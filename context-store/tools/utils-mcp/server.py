#!/usr/bin/env python3
#  CodeStoragePoc Utilities MCP Server
#
#  Dynamic task runner — discovers and executes Python scripts from tools/tasks/.
#  Drop a new .py file in tasks/, it's immediately available. No restart needed.
#
#  Each task file must define:
#    DESCRIPTION: str  — one-line summary
#    async def run(project_root: str, **params) -> str  — the task logic
#
#  Tools: list_tasks, run_task, store_turn, get_context
#
#  Depends on: mcp, psycopg2
#  Used by:    Claude Code (registered via `claude mcp add`)

import importlib.util
import json
import logging
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # tools/utils-mcp -> tools -> CodeStoragePoc
TASKS_DIR = SCRIPT_DIR.parent / "tasks"

DB_CONFIG = {
    "host": "localhost",
    "port": 5433,
    "dbname": "code_storage",
    "user": "postgres",
    "password": "postgres",
}

log = logging.getLogger("utils-mcp")
logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

mcp = FastMCP("codestoragepoc-utils")


def _discover_tasks() -> dict[str, dict]:
    """Scan tools/tasks/ for .py files with DESCRIPTION and run()."""
    tasks = {}
    if not TASKS_DIR.exists():
        return tasks

    for path in sorted(TASKS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            if hasattr(mod, "DESCRIPTION") and hasattr(mod, "run"):
                tasks[path.stem] = {
                    "description": mod.DESCRIPTION,
                    "file": str(path),
                    "module": mod,
                }
        except Exception as e:
            log.warning(f"Failed to load task {path.name}: {e}")
            tasks[path.stem] = {
                "description": f"[LOAD ERROR] {e}",
                "file": str(path),
                "module": None,
            }
    return tasks


@mcp.tool()
async def list_tasks() -> str:
    """List all available utility tasks from tools/tasks/.

    Each task is a Python script with a DESCRIPTION and async run() function.
    Returns task names, descriptions, and file paths.
    """
    tasks = _discover_tasks()
    if not tasks:
        return json.dumps({"tasks": [], "note": f"No tasks found in {TASKS_DIR}"})

    result = []
    for name, info in tasks.items():
        result.append({
            "name": name,
            "description": info["description"],
            "file": info["file"],
            "loadable": info["module"] is not None,
        })

    return json.dumps({"tasks": result, "count": len(result)}, indent=2)


@mcp.tool()
async def run_task(task: str, params: str = "{}") -> str:
    """Run a utility task by name.

    Args:
        task: Name of the task (filename without .py extension).
        params: JSON string of parameters to pass to the task's run() function.

    Returns the task's output string, or an error message if the task fails.
    """
    tasks = _discover_tasks()

    if task not in tasks:
        available = [name for name, info in tasks.items() if info["module"] is not None]
        return json.dumps({
            "error": f"Unknown task: {task}",
            "available": available,
        })

    info = tasks[task]
    if info["module"] is None:
        return json.dumps({"error": f"Task '{task}' failed to load: {info['description']}"})

    try:
        parsed_params = json.loads(params) if isinstance(params, str) else params
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid params JSON: {e}"})

    try:
        result = await info["module"].run(
            project_root=str(PROJECT_ROOT),
            **parsed_params,
        )
        return result
    except Exception as e:
        return json.dumps({
            "error": f"Task '{task}' failed: {e}",
            "traceback": traceback.format_exc(),
        })


# --- DB Context Tools ---


def _get_db():
    """Get a psycopg2 connection to the code_storage database."""
    import psycopg2
    return psycopg2.connect(**DB_CONFIG, connect_timeout=5)


@mcp.tool()
async def store_turn(role: str, content: str, conversation_id: str = "") -> str:
    """Store a conversation turn in the database as a node.

    Creates a new conversation if conversation_id is empty.
    Turns are stored as child nodes of a conversation node,
    making them searchable via get_context and queryable via SQL.

    Args:
        role: Who said this (user, assistant, claude, gemini, etc.)
        content: The message content.
        conversation_id: UUID of existing conversation, or empty to create new.

    Returns JSON with conversation_id and turn_id.
    """
    import psycopg2
    conn = psycopg2.connect(**DB_CONFIG, connect_timeout=5)
    cur = conn.cursor()
    try:
        # Find or create project for Claude Code sessions
        cur.execute("SELECT id FROM projects WHERE name = 'ClaudeCodeSessions'")
        row = cur.fetchone()
        if row:
            project_id = str(row[0])
        else:
            project_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO projects (id, name, root_path) VALUES (%s, %s, %s)",
                (project_id, "ClaudeCodeSessions", "claude-code"),
            )

        # Find or create conversation
        if conversation_id:
            conv_id = conversation_id
        else:
            conv_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO nodes (id, project_id, node_type, name, sibling_order) "
                "VALUES (%s, %s, %s, %s, %s)",
                (conv_id, project_id, "conversation",
                 f"Claude Code Session {datetime.now().isoformat()[:19]}", 0),
            )

        # Get next sibling order
        cur.execute(
            "SELECT COALESCE(MAX(sibling_order), 0) + 100 FROM nodes WHERE parent_id = %s",
            (conv_id,),
        )
        next_order = cur.fetchone()[0]

        # Insert turn node
        turn_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO nodes (id, project_id, node_type, value, parent_id, sibling_order, modified_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (turn_id, project_id, "turn", content, conv_id, next_order, "claude-code"),
        )

        # Insert attributes
        for key, val in [("speaker", role), ("input_mode", "text"), ("target", "user")]:
            cur.execute(
                "INSERT INTO node_attributes (id, node_id, key, value) "
                "VALUES (gen_random_uuid(), %s, %s, %s)",
                (turn_id, key, val),
            )

        conn.commit()
        return json.dumps({
            "conversation_id": conv_id,
            "turn_id": turn_id,
            "order": next_order,
        })
    except Exception as e:
        conn.rollback()
        return json.dumps({"error": str(e)})
    finally:
        conn.close()


@mcp.tool()
async def get_context(query: str, node_types: str = "", since: str = "",
                      conversation_id: str = "", limit: int = 5) -> str:
    """Search nodes in the database with optional filters.

    Returns matching nodes with their type, name, value (truncated),
    and parent info. Use this to find relevant past conversations,
    ideas, decisions, code nodes, and plans.

    Args:
        query: Text to search for (case-insensitive substring match).
        node_types: Comma-separated type filter (e.g. "decision,idea,turn").
        since: Time window — "30m", "2h", "1d". Only nodes modified within this window.
        conversation_id: Scope search to children of this conversation UUID.
        limit: Maximum number of results (default 5).

    Returns JSON array of matching nodes.
    """
    import re
    import psycopg2

    conn = psycopg2.connect(**DB_CONFIG, connect_timeout=5)
    cur = conn.cursor()
    try:
        conditions = []
        params = []

        # Text search
        if query:
            conditions.append("(n.name ILIKE %s OR n.value ILIKE %s)")
            pattern = f"%{query}%"
            params.extend([pattern, pattern])

        # Type filter
        if node_types:
            type_list = [t.strip() for t in node_types.split(",") if t.strip()]
            if type_list:
                placeholders = ",".join(["%s"] * len(type_list))
                conditions.append(f"n.node_type IN ({placeholders})")
                params.extend(type_list)

        # Time filter
        if since:
            match = re.match(r"^(\d+)([mhd])$", since.strip())
            if match:
                val, unit = int(match.group(1)), match.group(2)
                from datetime import timedelta
                delta = {"m": timedelta(minutes=val), "h": timedelta(hours=val),
                         "d": timedelta(days=val)}.get(unit)
                if delta:
                    conditions.append("n.modified_at >= %s")
                    params.append(datetime.now() - delta)

        # Conversation scope
        if conversation_id:
            conditions.append("(n.parent_id = %s OR n.id = %s)")
            params.extend([conversation_id, conversation_id])

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        cur.execute(
            f"""
            SELECT n.id, n.node_type, n.name, LEFT(n.value, 200) as value,
                   p.node_type as parent_type, p.name as parent_name,
                   n.modified_at
            FROM nodes n
            LEFT JOIN nodes p ON n.parent_id = p.id
            {where}
            ORDER BY n.modified_at DESC
            LIMIT %s
            """,
            params + [limit],
        )

        results = []
        for row in cur.fetchall():
            results.append({
                "id": str(row[0]),
                "type": row[1],
                "name": row[2],
                "value": row[3],
                "parent_type": row[4],
                "parent_name": row[5],
                "modified_at": row[6].isoformat() if row[6] else None,
            })

        return json.dumps(
            {"matches": results, "count": len(results), "query": query,
             "filters": {"node_types": node_types, "since": since,
                         "conversation_id": conversation_id}},
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": str(e)})
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
