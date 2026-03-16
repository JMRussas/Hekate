#!/usr/bin/env python3
#  CodeStoragePoc Skills MCP Server
#
#  Dynamic MCP server that reads skill definitions from skills.json
#  and exposes them as tools. Handles DB queries for search_ideas,
#  get_node_details, and list_threads. The route_to_model skill
#  is handled by ChatService directly (marked as builtin).
#
#  Tools: list_skills, execute_skill
#
#  Depends on: mcp, psycopg2
#  Used by:    ChatService (via HTTP), Claude Code (via stdio)

import json
import logging
import os
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras
from mcp.server.fastmcp import FastMCP

SCRIPT_DIR = Path(__file__).parent
SKILLS_DIR = SCRIPT_DIR.parent / "skills"
SKILLS_FILE = SKILLS_DIR / "skills.json"

log = logging.getLogger("skills-mcp")
logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

DB_CONFIG = {
    "host": "localhost",
    "port": 5433,
    "dbname": "code_storage",
    "user": "postgres",
    "password": "postgres",
}

PROJECT_ID = "8196b44e-6299-45a0-a5b0-bbd111f2990b"

mcp_server = FastMCP("codestoragepoc-skills")


def _load_skills():
    """Load skill definitions from skills.json. Re-reads on every call for hot reload."""
    with open(SKILLS_FILE) as f:
        data = json.load(f)
    return data.get("skills", [])


def _get_db():
    """Get a database connection."""
    return psycopg2.connect(**DB_CONFIG)


# --- MCP Tools ---

@mcp_server.tool()
def list_skills() -> str:
    """List all available skills with their names, descriptions, and parameters.

    Returns the full skill catalog. Use this to discover what tools are available
    before calling execute_skill.
    """
    skills = _load_skills()
    result = []
    for s in skills:
        result.append({
            "name": s["name"],
            "description": s["description"],
            "handler": s["handler"],
            "parameters": s.get("parameters", {}),
        })
    return json.dumps(result, indent=2)


@mcp_server.tool()
def execute_skill(name: str, params: str = "{}") -> str:
    """Execute a skill by name with the given parameters.

    Args:
        name: The skill name (e.g., search_ideas, get_node_details, list_threads)
        params: JSON string of parameters for the skill

    Returns a JSON result from the skill execution.
    """
    skills = _load_skills()
    skill = next((s for s in skills if s["name"] == name), None)
    if not skill:
        return json.dumps({"error": f"Unknown skill: {name}", "available": [s["name"] for s in skills]})

    if skill["handler"] == "builtin":
        return json.dumps({"error": f"Skill '{name}' is handled by ChatService directly, not via MCP"})

    try:
        parsed_params = json.loads(params) if isinstance(params, str) else params
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid params JSON: {e}"})

    # Dispatch to handler
    handlers = {
        "search_ideas": _handle_search_ideas,
        "get_node_details": _handle_get_node_details,
        "list_threads": _handle_list_threads,
        "orchestrate": _handle_orchestrate,
    }

    handler = handlers.get(name)
    if not handler:
        return json.dumps({"error": f"No handler implemented for skill: {name}"})

    try:
        return json.dumps(handler(parsed_params), indent=2)
    except Exception as e:
        log.error(f"Skill execution error: {e}")
        return json.dumps({"error": str(e)})


# --- Skill Handlers ---

def _handle_search_ideas(params: dict) -> dict:
    """Search ideas, questions, decisions by text match."""
    query = params.get("query", "")
    limit = params.get("limit", 5)

    if not query:
        return {"error": "query parameter is required"}

    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT n.id, n.node_type, n.name, n.value,
                       (SELECT na.value FROM node_attributes na
                        WHERE na.node_id = n.id AND na.key = 'status') as status
                FROM nodes n
                WHERE n.project_id = %s
                  AND n.node_type IN ('idea', 'question', 'decision', 'action_item')
                  AND (n.name ILIKE %s OR n.value ILIKE %s)
                ORDER BY n.modified_at DESC
                LIMIT %s
            """, (PROJECT_ID, f"%{query}%", f"%{query}%", limit))
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        "query": query,
        "count": len(rows),
        "results": [
            {
                "id": str(r["id"]),
                "type": r["node_type"],
                "name": r["name"],
                "description": r["value"],
                "status": r["status"] or "mentioned",
            }
            for r in rows
        ],
    }


def _handle_get_node_details(params: dict) -> dict:
    """Get a node with its attributes and optionally children."""
    node_id = params.get("node_id")
    include_children = params.get("include_children", True)

    if not node_id:
        return {"error": "node_id parameter is required"}

    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get node
            cur.execute("""
                SELECT id, node_type, name, value, parent_id, created_at, modified_at
                FROM nodes WHERE id = %s
            """, (node_id,))
            node = cur.fetchone()
            if not node:
                return {"error": f"Node not found: {node_id}"}

            # Get attributes
            cur.execute("""
                SELECT key, value FROM node_attributes WHERE node_id = %s
            """, (node_id,))
            attrs = {r["key"]: r["value"] for r in cur.fetchall()}

            result = {
                "id": str(node["id"]),
                "type": node["node_type"],
                "name": node["name"],
                "value": node["value"],
                "parent_id": str(node["parent_id"]) if node["parent_id"] else None,
                "created_at": str(node["created_at"]),
                "attributes": attrs,
            }

            # Get children
            if include_children:
                cur.execute("""
                    SELECT id, node_type, name, value
                    FROM nodes WHERE parent_id = %s
                    ORDER BY sibling_order, created_at
                """, (node_id,))
                result["children"] = [
                    {
                        "id": str(r["id"]),
                        "type": r["node_type"],
                        "name": r["name"],
                        "value": r["value"][:200] if r["value"] else None,
                    }
                    for r in cur.fetchall()
                ]
    finally:
        conn.close()

    return result


def _handle_list_threads(params: dict) -> dict:
    """List conversation threads with status and activity."""
    status_filter = params.get("status", "open")
    limit = params.get("limit", 10)

    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            status_clause = ""
            query_params = [PROJECT_ID]

            if status_filter == "open":
                status_clause = """AND (SELECT na.value FROM node_attributes na
                    WHERE na.node_id = n.id AND na.key = 'status') != 'parked'"""
            elif status_filter == "parked":
                status_clause = """AND (SELECT na.value FROM node_attributes na
                    WHERE na.node_id = n.id AND na.key = 'status') = 'parked'"""

            query_params.append(limit)

            cur.execute(f"""
                SELECT n.id, n.name, n.created_at,
                       (SELECT na.value FROM node_attributes na
                        WHERE na.node_id = n.id AND na.key = 'status') as status,
                       (SELECT na.value FROM node_attributes na
                        WHERE na.node_id = n.id AND na.key = 'model') as model,
                       (SELECT COUNT(*) FROM nodes c WHERE c.parent_id = n.id) as child_count
                FROM nodes n
                WHERE n.project_id = %s
                  AND n.node_type = 'thread'
                  {status_clause}
                ORDER BY n.created_at DESC
                LIMIT %s
            """, query_params)
            rows = cur.fetchall()
    finally:
        conn.close()

    return {
        "filter": status_filter,
        "count": len(rows),
        "threads": [
            {
                "id": str(r["id"]),
                "name": r["name"],
                "model": r["model"],
                "status": r["status"] or "open",
                "child_count": r["child_count"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ],
    }


def _handle_orchestrate(params: dict) -> dict:
    """Create an orchestration project, generate a plan, and optionally start execution."""
    name = params.get("name")
    requirements = params.get("requirements")
    if not name or not requirements:
        return {"error": "name and requirements are required"}

    repo_path = params.get("repo_path")
    rigor = params.get("rigor", "L2")
    auto_start = params.get("auto_start", False)

    orch_url = os.environ.get("ORCHESTRATION_URL", "http://localhost:5200")
    api_key = os.environ.get("ORCHESTRATION_API_KEY", "")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        client = httpx.Client(timeout=60.0)

        # 1. Create project
        create_body = {
            "name": name,
            "requirements": requirements,
            "planning_rigor": rigor,
        }
        if repo_path:
            create_body["repo_path"] = repo_path

        resp = client.post(f"{orch_url}/api/projects", json=create_body, headers=headers)
        resp.raise_for_status()
        project = resp.json()
        project_id = project["id"]

        result = {
            "project_id": project_id,
            "name": name,
            "status": "created",
        }

        # 2. Generate plan
        resp = client.post(f"{orch_url}/api/projects/{project_id}/plan", headers=headers)
        resp.raise_for_status()
        plan_result = resp.json()
        result["plan_id"] = plan_result.get("plan_id")
        result["plan_summary"] = plan_result.get("plan", {}).get("summary", "")
        result["status"] = "planned"

        # Count tasks across phases
        plan = plan_result.get("plan", {})
        task_count = 0
        phases = plan.get("phases", [])
        if phases:
            for phase in phases:
                task_count += len(phase.get("tasks", []))
        else:
            task_count = len(plan.get("tasks", []))
        result["task_count"] = task_count

        # 3. Optionally approve and start
        if auto_start and result.get("plan_id"):
            # Approve plan (decomposes into tasks)
            resp = client.post(
                f"{orch_url}/api/projects/{project_id}/plans/{result['plan_id']}/approve",
                headers=headers,
            )
            resp.raise_for_status()
            decompose_result = resp.json()
            result["tasks_created"] = decompose_result.get("tasks_created", 0)

            # Start execution
            resp = client.post(f"{orch_url}/api/projects/{project_id}/execute", headers=headers)
            resp.raise_for_status()
            result["status"] = "executing"

        client.close()
        return result

    except httpx.HTTPStatusError as exc:
        return {
            "error": f"Orchestration API error: {exc.response.status_code}",
            "detail": exc.response.text[:500],
        }
    except httpx.ConnectError:
        return {"error": f"Cannot connect to orchestration engine at {orch_url}"}
    except Exception as exc:
        return {"error": f"Orchestration failed: {str(exc)}"}


if __name__ == "__main__":
    mcp_server.run()
