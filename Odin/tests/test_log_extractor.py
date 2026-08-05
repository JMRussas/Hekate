"""Test log_extractor.parse_log against real log patterns.

Run with:
  cd Odin && pytest tests/test_log_extractor.py -v
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, date

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gods.log_extractor import parse_log, daily_summary


# -- Fixtures ----------------------------------------------------------------

REPRESENTATIVE_LOG = """\
21:46:02 [gods.pipeline] INFO Pipeline: restored cursor to 47
21:46:03 [gods.handlers.hermes] INFO Hermes: launched abc12345 via claude_code
21:46:05 [gods.handlers.athena] INFO L1 plan passed rule check (12 tasks)
21:46:10 [llm_gateway] INFO Claude: calling
21:46:12 [llm_gateway] INFO Claude: 5678 chars in 1.1s
21:46:15 [llm_gateway] INFO Gemini: calling
21:46:17 [llm_gateway] INFO Gemini: 1234 chars in 2.3s
21:47:00 [gods.handlers.hermes] INFO Hermes: task abc12345 completed in 45.2s (cost=$0.0234)
21:47:01 [gods.handlers.odin] INFO Odin: unblocked 3 task(s) for project proj0001
21:47:02 [gods.handlers.mimir] INFO Mimir: submitted passed verdict for task abc12345 via /verify API
21:47:03 [gods.handlers.mimir] INFO Mimir: submitted gaps_found verdict for task def67890 via /verify API
21:47:04 [gods.handlers.mimir] INFO Mimir: submitted human_needed verdict for task ghi11111 via /verify API
21:47:05 [gods.handlers.tyche] INFO Tyche: task abc12345 cost $0.0234
21:47:06 [gods.handlers.tyche] INFO Tyche: task def67890 cost $0.1500
21:47:10 [gods.handlers.hermes] INFO Hermes: launched def67890 via gemini_cli
21:47:20 [gods.handlers.hermes] INFO Hermes: task def67890 completed in 10.0s (cost=$0.15)
21:47:30 [gods.handlers.hermes] WARNING Hermes: task ghi11111 timed out after 300s
21:47:31 [gods.handlers.hermes] ERROR Hermes: task jkl22222 failed with exit code 1
  Traceback (most recent call last):
    File "executor.py", line 42, in run
      subprocess.check_call(cmd)
  subprocess.CalledProcessError: exit code 1
21:47:40 [gods.handlers.athena] ERROR Athena leveled: planning failed for proj0002: context too large
21:47:45 [gods.pipeline] WARNING Provider availability check failed for gemini_cli
21:47:50 [gods.handlers.odin] INFO Odin: unblocked 2 task(s) for project proj0001
21:48:00 [gods.pipeline] CRITICAL Pipeline fatal: database locked
  Traceback (most recent call last):
    File "pipeline.py", line 100, in tick
      db.execute(sql)
  sqlite3.OperationalError: database is locked
"""


@pytest.fixture
def log_file(tmp_path):
    """Write representative log to a temp file and return path."""
    p = tmp_path / "pipeline.log"
    p.write_text(REPRESENTATIVE_LOG, encoding="utf-8")
    return str(p)


@pytest.fixture
def empty_log(tmp_path):
    p = tmp_path / "empty.log"
    p.write_text("", encoding="utf-8")
    return str(p)


# -- Tests: full representative log ------------------------------------------

def test_hermes_task_durations(log_file):
    r = parse_log(log_file)
    durations = r["hermes_task_duration"]
    assert len(durations) == 2
    assert durations[0] == {"task_id": "abc12345", "duration_s": 45.2}
    assert durations[1] == {"task_id": "def67890", "duration_s": 10.0}


def test_error_extraction_with_stacktraces(log_file):
    r = parse_log(log_file)
    assert r["error_count"] == 3  # hermes failed, athena failed, pipeline critical
    errors = r["errors"]

    # First error: Hermes task failed — has stacktrace
    hermes_err = errors[0]
    assert hermes_err["level"] == "ERROR"
    assert "jkl22222 failed" in hermes_err["message"]
    assert "CalledProcessError" in hermes_err["stacktrace"]
    assert "Traceback" in hermes_err["stacktrace"]

    # Second error: Athena planning failed — no stacktrace (next line is a normal log)
    athena_err = errors[1]
    assert "planning failed" in athena_err["message"]
    assert athena_err["stacktrace"] == ""

    # Third error: pipeline critical with stacktrace
    critical_err = errors[2]
    assert critical_err["level"] == "CRITICAL"
    assert "database is locked" in critical_err["message"] or "database locked" in critical_err["message"]
    assert "OperationalError" in critical_err["stacktrace"]


def test_gateway_latency(log_file):
    r = parse_log(log_file)
    lat = r["gateway_latency"]
    assert len(lat) == 2
    assert lat[0] == {"provider": "claude", "chars": 5678, "latency_s": 1.1}
    assert lat[1] == {"provider": "gemini", "chars": 1234, "latency_s": 2.3}
    assert r["gateway_call_count"] == 2


def test_cli_lifecycle(log_file):
    r = parse_log(log_file)
    assert r["cli_launch_count"] == 2
    assert r["cli_complete_count"] == 2
    assert r["cli_timeout_count"] == 1
    assert r["cli_fail_count"] == 1
    assert r["cli_launches"][0] == {"task_id": "abc12345", "provider": "claude_code"}
    assert r["cli_launches"][1] == {"task_id": "def67890", "provider": "gemini_cli"}


def test_provider_availability(log_file):
    r = parse_log(log_file)
    assert r["provider_availability_failure"] == 1


def test_dispatch(log_file):
    r = parse_log(log_file)
    assert r["tasks_unblocked"] == 5  # 3 + 2
    assert len(r["dispatch_events"]) == 2
    assert r["dispatch_events"][0] == {"count": 3, "project_id": "proj0001"}


def test_planning(log_file):
    r = parse_log(log_file)
    assert r["plan_success_count"] == 1
    assert r["plan_fail_count"] == 1


def test_mimir_verdicts(log_file):
    r = parse_log(log_file)
    assert r["verify_passed"] == 1
    assert r["verify_gaps_found"] == 1
    assert r["verify_human_needed"] == 1
    assert len(r["verdicts"]) == 3
    assert r["verdicts"][0] == {"verdict": "passed", "task_id": "abc12345"}
    assert r["verdicts"][1] == {"verdict": "gaps_found", "task_id": "def67890"}
    assert r["verdicts"][2] == {"verdict": "human_needed", "task_id": "ghi11111"}


def test_tyche_costs(log_file):
    r = parse_log(log_file)
    assert len(r["task_costs"]) == 2
    assert r["task_costs"][0] == {"task_id": "abc12345", "cost": 0.0234}
    assert r["task_costs"][1] == {"task_id": "def67890", "cost": 0.15}
    assert abs(r["total_cost"] - 0.1734) < 1e-6


# -- Tests: edge cases -------------------------------------------------------

def test_empty_log(empty_log):
    r = parse_log(empty_log)
    assert r["error_count"] == 0
    assert r["hermes_task_duration"] == []
    assert r["gateway_latency"] == []
    assert r["total_cost"] == 0.0
    assert r["verdicts"] == []


def test_file_not_found():
    with pytest.raises(FileNotFoundError):
        parse_log("/nonexistent/path/pipeline.log")


def test_malformed_lines(tmp_path):
    """Lines that don't match the log format are silently skipped."""
    content = """\
this is not a log line
21:00:00 [test] INFO normal line
garbage garbage garbage
   indented non-stacktrace line
21:00:01 [test] INFO another normal line
"""
    p = tmp_path / "malformed.log"
    p.write_text(content, encoding="utf-8")
    r = parse_log(str(p))
    # Should parse without error, no metrics extracted from garbage
    assert r["error_count"] == 0


def test_multiline_stacktrace_at_end_of_file(tmp_path):
    """Stacktrace at EOF should be flushed correctly."""
    content = """\
10:00:00 [test] ERROR Something broke
  Traceback (most recent call last):
    File "foo.py", line 1, in bar
      raise ValueError("boom")
  ValueError: boom
"""
    p = tmp_path / "trailing.log"
    p.write_text(content, encoding="utf-8")
    r = parse_log(str(p))
    assert r["error_count"] == 1
    assert "ValueError: boom" in r["errors"][0]["stacktrace"]
    assert "Traceback" in r["errors"][0]["stacktrace"]


def test_since_filter(tmp_path):
    """Lines before `since` should be excluded."""
    content = """\
10:00:00 [test] INFO Tyche: task early cost $1.00
12:00:00 [test] INFO Tyche: task late1 cost $2.00
14:00:00 [test] INFO Tyche: task late2 cost $3.00
"""
    p = tmp_path / "filtered.log"
    p.write_text(content, encoding="utf-8")
    since = datetime.combine(date.today(), datetime.strptime("11:00:00", "%H:%M:%S").time())
    r = parse_log(str(p), since=since)
    assert len(r["task_costs"]) == 2
    assert r["total_cost"] == 5.0


def test_consecutive_errors_no_stacktrace(tmp_path):
    """Two errors in a row, neither with stacktraces."""
    content = """\
10:00:00 [a] ERROR first error
10:00:01 [b] ERROR second error
10:00:02 [c] INFO normal line
"""
    p = tmp_path / "consecutive.log"
    p.write_text(content, encoding="utf-8")
    r = parse_log(str(p))
    assert r["error_count"] == 2
    assert r["errors"][0]["stacktrace"] == ""
    assert r["errors"][1]["stacktrace"] == ""


# -- Tests: daily_summary ----------------------------------------------------

def test_daily_summary(log_file):
    metrics = parse_log(log_file)
    rows = daily_summary(metrics, day=date(2026, 4, 1))

    row_dict = {name: value for name, value, _ in rows}

    assert row_dict["hermes_task_duration_avg"] == pytest.approx(27.6, abs=0.1)
    assert row_dict["hermes_task_duration_p95"] == 45.2
    assert row_dict["error_count"] == 3.0
    assert row_dict["gateway_call_count"] == 2.0
    assert row_dict["cli_launch_count"] == 2.0
    assert row_dict["cli_complete_count"] == 2.0
    assert row_dict["cli_timeout_count"] == 1.0
    assert row_dict["cli_fail_count"] == 1.0
    assert row_dict["plan_success_count"] == 1.0
    assert row_dict["plan_fail_count"] == 1.0
    assert row_dict["total_cost"] == pytest.approx(0.1734, abs=1e-4)
