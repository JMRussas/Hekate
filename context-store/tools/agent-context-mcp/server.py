#!/usr/bin/env python3
#  CodeStoragePoc Agent Context MCP Server
#
#  Persistent memory for AI agents. Store decisions, findings, ideas,
#  and plans as nodes in the shared graph DB. Query them back by text
#  search, semantic similarity, tree navigation, or temporal history.
#
#  Tools: ensure_project, store_node, query_nodes, semantic_search,
#         get_node, get_children, list_projects, list_recent, history
#
#  Depends on: mcp, psycopg2, httpx
#  Used by:    Claude Code (registered via `claude mcp add`)

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras
from mcp.server.fastmcp import FastMCP

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # tools/agent-context-mcp -> tools -> CodeStoragePoc

log = logging.getLogger("agent-context")
logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

# --- Configuration ---

DB_CONFIG = {
    "host": os.environ.get("CODESTORAGE_DB_HOST", "localhost"),
    "port": int(os.environ.get("CODESTORAGE_DB_PORT", "5433")),
    "dbname": os.environ.get("CODESTORAGE_DB_NAME", "code_storage"),
    "user": os.environ.get("CODESTORAGE_DB_USER", "postgres"),
    "password": os.environ.get("CODESTORAGE_DB_PASSWORD", "postgres"),
}

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
EMBED_TIMEOUT = float(os.environ.get("OLLAMA_EMBED_TIMEOUT", "2.0"))
EMBED_DIMENSIONS = 768

# Gap numbering increment for sibling_order
SIBLING_GAP = 100

# AGE graph name (must match init.sql)
AGE_GRAPH = "code_graph"

_PORT = int(os.environ.get("MCP_PORT", "0"))
mcp = FastMCP("agent-context", port=_PORT) if _PORT else FastMCP("agent-context")


# --- Helpers ---

def _get_db():
    """Get a database connection."""
    return psycopg2.connect(**DB_CONFIG, connect_timeout=5)


def _serialize_row(row: dict) -> dict:
    """Convert a RealDictRow to JSON-safe dict."""
    result = {}
    for k, v in row.items():
        if isinstance(v, uuid.UUID):
            result[k] = str(v)
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


def _next_sibling_order(conn, parent_id: str) -> int:
    """Get the next available sibling_order for children of a parent."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(sibling_order), 0) + %s FROM nodes WHERE parent_id = %s",
            (SIBLING_GAP, parent_id),
        )
        return cur.fetchone()[0]


def _embed_text(text: str) -> list[float] | None:
    """Compute embedding via Ollama. Returns 768-dim vector or None on failure.

    Uses 'search_document:' prefix per nomic-embed-text convention for storage.
    Timeout: 2s (configurable via OLLAMA_EMBED_TIMEOUT). On timeout or error,
    returns None silently — the node is stored without an embedding.
    """
    if not text or not text.strip():
        return None
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": f"search_document: {text}"},
            timeout=EMBED_TIMEOUT,
        )
        resp.raise_for_status()
        embedding = resp.json().get("embedding")
        if embedding and len(embedding) == EMBED_DIMENSIONS:
            return embedding
        log.warning("Embedding dimension mismatch: got %d, expected %d",
                    len(embedding) if embedding else 0, EMBED_DIMENSIONS)
        return None
    except httpx.TimeoutException:
        log.warning("Ollama embedding timed out after %.1fs", EMBED_TIMEOUT)
        return None
    except Exception as e:
        log.warning("Ollama embedding failed: %s", e)
        return None


def _embed_query(text: str) -> list[float] | None:
    """Compute query embedding via Ollama. Uses 'search_query:' prefix."""
    if not text or not text.strip():
        return None
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": f"search_query: {text}"},
            timeout=EMBED_TIMEOUT,
        )
        resp.raise_for_status()
        embedding = resp.json().get("embedding")
        if embedding and len(embedding) == EMBED_DIMENSIONS:
            return embedding
        return None
    except Exception as e:
        log.warning("Ollama query embedding failed: %s", e)
        return None


def _set_embedding(conn, node_id: str, embedding: list[float]) -> None:
    """Store embedding vector on a node."""
    vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE nodes SET embedding = %s::vector WHERE id = %s",
            (vec_str, node_id),
        )
    conn.commit()


# --- MCP Tools ---

@mcp.tool()
def ensure_project(name: str, root_path: str = "") -> str:
    """Get or create a project by name. Returns the project ID.

    Use this to get a stable project ID before storing nodes.
    If the project already exists (by name), returns its existing ID.

    Args:
        name: Project name (e.g., "CodeStoragePoc", "my-feature-work")
        root_path: Optional filesystem path for the project root
    """
    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Check if exists
            cur.execute("SELECT id, name, root_path FROM projects WHERE name = %s", (name,))
            row = cur.fetchone()
            if row:
                return json.dumps({
                    "status": "existing",
                    "id": str(row["id"]),
                    "name": row["name"],
                    "root_path": row["root_path"],
                })

            # Create new
            project_id = uuid.uuid4()
            cur.execute(
                "INSERT INTO projects (id, name, root_path) VALUES (%s, %s, %s)",
                (str(project_id), name, root_path or name),
            )
            conn.commit()
            return json.dumps({
                "status": "created",
                "id": str(project_id),
                "name": name,
                "root_path": root_path or name,
            })
    finally:
        conn.close()


@mcp.tool()
def store_node(
    node_type: str,
    name: str,
    value: str = "",
    parent_id: str = "",
    project_name: str = "",
    project_id: str = "",
    attrs: str = "{}",
) -> str:
    """Store a node in the context graph. Returns the new node's ID.

    Automatically computes a semantic embedding via Ollama (nomic-embed-text, 768-dim).
    If Ollama is unavailable, the node is still stored without an embedding.

    This is how agents persist knowledge: decisions, findings, ideas,
    questions, action items, or any structured information.

    Args:
        node_type: Node type (e.g., decision, idea, question, finding, action_item,
                   conversation, turn, topic, plan, task, research)
        name: Short label for the node
        value: Full content/description (can be longer text)
        parent_id: Parent node ID (creates a tree structure). Empty for root nodes.
        project_name: Project name (used to look up project_id). Ignored if project_id is set.
        project_id: Direct project UUID. Takes precedence over project_name.
        attrs: JSON string of key-value attributes (e.g., '{"status": "open", "priority": "p1"}')
    """
    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Resolve project
            resolved_project_id = _resolve_project_id(cur, project_id, project_name)
            if not resolved_project_id:
                return json.dumps({"error": "No project found. Provide project_id or project_name, or call ensure_project first."})

            # Parse attributes
            try:
                parsed_attrs = json.loads(attrs) if isinstance(attrs, str) and attrs else {}
            except json.JSONDecodeError as e:
                return json.dumps({"error": f"Invalid attrs JSON: {e}"})

            # Determine sibling_order
            sibling_order = SIBLING_GAP
            if parent_id:
                sibling_order = _next_sibling_order(conn, parent_id)

            # Insert node
            node_id = uuid.uuid4()
            cur.execute(
                """INSERT INTO nodes (id, project_id, file_id, node_type, name, value,
                                     parent_id, sibling_order, modified_by)
                   VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s)""",
                (
                    str(node_id),
                    resolved_project_id,
                    node_type,
                    name,
                    value or None,
                    parent_id or None,
                    sibling_order,
                    "claude-code",
                ),
            )

            # Insert attributes
            for key, val in parsed_attrs.items():
                cur.execute(
                    """INSERT INTO node_attributes (id, node_id, key, value)
                       VALUES (gen_random_uuid(), %s, %s, %s)
                       ON CONFLICT (node_id, key) DO UPDATE SET value = %s""",
                    (str(node_id), key, str(val), str(val)),
                )

            conn.commit()

            # Auto-embed (after commit — node exists even if embedding fails)
            embed_text = f"{name} {value}".strip() if value else name
            embedding = _embed_text(embed_text)
            embedded = False
            if embedding:
                try:
                    _set_embedding(conn, str(node_id), embedding)
                    embedded = True
                except Exception as e:
                    log.warning("Failed to store embedding for %s: %s", node_id, e)

            return json.dumps({
                "id": str(node_id),
                "node_type": node_type,
                "name": name,
                "project_id": resolved_project_id,
                "parent_id": parent_id or None,
                "attribute_count": len(parsed_attrs),
                "embedded": embedded,
            })
    finally:
        conn.close()


@mcp.tool()
def query_nodes(text: str, node_types: str = "", project_name: str = "", limit: int = 10) -> str:
    """Search nodes by text match across name and value fields.

    Use this for exact/substring text search. For conceptual/meaning-based search,
    use semantic_search instead.

    Args:
        text: Search text (substring match, case-insensitive)
        node_types: Comma-separated node types to filter (e.g., "decision,idea,finding"). Empty for all.
        project_name: Filter to a specific project. Empty for all projects.
        limit: Max results (default 10)
    """
    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            search_pattern = f"%{text.replace('%', '').replace('_', '')}%"

            conditions = ["(n.name ILIKE %s OR n.value ILIKE %s)"]
            params = [search_pattern, search_pattern]

            # Filter by node types
            if node_types:
                types = [t.strip() for t in node_types.split(",") if t.strip()]
                if types:
                    placeholders = ",".join(["%s"] * len(types))
                    conditions.append(f"n.node_type IN ({placeholders})")
                    params.extend(types)

            # Filter by project
            if project_name:
                conditions.append("p.name = %s")
                params.append(project_name)

            params.append(limit)

            where = " AND ".join(conditions)
            cur.execute(
                f"""SELECT n.id, n.node_type, n.name, LEFT(n.value, 500) as value,
                           n.parent_id, n.created_at, n.modified_at,
                           p.name as project_name
                    FROM nodes n
                    JOIN projects p ON p.id = n.project_id
                    WHERE {where}
                    ORDER BY n.modified_at DESC
                    LIMIT %s""",
                params,
            )
            rows = cur.fetchall()

            # Get attributes for results
            results = []
            for row in rows:
                r = _serialize_row(row)
                cur.execute(
                    "SELECT key, value FROM node_attributes WHERE node_id = %s",
                    (row["id"],),
                )
                r["attributes"] = {a["key"]: a["value"] for a in cur.fetchall()}
                results.append(r)

            return json.dumps({"count": len(results), "results": results}, indent=2)
    finally:
        conn.close()


@mcp.tool()
def semantic_search(query: str, project_name: str = "", node_types: str = "", limit: int = 5) -> str:
    """Search nodes by semantic similarity using pgvector embeddings.

    Use this when you want meaning-based search (e.g., "how do I handle auth?"
    finds nodes about JWT tokens, login flows, etc.). For exact text matching,
    use query_nodes instead.

    Requires Ollama running with nomic-embed-text model.

    Args:
        query: Natural language search query
        project_name: Filter to a specific project. Empty for all projects.
        node_types: Comma-separated node types to filter (e.g., "decision,finding"). Empty for all.
        limit: Max results (default 5)
    """
    # Embed the query
    embedding = _embed_query(query)
    if not embedding:
        return json.dumps({"error": "Failed to compute query embedding. Is Ollama running with nomic-embed-text?"})

    vec_str = "[" + ",".join(str(v) for v in embedding) + "]"

    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Build filters
            conditions = ["n.embedding IS NOT NULL"]
            params = []

            if project_name:
                conditions.append("p.name = %s")
                params.append(project_name)

            if node_types:
                types = [t.strip() for t in node_types.split(",") if t.strip()]
                if types:
                    placeholders = ",".join(["%s"] * len(types))
                    conditions.append(f"n.node_type IN ({placeholders})")
                    params.extend(types)

            where = " AND ".join(conditions)

            # Semantic search via pgvector cosine distance
            # Param order matches SQL placeholder order: SELECT(vec), WHERE(filters), ORDER BY(vec), LIMIT
            cur.execute(
                f"""SELECT n.id, n.node_type, n.name, LEFT(n.value, 500) as value,
                           n.parent_id, n.modified_at,
                           n.embedding <=> %s::vector AS distance,
                           p.name as project_name
                    FROM nodes n
                    JOIN projects p ON p.id = n.project_id
                    WHERE {where}
                    ORDER BY n.embedding <=> %s::vector
                    LIMIT %s""",
                [vec_str] + params + [vec_str, limit],
            )
            rows = cur.fetchall()

            # Get attributes for results
            results = []
            for row in rows:
                r = _serialize_row(row)
                cur.execute(
                    "SELECT key, value FROM node_attributes WHERE node_id = %s",
                    (row["id"],),
                )
                r["attributes"] = {a["key"]: a["value"] for a in cur.fetchall()}
                results.append(r)

            # Coverage metric: how many nodes have embeddings vs total
            coverage_conditions = []
            coverage_params = []
            if project_name:
                coverage_conditions.append("p.name = %s")
                coverage_params.append(project_name)
            if node_types:
                types = [t.strip() for t in node_types.split(",") if t.strip()]
                if types:
                    placeholders = ",".join(["%s"] * len(types))
                    coverage_conditions.append(f"n.node_type IN ({placeholders})")
                    coverage_params.extend(types)

            coverage_where = ("WHERE " + " AND ".join(coverage_conditions)) if coverage_conditions else ""
            cur.execute(
                f"""SELECT
                        COUNT(*) FILTER (WHERE n.embedding IS NOT NULL) as embedded,
                        COUNT(*) as total
                    FROM nodes n
                    JOIN projects p ON p.id = n.project_id
                    {coverage_where}""",
                coverage_params,
            )
            cov = cur.fetchone()
            embedded_count = cov["embedded"]
            total_count = cov["total"]

            return json.dumps({
                "count": len(results),
                "results": results,
                "coverage": {
                    "embedded": embedded_count,
                    "total": total_count,
                    "pct": round(embedded_count / total_count * 100, 1) if total_count > 0 else 0,
                },
            }, indent=2)
    finally:
        conn.close()


@mcp.tool()
def get_node(node_id: str, include_children: bool = True) -> str:
    """Get a node with its attributes and optionally its direct children.

    Use this to inspect a specific node after finding it via query_nodes or list_recent.

    Args:
        node_id: The UUID of the node to fetch
        include_children: Whether to include direct children (default True)
    """
    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get node
            cur.execute(
                """SELECT n.*, p.name as project_name
                   FROM nodes n JOIN projects p ON p.id = n.project_id
                   WHERE n.id = %s""",
                (node_id,),
            )
            node = cur.fetchone()
            if not node:
                return json.dumps({"error": f"Node not found: {node_id}"})

            result = _serialize_row(node)

            # Get attributes
            cur.execute(
                "SELECT key, value FROM node_attributes WHERE node_id = %s",
                (node_id,),
            )
            result["attributes"] = {r["key"]: r["value"] for r in cur.fetchall()}

            # Get parent info
            if node["parent_id"]:
                cur.execute(
                    "SELECT id, node_type, name FROM nodes WHERE id = %s",
                    (str(node["parent_id"]),),
                )
                parent = cur.fetchone()
                if parent:
                    result["parent"] = _serialize_row(parent)

            # Get children
            if include_children:
                cur.execute(
                    """SELECT id, node_type, name, LEFT(value, 200) as value,
                              sibling_order, modified_at
                       FROM nodes WHERE parent_id = %s
                       ORDER BY sibling_order""",
                    (node_id,),
                )
                result["children"] = [_serialize_row(r) for r in cur.fetchall()]

            return json.dumps(result, indent=2)
    finally:
        conn.close()


@mcp.tool()
def get_children(node_id: str, type_filter: str = "") -> str:
    """Get direct children of a node, optionally filtered by type.

    Args:
        node_id: Parent node UUID
        type_filter: Optional node_type to filter by (e.g., "task", "idea")
    """
    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if type_filter:
                cur.execute(
                    """SELECT n.id, n.node_type, n.name, LEFT(n.value, 300) as value,
                              n.sibling_order, n.modified_at
                       FROM nodes n
                       WHERE n.parent_id = %s AND n.node_type = %s
                       ORDER BY n.sibling_order""",
                    (node_id, type_filter),
                )
            else:
                cur.execute(
                    """SELECT n.id, n.node_type, n.name, LEFT(n.value, 300) as value,
                              n.sibling_order, n.modified_at
                       FROM nodes n
                       WHERE n.parent_id = %s
                       ORDER BY n.sibling_order""",
                    (node_id,),
                )
            rows = cur.fetchall()

            # Batch-load attributes
            results = []
            for row in rows:
                r = _serialize_row(row)
                cur.execute(
                    "SELECT key, value FROM node_attributes WHERE node_id = %s",
                    (row["id"],),
                )
                r["attributes"] = {a["key"]: a["value"] for a in cur.fetchall()}
                results.append(r)

            return json.dumps({"parent_id": node_id, "count": len(results), "children": results}, indent=2)
    finally:
        conn.close()


@mcp.tool()
def list_projects() -> str:
    """List all projects in the context store with node counts.

    Use this to see what's in the database before querying or storing.
    """
    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT p.id, p.name, p.root_path, p.created_at,
                       COUNT(n.id) as node_count,
                       COUNT(n.id) FILTER (WHERE n.embedding IS NOT NULL) as embedded_count,
                       MAX(n.modified_at) as last_activity
                FROM projects p
                LEFT JOIN nodes n ON n.project_id = p.id
                GROUP BY p.id, p.name, p.root_path, p.created_at
                ORDER BY last_activity DESC NULLS LAST
            """)
            rows = cur.fetchall()
            return json.dumps({
                "count": len(rows),
                "projects": [_serialize_row(r) for r in rows],
            }, indent=2)
    finally:
        conn.close()


@mcp.tool()
def list_recent(node_types: str = "", project_name: str = "", limit: int = 20) -> str:
    """List recently modified nodes across the context store.

    Good for seeing what's been happening — recent decisions, new ideas,
    updated plans, etc.

    Args:
        node_types: Comma-separated types to filter (e.g., "decision,idea"). Empty for all.
        project_name: Filter to a specific project. Empty for all.
        limit: Max results (default 20)
    """
    conn = _get_db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params = []

            if node_types:
                types = [t.strip() for t in node_types.split(",") if t.strip()]
                if types:
                    placeholders = ",".join(["%s"] * len(types))
                    conditions.append(f"n.node_type IN ({placeholders})")
                    params.extend(types)

            if project_name:
                conditions.append("p.name = %s")
                params.append(project_name)

            params.append(limit)

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            cur.execute(
                f"""SELECT n.id, n.node_type, n.name, LEFT(n.value, 300) as value,
                           n.parent_id, n.modified_at, n.modified_by,
                           p.name as project_name
                    FROM nodes n
                    JOIN projects p ON p.id = n.project_id
                    {where}
                    ORDER BY n.modified_at DESC
                    LIMIT %s""",
                params,
            )
            rows = cur.fetchall()
            return json.dumps({
                "count": len(rows),
                "nodes": [_serialize_row(r) for r in rows],
            }, indent=2)
    finally:
        conn.close()


@mcp.tool()
def history(node_id: str) -> str:
    """Get temporal edge history for a node from the AGE graph.

    Shows all graph relationships (incoming and outgoing) for a node,
    including temporal properties (valid_from, valid_to, provenance).
    Useful for seeing how a node's relationships evolved over time.

    Args:
        node_id: The UUID of the node to query history for
    """
    conn = _get_db()
    try:
        with conn.cursor() as cur:
            # Load AGE extension and set search path
            try:
                cur.execute("LOAD 'age';")
                cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            except Exception as e:
                return json.dumps({"error": f"AGE extension not available. Run init.sql to set up the graph. Detail: {e}"})

            # Query outgoing edges
            edges = []
            try:
                cur.execute(f"""
                    SELECT * FROM cypher('{AGE_GRAPH}', $$
                        MATCH (a)-[e]->(b)
                        WHERE a.node_id = '{_escape_cypher(node_id)}'
                        RETURN type(e), e.valid_from, e.valid_to, e.provenance,
                               b.node_id, b.name
                    $$) AS (edge_type agtype, valid_from agtype, valid_to agtype,
                            provenance agtype, other_id agtype, other_name agtype);
                """)
                for row in cur.fetchall():
                    edges.append({
                        "direction": "outgoing",
                        "edge_type": _parse_agtype(row[0]),
                        "valid_from": _parse_agtype(row[1]),
                        "valid_to": _parse_agtype(row[2]),
                        "provenance": _parse_agtype(row[3]),
                        "other_node_id": _parse_agtype(row[4]),
                        "other_node_name": _parse_agtype(row[5]),
                    })
            except Exception as e:
                log.warning("Outgoing edge query failed: %s", e)
                conn.rollback()
                # Reload AGE after rollback
                cur.execute("LOAD 'age';")
                cur.execute("SET search_path = ag_catalog, \"$user\", public;")

            # Query incoming edges
            try:
                cur.execute(f"""
                    SELECT * FROM cypher('{AGE_GRAPH}', $$
                        MATCH (a)-[e]->(b)
                        WHERE b.node_id = '{_escape_cypher(node_id)}'
                        RETURN type(e), e.valid_from, e.valid_to, e.provenance,
                               a.node_id, a.name
                    $$) AS (edge_type agtype, valid_from agtype, valid_to agtype,
                            provenance agtype, other_id agtype, other_name agtype);
                """)
                for row in cur.fetchall():
                    edges.append({
                        "direction": "incoming",
                        "edge_type": _parse_agtype(row[0]),
                        "valid_from": _parse_agtype(row[1]),
                        "valid_to": _parse_agtype(row[2]),
                        "provenance": _parse_agtype(row[3]),
                        "other_node_id": _parse_agtype(row[4]),
                        "other_node_name": _parse_agtype(row[5]),
                    })
            except Exception as e:
                log.warning("Incoming edge query failed: %s", e)

            # Sort by valid_from chronologically (nulls last)
            edges.sort(key=lambda e: e.get("valid_from") or "9999")

            return json.dumps({
                "node_id": node_id,
                "edge_count": len(edges),
                "edges": edges,
            }, indent=2)
    finally:
        conn.close()


# --- Internal helpers ---

def _resolve_project_id(cur, project_id: str, project_name: str) -> str | None:
    """Resolve a project ID from either direct ID or name lookup."""
    if project_id:
        return project_id
    if project_name:
        cur.execute("SELECT id FROM projects WHERE name = %s", (project_name,))
        row = cur.fetchone()
        if row:
            return str(row["id"])
    return None


def _escape_cypher(value: str) -> str:
    """Escape a string for use in Cypher queries. Prevents injection."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _parse_agtype(val) -> str | None:
    """Parse an AGE agtype value to a Python string."""
    if val is None:
        return None
    s = str(val)
    # AGE wraps strings in quotes: "\"value\""
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


if __name__ == "__main__":
    if _PORT:
        mcp.run(transport="sse")
    else:
        mcp.run()
