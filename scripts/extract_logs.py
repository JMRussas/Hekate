"""Extract daily metrics from engine/pipeline logs into the orchestration DB.

Parses log files using Odin/gods/log_extractor.py, writes summary rows to
the daily_metrics table, and posts a metric_report node to the context store.

Usage:
    python scripts/extract_logs.py                  # today, default log dir
    python scripts/extract_logs.py --date 2026-03-31
    python scripts/extract_logs.py --log-dir /path/to/logs
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

# Ensure repo root is importable
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "Odin"))

from gods.log_extractor import parse_log, daily_summary  # noqa: E402

# ---------------------------------------------------------------------------
# DB helpers — dual-backend (Postgres via ORCHESTRATION_DSN, else SQLite)
# ---------------------------------------------------------------------------

_DEFAULT_SQLITE = _REPO_ROOT / "orchestration" / "data" / "orchestration.db"
_PG_PARAM_RE = re.compile(r"\$(\d+)")


def _get_connection():
    """Return (conn, is_postgres) using the same logic as orchestration."""
    dsn = os.environ.get("ORCHESTRATION_DSN", "")
    if dsn.startswith("postgresql://") or dsn.startswith("postgres://"):
        import psycopg2
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        return conn, True

    db_path = str(_DEFAULT_SQLITE)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn, False


def _exec(conn, sql: str, params: tuple, *, is_postgres: bool):
    """Execute SQL, translating $N placeholders to ? for SQLite."""
    if not is_postgres:
        sql = _PG_PARAM_RE.sub("?", sql)
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


# ---------------------------------------------------------------------------
# Ensure daily_metrics table exists (idempotent)
# ---------------------------------------------------------------------------

_CREATE_TABLE_PG = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    id BIGSERIAL PRIMARY KEY,
    date TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value DOUBLE PRECISION,
    details_json JSONB,
    extracted_at DOUBLE PRECISION
)
"""

_CREATE_TABLE_SQLITE = """
CREATE TABLE IF NOT EXISTS daily_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL,
    details_json TEXT,
    extracted_at REAL
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS ix_daily_metrics_date_metric
    ON daily_metrics (date, metric_name)
"""


def _ensure_table(conn, *, is_postgres: bool):
    cur = conn.cursor()
    cur.execute(_CREATE_TABLE_PG if is_postgres else _CREATE_TABLE_SQLITE)
    cur.execute(_CREATE_INDEX)
    conn.commit()


# ---------------------------------------------------------------------------
# Context store helpers
# ---------------------------------------------------------------------------

CONTEXT_STORE_URL = "http://localhost:5102"


def _post_metric_report(day: date, summary: dict):
    """Create a metric_report node in the context store."""
    try:
        import httpx
    except ImportError:
        print("  [warn] httpx not installed — skipping context store post")
        return

    # Ensure a project for platform metrics
    project_id = "hekate-platform-metrics"
    try:
        httpx.post(
            f"{CONTEXT_STORE_URL}/api/projects",
            json={"id": project_id, "name": "Hekate Platform Metrics"},
            timeout=10,
        )
    except Exception:
        pass  # may already exist or context store is down

    node_id = f"metric-report-{day.isoformat()}"
    node = {
        "id": node_id,
        "type": "metric_report",
        "name": f"Daily metrics {day.isoformat()}",
        "attributes": summary,
    }

    try:
        resp = httpx.post(
            f"{CONTEXT_STORE_URL}/api/node/{project_id}/children",
            json=node,
            timeout=10,
        )
        if resp.status_code < 300:
            print(f"  Context store: created node {node_id}")
        else:
            print(f"  Context store: {resp.status_code} — {resp.text[:200]}")
    except Exception as exc:
        print(f"  Context store: unavailable ({exc})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(target_date: date, log_dir: Path):
    print(f"Extracting logs for {target_date.isoformat()} from {log_dir}")

    # Collect metrics from all available log files
    combined_metrics = None
    log_files = [
        log_dir / "engine.log",
        _REPO_ROOT / "Odin" / "pipeline.log",
    ]

    for log_file in log_files:
        if not log_file.exists():
            print(f"  [skip] {log_file} not found")
            continue
        print(f"  Parsing {log_file} ...")
        metrics = parse_log(str(log_file))
        if combined_metrics is None:
            combined_metrics = metrics
        else:
            # Merge: sum scalars, concat lists
            for k, v in metrics.items():
                if isinstance(v, list):
                    combined_metrics[k].extend(v)
                elif isinstance(v, (int, float)):
                    combined_metrics[k] += v

    if combined_metrics is None:
        print("  No log files found — nothing to extract")
        return

    # Build summary rows
    rows = daily_summary(combined_metrics, target_date)
    print(f"  {len(rows)} metric rows to write")

    # DB: idempotent upsert
    conn, is_pg = _get_connection()
    _ensure_table(conn, is_postgres=is_pg)

    try:
        _exec(conn, "DELETE FROM daily_metrics WHERE date = $1",
              (target_date.isoformat(),), is_postgres=is_pg)

        now = time.time()
        for metric_name, metric_value, details_json in rows:
            _exec(
                conn,
                "INSERT INTO daily_metrics (date, metric_name, metric_value, details_json, extracted_at) "
                "VALUES ($1, $2, $3, $4, $5)",
                (target_date.isoformat(), metric_name, metric_value, details_json, now),
                is_postgres=is_pg,
            )

        conn.commit()
        print(f"  DB: wrote {len(rows)} rows for {target_date.isoformat()}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Context store: post metric report node
    total_tasks = combined_metrics.get("cli_launch_count", 0)
    completed = combined_metrics.get("cli_complete_count", 0)
    success_rate = (completed / total_tasks * 100) if total_tasks > 0 else 0.0

    top_errors = [
        e["message"][:120] for e in combined_metrics.get("errors", [])[:5]
    ]

    report = {
        "date": target_date.isoformat(),
        "total_tasks": total_tasks,
        "success_rate": round(success_rate, 1),
        "avg_cost": round(
            combined_metrics["total_cost"] / max(total_tasks, 1), 4
        ),
        "top_errors": top_errors,
    }
    _post_metric_report(target_date, report)

    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Extract daily metrics from engine logs")
    parser.add_argument(
        "--date",
        type=lambda s: date.fromisoformat(s),
        default=date.today(),
        help="Date to extract (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path("D:/Hekate/logs"),
        help="Directory containing engine.log (default: D:/Hekate/logs/)",
    )
    args = parser.parse_args()
    run(args.date, args.log_dir)


if __name__ == "__main__":
    main()
