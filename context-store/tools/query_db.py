#!/usr/bin/env python3
#  CodeStoragePoc - DB Context Query CLI
#
#  Standalone script for querying the node database from any context:
#  bash pipes, CLI model prompts, MCP tools, or direct invocation.
#
#  Usage:
#    python tools/query_db.py "search term"
#    python tools/query_db.py "CQRS" --types decision,idea --limit 3
#    python tools/query_db.py "event" --since 2h
#    python tools/query_db.py --conversation <uuid>        # get thread
#    python tools/query_db.py --recent 10                  # last N nodes
#    python tools/query_db.py --store "user" "message"     # store a turn
#
#  Depends on: psycopg2
#  Used by:    bash pipes to gemini/codex/copilot, Claude Code, scripts

import argparse
import json
import sys
import re
from datetime import datetime, timedelta

DB_CONFIG = {
    "host": "localhost",
    "port": 5433,
    "dbname": "code_storage",
    "user": "postgres",
    "password": "postgres",
}


def get_db():
    import psycopg2
    return psycopg2.connect(**DB_CONFIG, connect_timeout=5)


def search(query, types=None, since=None, limit=5, conversation=None):
    """Text search across nodes with optional filters."""
    conn = get_db()
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
        if types:
            type_list = [t.strip() for t in types.split(",")]
            placeholders = ",".join(["%s"] * len(type_list))
            conditions.append(f"n.node_type IN ({placeholders})")
            params.extend(type_list)

        # Time filter
        if since:
            delta = parse_duration(since)
            if delta:
                conditions.append("n.modified_at >= %s")
                params.append(datetime.now() - delta)

        # Conversation scope
        if conversation:
            conditions.append("(n.parent_id = %s OR n.id = %s)")
            params.extend([conversation, conversation])

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        cur.execute(
            f"""
            SELECT n.id, n.node_type, n.name, LEFT(n.value, 300) as value,
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
        return results
    finally:
        conn.close()


def recent(limit=10):
    """Get the N most recently modified nodes."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT n.id, n.node_type, n.name, LEFT(n.value, 300) as value,
                   p.node_type as parent_type, p.name as parent_name,
                   n.modified_at
            FROM nodes n
            LEFT JOIN nodes p ON n.parent_id = p.id
            ORDER BY n.modified_at DESC
            LIMIT %s
            """,
            (limit,),
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
        return results
    finally:
        conn.close()


def store(role, content, conversation_id=None):
    """Store a conversation turn. Returns conversation_id and turn_id."""
    import uuid
    conn = get_db()
    cur = conn.cursor()
    try:
        # Find or create project
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
                 f"CLI Session {datetime.now().isoformat()[:19]}", 0),
            )

        # Next sibling order
        cur.execute(
            "SELECT COALESCE(MAX(sibling_order), 0) + 100 FROM nodes WHERE parent_id = %s",
            (conv_id,),
        )
        next_order = cur.fetchone()[0]

        # Insert turn
        turn_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO nodes (id, project_id, node_type, value, parent_id, sibling_order, modified_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (turn_id, project_id, "turn", content, conv_id, next_order, "cli"),
        )

        for key, val in [("speaker", role), ("input_mode", "text"), ("target", "user")]:
            cur.execute(
                "INSERT INTO node_attributes (id, node_id, key, value) "
                "VALUES (gen_random_uuid(), %s, %s, %s)",
                (turn_id, key, val),
            )

        conn.commit()
        return {"conversation_id": conv_id, "turn_id": turn_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_duration(s):
    """Parse '2h', '30m', '1d' into a timedelta."""
    match = re.match(r"^(\d+)([mhd])$", s.strip())
    if not match:
        return None
    val, unit = int(match.group(1)), match.group(2)
    if unit == "m":
        return timedelta(minutes=val)
    elif unit == "h":
        return timedelta(hours=val)
    elif unit == "d":
        return timedelta(days=val)
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Query the CodeStoragePoc node database",
        epilog="Examples:\n"
               "  python tools/query_db.py 'CQRS' --types decision,idea\n"
               "  python tools/query_db.py 'event' --since 2h --limit 3\n"
               "  python tools/query_db.py --recent 10\n"
               "  python tools/query_db.py --store user 'What about CQRS?'\n"
               "  python tools/query_db.py --conversation <uuid>",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", nargs="?", default="", help="Text to search for")
    parser.add_argument("--types", "-t", help="Comma-separated node types (decision,idea,turn,...)")
    parser.add_argument("--since", "-s", help="Time window: 30m, 2h, 1d")
    parser.add_argument("--limit", "-n", type=int, default=5, help="Max results (default 5)")
    parser.add_argument("--conversation", "-c", help="Scope to a conversation UUID")
    parser.add_argument("--recent", "-r", type=int, metavar="N", help="Show N most recent nodes")
    parser.add_argument("--store", nargs=2, metavar=("ROLE", "CONTENT"), help="Store a turn: --store user 'message'")
    parser.add_argument("--compact", action="store_true", help="One-line-per-result output for piping")

    args = parser.parse_args()

    try:
        if args.store:
            result = store(args.store[0], args.store[1], args.conversation)
            print(json.dumps(result, indent=2))
        elif args.recent:
            results = recent(args.recent)
            _output(results, args.compact)
        elif args.query or args.types or args.since or args.conversation:
            results = search(
                args.query, args.types, args.since, args.limit, args.conversation
            )
            _output(results, args.compact)
        else:
            parser.print_help()
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)


def _output(results, compact):
    if compact:
        for r in results:
            name = r["name"] or (r["value"][:80] if r["value"] else "(empty)")
            print(f"[{r['type']}] {name}")
    else:
        print(json.dumps({"count": len(results), "results": results}, indent=2))


if __name__ == "__main__":
    main()
