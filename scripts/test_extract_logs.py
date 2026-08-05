"""Test extract_logs.py idempotency — running twice produces identical rows (no duplicates).

Run with:
  pytest scripts/test_extract_logs.py -v
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import textwrap
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_LOG = textwrap.dedent("""\
    10:00:01 [gods.pipeline] INFO Pipeline: restored cursor to 47
    10:00:05 [gods.hermes] INFO Hermes: launched abc123 via claude_code
    10:00:10 [gods.hermes] INFO Hermes: task abc123 completed in 32.5s (cost=$0.0234)
    10:01:00 [gods.odin] INFO Odin: unblocked 3 task(s) for project proj-1
    10:01:05 [gods.athena] INFO L1 plan passed rule check (12 tasks)
    10:01:10 [gods.mimir] INFO Mimir: submitted passed verdict for task abc123
    10:02:00 [gods.tyche] INFO Tyche: task abc123 cost $0.0500
    10:02:05 [gods.gateway] INFO Claude: 1500 chars in 1.2s
    10:02:06 [gods.gateway] INFO Claude: calling
    10:05:00 [gods.hermes] ERROR Hermes: task def456 failed with exit code 1
""")

TARGET_DATE = date(2026, 3, 31)


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    """Write a sample engine.log into a temp directory."""
    (tmp_path / "engine.log").write_text(SAMPLE_LOG, encoding="utf-8")
    return tmp_path


@pytest.fixture
def sqlite_db(tmp_path: Path):
    """Override extract_logs to use a temp SQLite DB."""
    db_path = tmp_path / "test.db"
    original_get_conn = None

    import scripts.extract_logs as mod
    original_get_conn = mod._get_connection

    def _mock_conn():
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn, False

    with patch.object(mod, "_get_connection", _mock_conn):
        yield db_path, mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_idempotent_no_duplicates(log_dir: Path, sqlite_db):
    """Running extract twice for the same date should produce identical row count."""
    db_path, mod = sqlite_db

    with patch.object(mod, "_post_metric_report"):
        mod.run(TARGET_DATE, log_dir)
        mod.run(TARGET_DATE, log_dir)

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT date, metric_name, metric_value FROM daily_metrics WHERE date = ?",
        (TARGET_DATE.isoformat(),),
    ).fetchall()
    conn.close()

    # Count occurrences of each metric_name — should all be 1
    names = [r[1] for r in rows]
    for name in set(names):
        count = names.count(name)
        assert count == 1, f"Metric '{name}' appears {count} times — expected 1 (idempotency broken)"


def test_rows_identical_after_second_run(log_dir: Path, sqlite_db):
    """Values from the second run should match the first."""
    db_path, mod = sqlite_db

    with patch.object(mod, "_post_metric_report"):
        mod.run(TARGET_DATE, log_dir)

    conn = sqlite3.connect(str(db_path))
    first_run = conn.execute(
        "SELECT metric_name, metric_value, details_json FROM daily_metrics "
        "WHERE date = ? ORDER BY metric_name",
        (TARGET_DATE.isoformat(),),
    ).fetchall()
    conn.close()

    with patch.object(mod, "_post_metric_report"):
        mod.run(TARGET_DATE, log_dir)

    conn = sqlite3.connect(str(db_path))
    second_run = conn.execute(
        "SELECT metric_name, metric_value, details_json FROM daily_metrics "
        "WHERE date = ? ORDER BY metric_name",
        (TARGET_DATE.isoformat(),),
    ).fetchall()
    conn.close()

    assert len(first_run) == len(second_run), "Row count changed between runs"
    for (n1, v1, d1), (n2, v2, d2) in zip(first_run, second_run):
        assert n1 == n2, f"Metric name mismatch: {n1} vs {n2}"
        assert v1 == pytest.approx(v2), f"Value mismatch for {n1}: {v1} vs {v2}"


def test_date_flag_isolates_dates(log_dir: Path, sqlite_db):
    """Extracting for two different dates should not interfere."""
    db_path, mod = sqlite_db
    other_date = date(2026, 4, 1)

    with patch.object(mod, "_post_metric_report"):
        mod.run(TARGET_DATE, log_dir)
        mod.run(other_date, log_dir)

    conn = sqlite3.connect(str(db_path))
    count_target = conn.execute(
        "SELECT COUNT(*) FROM daily_metrics WHERE date = ?",
        (TARGET_DATE.isoformat(),),
    ).fetchone()[0]
    count_other = conn.execute(
        "SELECT COUNT(*) FROM daily_metrics WHERE date = ?",
        (other_date.isoformat(),),
    ).fetchone()[0]
    conn.close()

    assert count_target > 0, "Target date should have rows"
    assert count_other > 0, "Other date should have rows"
    assert count_target == count_other, "Same log data should produce same number of metrics"


def test_context_store_post_is_mocked(log_dir: Path, sqlite_db):
    """Verify the context store HTTP call is mocked (not hitting real service)."""
    _, mod = sqlite_db
    mock_post = MagicMock()

    with patch.object(mod, "_post_metric_report", mock_post):
        mod.run(TARGET_DATE, log_dir)

    mock_post.assert_called_once()
    args = mock_post.call_args
    assert args[0][0] == TARGET_DATE
    assert isinstance(args[0][1], dict)
    assert "total_tasks" in args[0][1]


def test_expected_metrics_present(log_dir: Path, sqlite_db):
    """Verify the sample log produces the expected metric names."""
    db_path, mod = sqlite_db

    with patch.object(mod, "_post_metric_report"):
        mod.run(TARGET_DATE, log_dir)

    conn = sqlite3.connect(str(db_path))
    names = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT metric_name FROM daily_metrics WHERE date = ?",
            (TARGET_DATE.isoformat(),),
        ).fetchall()
    }
    conn.close()

    expected = {
        "error_count",
        "cli_launch_count",
        "cli_complete_count",
        "cli_timeout_count",
        "cli_fail_count",
        "tasks_unblocked",
        "plan_success_count",
        "plan_fail_count",
        "verify_passed",
        "total_cost",
        "hermes_task_duration_avg",
        "hermes_task_duration_p95",
        "gateway_call_count",
        "gateway_latency_claude_avg",
    }
    missing = expected - names
    assert not missing, f"Missing metrics: {missing}"


def test_no_log_files_noop(tmp_path: Path, sqlite_db):
    """When no log files exist, nothing is written."""
    db_path, mod = sqlite_db
    empty_dir = tmp_path / "empty_logs"
    empty_dir.mkdir()

    # Patch _REPO_ROOT so pipeline.log lookup also points to the empty dir
    with patch.object(mod, "_REPO_ROOT", empty_dir), \
         patch.object(mod, "_post_metric_report"):
        mod.run(TARGET_DATE, empty_dir)

    conn = sqlite3.connect(str(db_path))
    # Table may not even be created if run() returns early — that's fine
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_metrics'"
    ).fetchall()
    if tables:
        count = conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0]
    else:
        count = 0
    conn.close()

    assert count == 0, "No rows should be written when no log files exist"
