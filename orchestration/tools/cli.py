#!/usr/bin/env python3
# orchestration/tools/cli.py
#
# Local monitoring CLI for the orchestration engine.
# Reads orchestration/data/orchestration.db and orchestration/config.json directly.
# No REST API, no auth, no external dependencies.
#
# Usage:
#   python tools/cli.py --help
#   python tools/cli.py usage
#   python tools/cli.py status
#   python tools/cli.py providers
#   python tools/cli.py history
#   python tools/cli.py usage --json
#
# Depends on: orchestration/data/orchestration.db, orchestration/config.json
# Used by:    humans, AI agents monitoring task execution

import argparse
import json
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CLIError(Exception):
    """CLI-specific error for graceful error handling."""
    pass

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_ORCH_DIR = _SCRIPT_DIR.parent
DB_PATH = _ORCH_DIR / "data" / "orchestration.db"
CONFIG_PATH = _ORCH_DIR / "config.json"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def _error_exit(message: str, args: argparse.Namespace, code: int = 1) -> None:
    """Print error in appropriate format (JSON or stderr) and exit."""
    if args.json:
        print(json.dumps({"error": message}))
    else:
        print(f"Error: {message}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load orchestration config.json, return empty dict if missing."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Open the SQLite database in read-only mode. Raises CLIError if not found."""
    if not DB_PATH.exists():
        raise CLIError(f"Database not found: {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Table rendering (pure stdlib)
# ---------------------------------------------------------------------------

def render_table(headers: list[str], rows: list[list], min_col_width: int = 3) -> str:
    """
    Render a list of rows as an aligned terminal table.

    Args:
        headers: Column header strings.
        rows:    Each row is a list of values (auto-converted to str).
        min_col_width: Minimum width for any column.

    Returns:
        A multi-line string ready for print().
    """
    if not headers:
        return ""

    str_rows = [[str(v) for v in row] for row in rows]

    widths = [
        max(min_col_width, len(h), max((len(r[i]) for r in str_rows), default=0))
        for i, h in enumerate(headers)
    ]

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    fmt_row = lambda cells: "| " + " | ".join(
        c.ljust(widths[i]) for i, c in enumerate(cells)
    ) + " |"

    lines = [sep, fmt_row(headers), sep]
    for row in str_rows:
        # Pad short rows so we never index out of range
        padded = row + [""] * (len(headers) - len(row))
        lines.append(fmt_row(padded))
    lines.append(sep)
    return "\n".join(lines)


def print_table(headers: list[str], rows: list[list], title: str = "") -> None:
    """Print an optional title then a formatted table."""
    if title:
        print(f"\n{title}")
        print("=" * len(title))
    print(render_table(headers, rows))


# ---------------------------------------------------------------------------
# Provider name mapping
# DB writes short names; config uses longer canonical names.
# ---------------------------------------------------------------------------

# Maps DB provider names → config provider_quotas keys
_DB_TO_CONFIG: dict[str, str] = {
    "gemini":          "gemini_cli",
    "codex":           "codex_cli",
    "claude_code_cli": "claude_code",
    # pass-through (already match config)
    "ollama":          "ollama",
    "claude_code":     "claude_code",
    "gemini_cli":      "gemini_cli",
    "codex_cli":       "codex_cli",
}


def _config_key(db_provider: str) -> str:
    """Return the config provider_quotas key for a DB provider name."""
    return _DB_TO_CONFIG.get(db_provider, db_provider)


# ---------------------------------------------------------------------------
# Subcommand stubs (filled in by sibling tasks)
# ---------------------------------------------------------------------------

def _get_provider_quotas(config: dict) -> dict:
    """Return provider_quotas section from config, keyed by provider name."""
    return config.get("provider_quotas", {})


def _window_seconds(window: dict) -> int:
    if "duration_minutes" in window:
        return int(window["duration_minutes"] * 60)
    return int(window.get("duration_hours", 1) * 3600)


def _query_usage(conn: sqlite3.Connection, provider: str, since_ts: float) -> dict:
    """Return token totals and request count for a provider since since_ts."""
    row = conn.execute(
        "SELECT COALESCE(SUM(prompt_tokens), 0) AS pt, "
        "       COALESCE(SUM(completion_tokens), 0) AS ct, "
        "       COUNT(*) AS reqs "
        "FROM usage_log WHERE provider = ? AND timestamp >= ?",
        (provider, since_ts),
    ).fetchone()
    return {
        "prompt_tokens": int(row["pt"]),
        "completion_tokens": int(row["ct"]),
        "total_tokens": int(row["pt"]) + int(row["ct"]),
        "requests": int(row["reqs"]),
    }


def _headroom_str(used: int, limit: int) -> str:
    """Return 'used/limit (pct%)' or 'used (no limit)' string."""
    if limit <= 0:
        return f"{used:,} (no limit)"
    pct = used * 100 // limit
    remaining = max(0, limit - used)
    return f"{used:,}/{limit:,} ({pct}%) — {remaining:,} left"


def cmd_usage(args: argparse.Namespace) -> None:
    """Token consumption per provider — current 5h window, today, this week."""
    import time, datetime

    try:
        conn = get_db()
    except CLIError as e:
        _error_exit(str(e), args)
        return

    config = load_config()
    quotas = _get_provider_quotas(config)

    now = time.time()

    # Fixed windows used for the table
    today_start = datetime.datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    week_start = (
        datetime.datetime.now() - datetime.timedelta(days=datetime.datetime.now().weekday())
    ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    window_5h = now - 5 * 3600

    # Collect DB provider names first (these have actual data)
    db_providers = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT provider FROM usage_log"
        ).fetchall()
    ]
    providers = list(db_providers)

    # Also add any config-only providers (no DB rows yet) by their config key
    seen_config_keys = {_config_key(p) for p in providers}
    for config_key in quotas:
        if config_key not in seen_config_keys:
            providers.append(config_key)

    if not providers:
        print("No usage data found.")
        conn.close()
        return

    # Build rows
    rows_data = []
    for provider in providers:
        u5h = _query_usage(conn, provider, window_5h)
        utd = _query_usage(conn, provider, today_start)
        uwk = _query_usage(conn, provider, week_start)

        # Look up quotas via canonical config key (handles DB/config name mismatch)
        pq = quotas.get(_config_key(provider), {})
        windows = pq.get("windows", [])

        # Find quota limits for display (prefer matching window by name/duration)
        limit_5h = 0
        limit_today_req = 0
        limit_week = 0
        for w in windows:
            metric = w.get("metric", "tokens")
            dur_h = w.get("duration_hours", 0)
            dur_m = w.get("duration_minutes", 0)
            name = w.get("name", "")
            lim = int(w.get("limit", 0))
            if metric == "tokens":
                if dur_h == 5 or name == "sliding_5h":
                    limit_5h = lim
                elif dur_h == 168 or name == "weekly":
                    limit_week = lim
            elif metric == "requests":
                if dur_h == 24 or name == "daily":
                    limit_today_req = lim

        rows_data.append({
            "provider": provider,
            "display_name": _config_key(provider),  # canonical name for display
            "u5h": u5h,
            "utd": utd,
            "uwk": uwk,
            "limit_5h": limit_5h,
            "limit_today_req": limit_today_req,
            "limit_week": limit_week,
            "is_request_based": any(
                w.get("metric") == "requests" for w in windows
            ),
        })

    conn.close()

    if args.json:
        out = []
        for r in rows_data:
            out.append({
                "provider": r["provider"],
                "last_5h": r["u5h"],
                "today": r["utd"],
                "this_week": r["uwk"],
                "limits": {
                    "tokens_5h": r["limit_5h"],
                    "requests_daily": r["limit_today_req"],
                    "tokens_weekly": r["limit_week"],
                },
            })
        print(json.dumps(out, indent=2))
        return

    # --- Token table ---
    token_providers = [r for r in rows_data if not r["is_request_based"]]
    if token_providers:
        headers = ["Provider", "5h tokens", "Headroom (5h)", "Week tokens", "Headroom (week)"]
        table_rows = []
        for r in token_providers:
            table_rows.append([
                r["display_name"],
                f"{r['u5h']['total_tokens']:,}",
                _headroom_str(r["u5h"]["total_tokens"], r["limit_5h"]),
                f"{r['uwk']['total_tokens']:,}",
                _headroom_str(r["uwk"]["total_tokens"], r["limit_week"]),
            ])
        print_table(headers, table_rows, title="Token Usage")

    # --- Request table (Gemini etc.) ---
    req_providers = [r for r in rows_data if r["is_request_based"]]
    if req_providers:
        headers = ["Provider", "Today reqs", "Headroom (daily)", "5h reqs", "Week reqs"]
        table_rows = []
        for r in req_providers:
            table_rows.append([
                r["display_name"],
                f"{r['utd']['requests']:,}",
                _headroom_str(r["utd"]["requests"], r["limit_today_req"]),
                f"{r['u5h']['requests']:,}",
                f"{r['uwk']['requests']:,}",
            ])
        print_table(headers, table_rows, title="Request Usage")

    # --- Today cost summary ---
    all_td_tokens = sum(r["utd"]["total_tokens"] for r in rows_data)
    all_5h_tokens = sum(r["u5h"]["total_tokens"] for r in rows_data)
    print(f"\nTotal tokens today: {all_td_tokens:,}  |  last 5h: {all_5h_tokens:,}")


def cmd_status(args: argparse.Namespace) -> None:
    """Running/queued tasks, recent failures, per-project progress."""
    try:
        conn = get_db()
    except CLIError as e:
        _error_exit(str(e), args)
        return

    cur = conn.cursor()

    # 1. Running tasks grouped by provider (claimed_by agent, fallback to model_tier)
    cur.execute("""
        SELECT COALESCE(claimed_by, model_tier) AS provider,
               COUNT(*) AS count,
               GROUP_CONCAT(SUBSTR(title, 1, 40), ' | ') AS titles
          FROM tasks
         WHERE status = 'in_progress'
         GROUP BY provider
         ORDER BY count DESC
    """)
    running_rows = cur.fetchall()

    # 2. Queued tasks by tier
    cur.execute("""
        SELECT model_tier, COUNT(*) AS count
          FROM tasks
         WHERE status = 'pending'
         GROUP BY model_tier
         ORDER BY count DESC
    """)
    queued_rows = cur.fetchall()

    # 3. Last 5 failures
    cur.execute("""
        SELECT SUBSTR(title, 1, 40) AS title,
               COALESCE(claimed_by, model_tier) AS provider,
               SUBSTR(COALESCE(error, '(no error text)'), 1, 80) AS error_summary,
               DATETIME(updated_at, 'unixepoch', 'localtime') AS failed_at
          FROM tasks
         WHERE status = 'failed'
         ORDER BY updated_at DESC
         LIMIT 5
    """)
    failure_rows = cur.fetchall()

    # 4. Project progress
    cur.execute("""
        SELECT p.name,
               COUNT(t.id)                                               AS total,
               SUM(CASE WHEN t.status = 'completed'   THEN 1 ELSE 0 END) AS done,
               SUM(CASE WHEN t.status = 'in_progress' THEN 1 ELSE 0 END) AS running,
               SUM(CASE WHEN t.status = 'failed'      THEN 1 ELSE 0 END) AS failed,
               SUM(CASE WHEN t.status = 'pending'     THEN 1 ELSE 0 END) AS pending
          FROM projects p
          LEFT JOIN tasks t ON t.project_id = p.id
         GROUP BY p.id, p.name
         ORDER BY p.created_at DESC
    """)
    progress_rows = cur.fetchall()
    conn.close()

    if args.json:
        data = {
            "running":  [dict(r) for r in running_rows],
            "queued":   [dict(r) for r in queued_rows],
            "failures": [dict(r) for r in failure_rows],
            "projects": [dict(r) for r in progress_rows],
        }
        print(json.dumps(data, indent=2))
        return

    # --- Running tasks ---
    if running_rows:
        print_table(
            ["Provider", "Count", "Tasks (truncated)"],
            [[r["provider"], r["count"], r["titles"]] for r in running_rows],
            title="Running Tasks by Provider",
        )
    else:
        print("\nRunning Tasks by Provider")
        print("=========================")
        print("  (none)")

    # --- Queued tasks ---
    if queued_rows:
        print_table(
            ["Tier", "Queued"],
            [[r["model_tier"], r["count"]] for r in queued_rows],
            title="Queued Tasks by Tier",
        )
    else:
        print("\nQueued Tasks by Tier")
        print("====================")
        print("  (none)")

    # --- Last 5 failures ---
    if failure_rows:
        print_table(
            ["Title", "Provider", "Error", "Failed At"],
            [[r["title"], r["provider"], r["error_summary"], r["failed_at"]]
             for r in failure_rows],
            title="Last 5 Failures",
        )
    else:
        print("\nLast 5 Failures")
        print("===============")
        print("  (none)")

    # --- Project progress ---
    if progress_rows:
        progress_display = []
        for r in progress_rows:
            total = r["total"] or 0
            done  = r["done"]  or 0
            pct   = f"{done / total * 100:.0f}%" if total else "n/a"
            progress_display.append([
                r["name"][:40],
                done,
                r["running"] or 0,
                r["failed"]  or 0,
                r["pending"] or 0,
                total,
                pct,
            ])
        print_table(
            ["Project", "Done", "Running", "Failed", "Pending", "Total", "%"],
            progress_display,
            title="Project Progress",
        )



def cmd_providers(args: argparse.Namespace) -> None:
    """CLI install status, Ollama connectivity, quota utilization."""
    config = load_config()

    # Check CLI installations
    cli_names = ["claude", "gemini", "codex"]
    providers_status = {}
    for cli_name in cli_names:
        path = shutil.which(cli_name)
        providers_status[cli_name] = {
            "installed": path is not None,
            "path": path,
        }

    # Check Ollama connectivity
    ollama_info = {
        "connected": False,
        "models": [],
        "default_model": None,
        "url": None,
    }

    ollama_config = config.get("ollama", {})
    ollama_url = ollama_config.get("hosts", {}).get("local", "http://localhost:11434")
    ollama_info["url"] = ollama_url

    try:
        response = urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=2)
        if response.status == 200:
            data = json.loads(response.read().decode())
            ollama_info["connected"] = True
            if "models" in data:
                ollama_info["models"] = [m.get("name", "unknown") for m in data["models"]]
            ollama_info["default_model"] = ollama_config.get("default_model")
    except (urllib.error.URLError, urllib.error.HTTPError, Exception):
        pass

    # Get provider quotas from config
    provider_quotas = config.get("provider_quotas", {})

    if args.json:
        output = {
            "providers": providers_status,
            "ollama": ollama_info,
            "quotas": provider_quotas,
        }
        print(json.dumps(output, indent=2))
    else:
        # CLI Status Table
        print_table(
            ["CLI", "Status", "Path"],
            [
                [
                    cli_name.capitalize(),
                    "[+] Installed" if providers_status[cli_name]["installed"] else "[-] Not found",
                    providers_status[cli_name]["path"] or "-",
                ]
                for cli_name in cli_names
            ],
            title="CLI Installation Status",
        )

        # Ollama Status
        print("\nOllama Connectivity")
        print("=" * 50)
        print(f"{'Status':<25} {'[+] Connected' if ollama_info['connected'] else '[-] Not available'}")
        print(f"{'URL':<25} {ollama_url}")
        print(f"{'Default Model':<25} {ollama_info['default_model'] or '-'}")
        if ollama_info["models"]:
            models_str = ", ".join(ollama_info["models"])
            print(f"{'Available Models':<25} {models_str}")
        else:
            print(f"{'Available Models':<25} -")

        # Provider Quotas Table
        quota_rows = []
        for provider, quota_info in provider_quotas.items():
            windows = quota_info.get("windows", [])
            if windows:
                for i, window in enumerate(windows):
                    row = [
                        provider if i == 0 else "",
                        window.get("name", "?"),
                        window.get("metric", "?"),
                        str(window.get("limit", "?")),
                    ]
                    quota_rows.append(row)
            else:
                quota_rows.append([provider, "-", "-", "-"])

        print_table(
            ["Provider", "Window", "Metric", "Limit"],
            quota_rows,
            title="Provider Quotas",
        )


def _ascii_bar(value: int, max_value: int, width: int = 30) -> str:
    """Return a '#'-filled bar scaled to max_value, padded to width."""
    if max_value <= 0:
        filled = 0
    else:
        filled = round(value * width / max_value)
    return "#" * filled + "." * (width - filled)


def _format_k(n: int) -> str:
    """Format large numbers compactly: 1200 -> '1.2k', 3400000 -> '3.4M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def cmd_history(args: argparse.Namespace) -> None:
    """Hourly/daily token usage with ASCII bar charts."""
    import time, datetime

    try:
        conn = get_db()
    except CLIError as e:
        _error_exit(str(e), args)
        return

    now = time.time()

    # ---- Hourly buckets: last 24 h ----------------------------------------
    since_24h = now - 24 * 3600
    hourly_rows = conn.execute(
        """
        SELECT provider,
               CAST((timestamp - ?) / 3600 AS INTEGER) AS hour_offset,
               COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
               COUNT(*) AS reqs
          FROM usage_log
         WHERE timestamp >= ?
         GROUP BY provider, hour_offset
         ORDER BY provider, hour_offset
        """,
        (since_24h, since_24h),
    ).fetchall()

    # ---- Daily buckets: last 7 d -------------------------------------------
    since_7d = now - 7 * 86400
    daily_rows = conn.execute(
        """
        SELECT provider,
               DATE(timestamp, 'unixepoch', 'localtime') AS day,
               COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
               COUNT(*) AS reqs
          FROM usage_log
         WHERE timestamp >= ?
         GROUP BY provider, day
         ORDER BY provider, day
        """,
        (since_7d,),
    ).fetchall()

    conn.close()

    if args.json:
        print(json.dumps({
            "hourly": [dict(r) for r in hourly_rows],
            "daily":  [dict(r) for r in daily_rows],
        }, indent=2))
        return

    # ---- Organise by provider ----------------------------------------------
    hourly: dict = {}   # provider -> {hour_offset: tokens}
    for r in hourly_rows:
        hourly.setdefault(r["provider"], {})[int(r["hour_offset"])] = int(r["tokens"])

    daily: dict = {}    # provider -> {day: tokens}
    for r in daily_rows:
        daily.setdefault(r["provider"], {})[r["day"]] = int(r["tokens"])

    providers = sorted(set(list(hourly.keys()) + list(daily.keys())))
    if not providers:
        print("No usage history found.")
        return

    BAR_W = 30
    now_dt = datetime.datetime.now()

    for provider in providers:
        # ---- Hourly chart --------------------------------------------------
        h_data = hourly.get(provider, {})
        # hour_offset 0 = oldest slot, 23 = most recent
        slots_h = [h_data.get(i, 0) for i in range(24)]
        max_h = max(slots_h) if slots_h else 0

        print(f"\n{'-' * 60}")
        print(f"  {provider}  -- last 24 h (tokens per hour)")
        print(f"{'-' * 60}")
        for i, val in enumerate(slots_h):
            slot_start = since_24h + i * 3600
            label = datetime.datetime.fromtimestamp(slot_start).strftime("%H:00")
            bar = _ascii_bar(val, max_h, BAR_W)
            num = _format_k(val).rjust(6)
            print(f"  {label}  {bar}  {num}")

        # ---- Daily chart ---------------------------------------------------
        d_data = daily.get(provider, {})
        day_labels = [
            (now_dt - datetime.timedelta(days=6 - i)).strftime("%Y-%m-%d")
            for i in range(7)
        ]
        slots_d = [d_data.get(day, 0) for day in day_labels]
        max_d = max(slots_d) if slots_d else 0

        print(f"\n  {provider}  -- last 7 d (tokens per day)")
        print(f"{'-' * 60}")
        for day, val in zip(day_labels, slots_d):
            bar = _ascii_bar(val, max_d, BAR_W)
            num = _format_k(val).rjust(6)
            print(f"  {day}  {bar}  {num}")

    print(f"\n{'-' * 60}")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Orchestration engine monitoring CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python tools/cli.py usage
  python tools/cli.py status
  python tools/cli.py providers
  python tools/cli.py history
  python tools/cli.py usage --json
""",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="emit machine-readable JSON instead of terminal tables",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    sub.add_parser("usage",     help="token/request consumption per provider")
    sub.add_parser("status",    help="running tasks, queued tasks, recent failures")
    sub.add_parser("providers", help="CLI install check, Ollama connectivity, quotas")
    sub.add_parser("history",   help="hourly and daily usage charts (last 24h / 7d)")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "usage":     cmd_usage,
        "status":    cmd_status,
        "providers": cmd_providers,
        "history":   cmd_history,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
