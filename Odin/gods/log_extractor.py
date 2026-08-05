"""Log extractor — regex-based parser for engine log format.

Parses lines matching: HH:MM:SS [logger_name] LEVEL message

Extracts metrics for handler durations, errors, gateway latency,
CLI lifecycle, provider availability, dispatch stats, planning,
verification, and cost tracking.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, date
from pathlib import Path
from typing import Any

# -- Log line structure -------------------------------------------------------
# Format: "%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"
# Example: 21:46:02 [gods.pipeline] INFO Pipeline: restored cursor to 47

_LINE_RE = re.compile(
    r"^(\d{2}:\d{2}:\d{2})\s+"
    r"\[([^\]]+)\]\s+"
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(.*)$"
)

# -- Metric patterns ----------------------------------------------------------

# 1. Handler durations: "Hermes: task XXXXXXXX completed in 45.2s (cost=$0.0234)"
_HERMES_DURATION_RE = re.compile(
    r"Hermes: task (\S+) completed in ([\d.]+)s"
)

# 3. Gateway latency: "Gemini: 1234 chars in 2.3s" / "Claude: 5678 chars in 1.1s"
_GATEWAY_LATENCY_RE = re.compile(
    r"(Gemini|Claude): (\d+) chars in ([\d.]+)s"
)

# "Claude: calling" / "Gemini: calling"
_GATEWAY_CALL_RE = re.compile(
    r"(Gemini|Claude): calling"
)

# 4. CLI lifecycle
_CLI_LAUNCH_RE = re.compile(
    r"Hermes: launched (\S+) via (\S+)"
)

_CLI_COMPLETE_RE = re.compile(
    r"Hermes: task (\S+) completed in"
)

_CLI_TIMEOUT_RE = re.compile(
    r"Hermes: task (\S+) timed out"
)

_CLI_FAILED_RE = re.compile(
    r"Hermes: task (\S+) failed"
)

# 5. Provider availability
_PROVIDER_AVAIL_RE = re.compile(
    r"Provider availability check failed"
)

# 6. Dispatch — "Odin: unblocked 3 task(s) for project XXXXXXXX"
_DISPATCH_UNBLOCKED_RE = re.compile(
    r"Odin: unblocked (\d+) task\(s\) for project (\S+)"
)

# 7. Planning — "L1 plan passed rule check (12 tasks)" or "L2 plan passed rule check"
#    These come from narrate() which emits events, and from logger calls.
#    Also matches "Athena leveled: planning failed for XXXXXXXX: ..."
_PLAN_PASSED_RE = re.compile(
    r"plan passed rule check"
)

_PLAN_FAILED_RE = re.compile(
    r"planning failed"
)

# 8. Verification — "Mimir: submitted passed verdict for task XXXXXXXX via /verify API"
_MIMIR_VERDICT_RE = re.compile(
    r"Mimir: submitted (\S+) verdict for task (\S+)"
)

# 9. Cost — "Tyche: task XXXXXXXX cost $0.0234"
_TYCHE_COST_RE = re.compile(
    r"Tyche: task (\S+) cost \$([\d.]+)"
)


def _parse_time(time_str: str, ref_date: date | None = None) -> datetime:
    """Parse HH:MM:SS into a datetime (using ref_date or today)."""
    t = datetime.strptime(time_str, "%H:%M:%S").time()
    d = ref_date or date.today()
    return datetime.combine(d, t)


def parse_log(file_path: str, since: datetime | None = None) -> dict[str, Any]:
    """Parse an engine log file and extract metrics.

    Args:
        file_path: Path to the log file.
        since: If set, only process lines at or after this time.

    Returns:
        Dict with keys for each metric category. Each value is a list of
        extracted data points or an aggregate number.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Log file not found: {file_path}")

    result: dict[str, Any] = {
        # 1. Handler durations
        "hermes_task_duration": [],        # [{task_id, duration_s}]
        # 2. Errors
        "errors": [],                      # [{time, logger, level, message, stacktrace}]
        "error_count": 0,
        # 3. Gateway latency
        "gateway_latency": [],             # [{provider, chars, latency_s}]
        "gateway_call_count": 0,
        # 4. CLI lifecycle
        "cli_launch_count": 0,
        "cli_complete_count": 0,
        "cli_timeout_count": 0,
        "cli_fail_count": 0,
        "cli_launches": [],                # [{task_id, provider}]
        # 5. Provider availability
        "provider_availability_failure": 0,
        # 6. Dispatch
        "tasks_unblocked": 0,
        "dispatch_events": [],             # [{count, project_id}]
        # 7. Planning
        "plan_success_count": 0,
        "plan_fail_count": 0,
        # 8. Verification
        "verify_passed": 0,
        "verify_gaps_found": 0,
        "verify_human_needed": 0,
        "verdicts": [],                    # [{verdict, task_id}]
        # 9. Cost
        "total_cost": 0.0,
        "task_costs": [],                  # [{task_id, cost}]
    }

    ref_date = (since.date() if since else None) or date.today()
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    collecting_stacktrace = False
    current_error: dict | None = None

    for line in lines:
        # Collect indented stacktrace lines after an error
        if collecting_stacktrace:
            if line and (line[0] in (" ", "\t") or line.startswith("Traceback")):
                current_error["stacktrace"] += line + "\n"
                continue
            else:
                # End of stacktrace
                current_error["stacktrace"] = current_error["stacktrace"].rstrip()
                collecting_stacktrace = False
                current_error = None

        m = _LINE_RE.match(line)
        if not m:
            continue

        time_str, logger_name, level, message = m.groups()

        # Time filter
        if since:
            line_time = _parse_time(time_str, ref_date)
            if line_time < since:
                continue

        # 2. Errors
        if level in ("ERROR", "CRITICAL"):
            current_error = {
                "time": time_str,
                "logger": logger_name,
                "level": level,
                "message": message,
                "stacktrace": "",
            }
            result["errors"].append(current_error)
            result["error_count"] += 1
            collecting_stacktrace = True

        # 1. Handler durations
        dm = _HERMES_DURATION_RE.search(message)
        if dm:
            result["hermes_task_duration"].append({
                "task_id": dm.group(1),
                "duration_s": float(dm.group(2)),
            })

        # 3. Gateway latency
        gm = _GATEWAY_LATENCY_RE.search(message)
        if gm:
            result["gateway_latency"].append({
                "provider": gm.group(1).lower(),
                "chars": int(gm.group(2)),
                "latency_s": float(gm.group(3)),
            })

        if _GATEWAY_CALL_RE.search(message):
            result["gateway_call_count"] += 1

        # 4. CLI lifecycle
        lm = _CLI_LAUNCH_RE.search(message)
        if lm:
            result["cli_launch_count"] += 1
            result["cli_launches"].append({
                "task_id": lm.group(1),
                "provider": lm.group(2),
            })

        if _CLI_COMPLETE_RE.search(message):
            result["cli_complete_count"] += 1

        if _CLI_TIMEOUT_RE.search(message):
            result["cli_timeout_count"] += 1

        if _CLI_FAILED_RE.search(message):
            result["cli_fail_count"] += 1

        # 5. Provider availability
        if _PROVIDER_AVAIL_RE.search(message):
            result["provider_availability_failure"] += 1

        # 6. Dispatch
        um = _DISPATCH_UNBLOCKED_RE.search(message)
        if um:
            count = int(um.group(1))
            result["tasks_unblocked"] += count
            result["dispatch_events"].append({
                "count": count,
                "project_id": um.group(2),
            })

        # 7. Planning
        if _PLAN_PASSED_RE.search(message):
            result["plan_success_count"] += 1

        if _PLAN_FAILED_RE.search(message):
            result["plan_fail_count"] += 1

        # 8. Verification
        vm = _MIMIR_VERDICT_RE.search(message)
        if vm:
            verdict = vm.group(1).lower()
            result["verdicts"].append({
                "verdict": verdict,
                "task_id": vm.group(2),
            })
            if verdict == "passed":
                result["verify_passed"] += 1
            elif verdict in ("gaps_found", "gaps-found", "gaps"):
                result["verify_gaps_found"] += 1
            elif verdict in ("human_needed", "human-needed", "human"):
                result["verify_human_needed"] += 1

        # 9. Cost
        cm = _TYCHE_COST_RE.search(message)
        if cm:
            cost = float(cm.group(2))
            result["total_cost"] += cost
            result["task_costs"].append({
                "task_id": cm.group(1),
                "cost": cost,
            })

    # Flush any trailing stacktrace
    if collecting_stacktrace and current_error:
        current_error["stacktrace"] = current_error["stacktrace"].rstrip()

    return result


def daily_summary(
    metrics: dict[str, Any],
    day: date | None = None,
) -> list[tuple[str, float, str]]:
    """Roll up parsed metrics into (metric_name, metric_value, details_json) tuples.

    Ready for DB insert into a daily_metrics table.
    """
    d = (day or date.today()).isoformat()
    rows: list[tuple[str, float, str]] = []

    # Handler durations — avg and p95
    durations = [e["duration_s"] for e in metrics.get("hermes_task_duration", [])]
    if durations:
        durations_sorted = sorted(durations)
        avg = sum(durations_sorted) / len(durations_sorted)
        p95_idx = int(len(durations_sorted) * 0.95)
        p95 = durations_sorted[min(p95_idx, len(durations_sorted) - 1)]
        rows.append(("hermes_task_duration_avg", avg,
                      json.dumps({"date": d, "count": len(durations)})))
        rows.append(("hermes_task_duration_p95", p95,
                      json.dumps({"date": d, "count": len(durations)})))

    # Errors
    rows.append(("error_count", float(metrics.get("error_count", 0)),
                  json.dumps({"date": d})))

    # Gateway latency — per provider avg
    for provider in ("claude", "gemini"):
        lats = [e["latency_s"] for e in metrics.get("gateway_latency", [])
                if e["provider"] == provider]
        if lats:
            rows.append((f"gateway_latency_{provider}_avg",
                          sum(lats) / len(lats),
                          json.dumps({"date": d, "count": len(lats)})))

    rows.append(("gateway_call_count", float(metrics.get("gateway_call_count", 0)),
                  json.dumps({"date": d})))

    # CLI lifecycle
    for key in ("cli_launch_count", "cli_complete_count",
                "cli_timeout_count", "cli_fail_count"):
        rows.append((key, float(metrics.get(key, 0)), json.dumps({"date": d})))

    # Provider availability
    rows.append(("provider_availability_failure",
                  float(metrics.get("provider_availability_failure", 0)),
                  json.dumps({"date": d})))

    # Dispatch
    rows.append(("tasks_unblocked", float(metrics.get("tasks_unblocked", 0)),
                  json.dumps({"date": d})))

    # Planning
    rows.append(("plan_success_count", float(metrics.get("plan_success_count", 0)),
                  json.dumps({"date": d})))
    rows.append(("plan_fail_count", float(metrics.get("plan_fail_count", 0)),
                  json.dumps({"date": d})))

    # Verification
    for key in ("verify_passed", "verify_gaps_found", "verify_human_needed"):
        rows.append((key, float(metrics.get(key, 0)), json.dumps({"date": d})))

    # Cost
    rows.append(("total_cost", metrics.get("total_cost", 0.0),
                  json.dumps({"date": d, "tasks": len(metrics.get("task_costs", []))})))

    return rows
