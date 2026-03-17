"""
Migrate all data from SQLite orchestration.db to Postgres.

Usage:
    python tools/migrate_to_postgres.py

Safe to re-run — uses ON CONFLICT DO NOTHING.
"""

import asyncio
import sqlite3
import sys
import os

# Resolve paths relative to script location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ORCH_DIR = os.path.dirname(SCRIPT_DIR)
SQLITE_PATH = os.path.join(ORCH_DIR, "data", "orchestration.db")
POSTGRES_DSN = "postgresql://postgres:postgres@localhost:5433/orchestration"

# Tables in dependency order (parents before children).
# Tuples of (table_name, list_of_columns, identity_columns).
# identity_columns: columns that are GENERATED ALWAYS AS IDENTITY in Postgres
# and need OVERRIDING SYSTEM VALUE.
TABLES = [
    ("users", None, []),
    ("projects", None, []),
    ("plans", None, []),
    ("tasks", None, []),
    ("task_deps", None, []),
    ("usage_log", None, ["id"]),
    ("budget_periods", None, []),
    ("task_events", None, ["id"]),
    ("checkpoints", None, []),
    ("user_identities", None, []),
    ("project_knowledge", None, []),
    ("refresh_token_families", None, []),
    ("api_keys", None, []),
    ("sentinel_observations", None, []),
    ("sentinel_decisions", None, []),
]


def read_sqlite(path: str):
    """Read all tables from SQLite, return dict of table -> (columns, rows)."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    result = {}
    for table_name, _, identity_cols in TABLES:
        cur.execute(f"PRAGMA table_info({table_name})")
        columns = [row[1] for row in cur.fetchall()]

        cur.execute(f"SELECT * FROM {table_name}")
        rows = [tuple(r) for r in cur.fetchall()]

        result[table_name] = (columns, rows, identity_cols)

    conn.close()
    return result


async def write_postgres(dsn: str, data: dict):
    """Write all tables to Postgres."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        for table_name, _, _ in TABLES:
            columns, rows, identity_cols = data[table_name]
            if not rows:
                print(f"  {table_name}: 0 rows (empty, skipped)")
                continue

            col_list = ", ".join(columns)
            placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))

            # Determine conflict target: use primary key columns from Postgres
            # For task_deps (composite key), we need special handling
            pk_cols = await _get_pk_columns(conn, table_name)
            if pk_cols:
                conflict_clause = f"ON CONFLICT ({', '.join(pk_cols)}) DO NOTHING"
            else:
                conflict_clause = "ON CONFLICT DO NOTHING"

            # OVERRIDING SYSTEM VALUE needed for GENERATED ALWAYS AS IDENTITY cols
            overriding = "OVERRIDING SYSTEM VALUE" if identity_cols else ""

            sql = f"INSERT INTO {table_name} ({col_list}) {overriding} VALUES ({placeholders}) {conflict_clause}"

            # Insert in batches
            inserted = 0
            batch_size = 500
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                # Use executemany for speed
                for row in batch:
                    try:
                        result = await conn.execute(sql, *row)
                        if "INSERT 0 1" in result:
                            inserted += 1
                    except Exception as e:
                        print(f"    ERROR on {table_name} row: {e}")
                        print(f"    Row data: {row[:3]}...")

            print(f"  {table_name}: {inserted}/{len(rows)} rows inserted")

        # Reset sequences for identity columns so future inserts get correct IDs
        for table_name, _, _ in TABLES:
            _, rows, identity_cols = data[table_name]
            for col in identity_cols:
                if rows:
                    max_id = await conn.fetchval(
                        f"SELECT COALESCE(MAX({col}), 0) FROM {table_name}"
                    )
                    await conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table_name}', '{col}'), $1)",
                        max_id,
                    )
                    print(f"  {table_name}.{col} sequence reset to {max_id}")

    finally:
        await conn.close()


async def _get_pk_columns(conn, table_name: str) -> list[str]:
    """Get primary key columns for a table from Postgres catalog."""
    rows = await conn.fetch("""
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = $1::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
    """, table_name)
    return [r["attname"] for r in rows]


async def main():
    print(f"SQLite: {SQLITE_PATH}")
    print(f"Postgres: {POSTGRES_DSN}")
    print()

    if not os.path.exists(SQLITE_PATH):
        print(f"ERROR: SQLite database not found at {SQLITE_PATH}")
        sys.exit(1)

    print("Reading SQLite...")
    data = read_sqlite(SQLITE_PATH)

    total = sum(len(v[1]) for v in data.values())
    print(f"  Total rows to migrate: {total}")
    print()

    print("Writing to Postgres...")
    await write_postgres(POSTGRES_DSN, data)

    print()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
