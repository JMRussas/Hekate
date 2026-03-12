#!/usr/bin/env python3
"""Backfill embeddings on existing nodes via Ollama (nomic-embed-text).

Idempotent: only processes nodes where embedding IS NULL.
Safe to re-run after interruption — already-embedded nodes are skipped.

Usage:
    python tools/backfill_embeddings.py              # embed all unembedded nodes
    python tools/backfill_embeddings.py --dry-run    # show count, don't modify
    python tools/backfill_embeddings.py --force      # re-embed ALL nodes (model version change)

Requires: Ollama running on localhost:11434 with nomic-embed-text pulled.
"""

import argparse
import os
import sys
import time

import httpx
import psycopg2
import psycopg2.extras

DB_CONFIG = {
    "host": os.environ.get("CODESTORAGE_DB_HOST", "localhost"),
    "port": int(os.environ.get("CODESTORAGE_DB_PORT", "5433")),
    "dbname": os.environ.get("CODESTORAGE_DB_NAME", "code_storage"),
    "user": os.environ.get("CODESTORAGE_DB_USER", "postgres"),
    "password": os.environ.get("CODESTORAGE_DB_PASSWORD", "postgres"),
}

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
EXPECTED_DIMENSIONS = 768
BATCH_SIZE = 10
BATCH_DELAY_S = 0.1  # 100ms between batches


def check_vector_dimension(conn) -> int | None:
    """Check the vector column dimension. Returns dimension or None if no column."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT atttypmod FROM pg_attribute
            WHERE attrelid = 'nodes'::regclass AND attname = 'embedding'
        """)
        row = cur.fetchone()
        if not row:
            return None
        return row[0]


def embed_text(text: str) -> list[float] | None:
    """Compute embedding via Ollama with search_document prefix."""
    try:
        resp = httpx.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": f"search_document: {text}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        embedding = resp.json().get("embedding")
        if embedding and len(embedding) == EXPECTED_DIMENSIONS:
            return embedding
        print(f"  WARNING: got {len(embedding) if embedding else 0} dims, expected {EXPECTED_DIMENSIONS}")
        return None
    except httpx.TimeoutException:
        print("  WARNING: Ollama timed out")
        return None
    except httpx.ConnectError:
        print("  ERROR: Cannot connect to Ollama. Is it running?")
        print(f"  Run: ollama pull {EMBED_MODEL}")
        return None
    except Exception as e:
        print(f"  WARNING: Embedding failed: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Backfill node embeddings via Ollama")
    parser.add_argument("--dry-run", action="store_true", help="Show count without modifying")
    parser.add_argument("--force", action="store_true", help="Re-embed ALL nodes (ignore existing embeddings)")
    args = parser.parse_args()

    conn = psycopg2.connect(**DB_CONFIG, connect_timeout=5)

    # Dimension pre-flight
    dim = check_vector_dimension(conn)
    if dim is None:
        print("ERROR: No 'embedding' column on nodes table. Run Schema.cs migration first.")
        sys.exit(1)
    if dim != EXPECTED_DIMENSIONS:
        print(f"ERROR: Vector dimension mismatch. Column is vector({dim}), expected vector({EXPECTED_DIMENSIONS}).")
        print("A schema migration is needed before backfill.")
        sys.exit(1)
    print(f"Pre-flight OK: embedding column is vector({dim})")

    # Test Ollama connectivity
    test_embed = embed_text("test")
    if test_embed is None:
        print("ERROR: Ollama embedding test failed. Aborting.")
        sys.exit(1)
    print(f"Ollama OK: {EMBED_MODEL} returning {len(test_embed)}-dim vectors")

    # Find nodes to embed
    where_clause = "(name IS NOT NULL OR value IS NOT NULL)"
    if not args.force:
        where_clause += " AND embedding IS NULL"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT count(*) as cnt FROM nodes WHERE {where_clause}")
        total = cur.fetchone()["cnt"]

    if total == 0:
        print("No nodes to embed. All nodes already have embeddings.")
        sys.exit(0)

    print(f"Nodes to embed: {total}" + (" (force mode)" if args.force else ""))

    if args.dry_run:
        print("Dry run — no changes made.")
        sys.exit(0)

    # Process in batches
    embedded = 0
    failed = 0
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT id, name, value FROM nodes WHERE {where_clause} ORDER BY created_at"
        )
        batch = []
        for row in cur:
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                e, f = _process_batch(conn, batch)
                embedded += e
                failed += f
                print(f"  Embedded {embedded}/{total} nodes ({round(embedded/total*100, 1)}%)"
                      + (f" [{failed} failed]" if failed else ""))
                batch = []
                time.sleep(BATCH_DELAY_S)

        # Remaining batch
        if batch:
            e, f = _process_batch(conn, batch)
            embedded += e
            failed += f

    print(f"\nDone. Embedded {embedded}/{total} nodes." + (f" {failed} failed." if failed else ""))


def _process_batch(conn, batch: list[dict]) -> tuple[int, int]:
    """Process a batch of nodes. Returns (embedded_count, failed_count)."""
    embedded = 0
    failed = 0
    for row in batch:
        text = f"{row['name'] or ''} {row['value'] or ''}".strip()
        if not text:
            continue
        embedding = embed_text(text)
        if embedding:
            vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
            with conn.cursor() as ucur:
                ucur.execute(
                    "UPDATE nodes SET embedding = %s::vector WHERE id = %s",
                    (vec_str, str(row["id"])),
                )
            conn.commit()
            embedded += 1
        else:
            failed += 1
    return embedded, failed


if __name__ == "__main__":
    main()
