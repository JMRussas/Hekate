#!/usr/bin/env python3
"""Sentinel CLI — health monitoring, observation tracking, decision reasoning."""

import argparse
import os
import sys

import httpx

DEFAULT_BASE = "http://localhost:5200"


def build_client(args) -> httpx.Client:
    token = args.token or os.environ.get("ORCH_TOKEN", "")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=args.base_url, headers=headers, timeout=10)


def cmd_health(args):
    """GET /api/sentinel/status — format as table."""
    with build_client(args) as client:
        resp = client.get("/api/sentinel/status")
        resp.raise_for_status()
        data = resp.json()

    # Health trends table
    trends = data.get("health_trends", {})
    if not trends:
        print("No health trends reported.")
    else:
        # Column widths
        svc_w = max(len("SERVICE"), max(len(k) for k in trends))
        st_w = max(len("STATE"), max(len(v["state"]) for v in trends.values()))
        lat_hdr = "LATENCY (ms)"

        print(f"{'SERVICE':<{svc_w}}  {'STATE':<{st_w}}  {lat_hdr:>12}  {'FAIL %':>6}  {'SAMPLES':>7}")
        print(f"{'-' * svc_w}  {'-' * st_w}  {'-' * 12}  {'-' * 6}  {'-' * 7}")
        for svc, info in sorted(trends.items()):
            lat = f"{info['avg_latency_ms']:>12.1f}" if info["avg_latency_ms"] is not None else f"{'—':>12}"
            fail_pct = f"{info['failure_rate'] * 100:>5.1f}%"
            print(f"{svc:<{svc_w}}  {info['state']:<{st_w}}  {lat}  {fail_pct}  {info['sample_count']:>7}")

    # Plan sentinels summary
    plans = data.get("plan_sentinels", {})
    if plans:
        print(f"\nActive plan sentinels: {len(plans)}")
        for pid, ps in sorted(plans.items()):
            status = "running" if ps["running"] else "stopped"
            print(f"  {pid}: {status}, wave {ps['current_wave']}, "
                  f"{ps['task_count']} tasks, {ps['failure_count']} failures")

    print(f"\nSentinel: {'RUNNING' if data.get('running') else 'STOPPED'}")

    # Exit 1 if any service is degraded or unhealthy
    degraded = any(v["state"] not in ("healthy",) for v in trends.values())
    return 1 if degraded else 0


def cmd_observations(args):
    """GET /api/sentinel/observations — format as timeline."""
    params: dict = {"limit": args.limit if hasattr(args, "limit") else 50}
    if hasattr(args, "project_id") and args.project_id:
        params["project_id"] = args.project_id
    if hasattr(args, "severity") and args.severity:
        params["severity"] = args.severity
    if hasattr(args, "category") and args.category:
        params["category"] = args.category

    with build_client(args) as client:
        resp = client.get("/api/sentinel/observations", params=params)
        resp.raise_for_status()
        observations = resp.json()

    if not observations:
        print("No observations found.")
        return 0

    # Severity symbols for visual scanning
    sev_icon = {"critical": "!!", "warning": "! ", "info": "  "}

    for obs in reversed(observations):  # chronological (API returns newest first)
        ts = obs["timestamp"]
        # Trim to HH:MM:SS if full ISO
        if "T" in ts:
            ts = ts.split("T")[1][:8]

        sev = sev_icon.get(obs.get("severity", "info"), "  ")
        cat = obs.get("category", "")
        msg = obs.get("message", "")

        prefix = f"[{ts}] {sev} {cat}"
        print(prefix)
        print(f"  {msg}")

        # Show project/task context if present
        ctx_parts = []
        if obs.get("project_id"):
            ctx_parts.append(f"project={obs['project_id'][:8]}")
        if obs.get("task_id"):
            ctx_parts.append(f"task={obs['task_id'][:8]}")
        if ctx_parts:
            print(f"  ({', '.join(ctx_parts)})")

        # Show details if present
        details = obs.get("details")
        if details and isinstance(details, dict):
            for k, v in details.items():
                print(f"    {k}: {v}")
        print()

    return 0


def cmd_decisions(args):
    """GET /api/sentinel/decisions — show reasoning chains."""
    params: dict = {"limit": args.limit if hasattr(args, "limit") else 50}
    if hasattr(args, "project_id") and args.project_id:
        params["project_id"] = args.project_id
    if hasattr(args, "command") and args.command:
        params["command"] = args.command

    with build_client(args) as client:
        resp = client.get("/api/sentinel/decisions", params=params)
        resp.raise_for_status()
        decisions = resp.json()

    if not decisions:
        print("No decisions found.")
        return 0

    for i, dec in enumerate(decisions):
        ts = dec.get("timestamp", "")
        if "T" in ts:
            ts = ts.split("T")[1][:8]

        conf = dec.get("confidence", 0)
        conf_bar = "#" * int(conf * 10) + "-" * (10 - int(conf * 10))

        print(f"{'─' * 60}")
        print(f"  {dec.get('command', '?')}  [{ts}]  confidence [{conf_bar}] {conf:.0%}")
        if dec.get("project_id"):
            print(f"  project: {dec['project_id'][:12]}")

        # Reasoning chain — may be multi-line or structured
        reasoning = dec.get("reasoning", "")
        if reasoning:
            print(f"\n  Reasoning:")
            for line in reasoning.strip().splitlines():
                print(f"    {line}")

        # Outcome
        outcome = dec.get("outcome", "")
        if outcome:
            print(f"\n  Outcome: {outcome}")

        # Nested details
        details = dec.get("details", {})
        if details and isinstance(details, dict):
            print(f"\n  Details:")
            _print_details(details, indent=4)

        print()

    print(f"{'─' * 60}")
    print(f"{len(decisions)} decision(s)")
    return 0


def _print_details(obj, indent=4):
    """Recursively print nested details with indentation."""
    prefix = " " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                print(f"{prefix}{k}:")
                _print_details(v, indent + 2)
            else:
                print(f"{prefix}{k}: {v}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _print_details(item, indent)
                print(f"{prefix}---")
            else:
                print(f"{prefix}- {item}")


def main():
    parser = argparse.ArgumentParser(
        prog="sentinel_cli",
        description="Sentinel CLI — health, observations, and decision reasoning",
    )
    parser.add_argument(
        "--token", default=None, help="Auth token (default: ORCH_TOKEN env var)"
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE, help=f"Orchestration API base URL (default: {DEFAULT_BASE})"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="Show sentinel service health status")

    obs_p = sub.add_parser("observations", help="Show observation timeline")
    obs_p.add_argument("--project-id", default=None, help="Filter by project ID")
    obs_p.add_argument("--severity", default=None, choices=["info", "warning", "critical"])
    obs_p.add_argument("--category", default=None, help="Filter by category")
    obs_p.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")

    dec_p = sub.add_parser("decisions", help="Show decision reasoning chains")
    dec_p.add_argument("--project-id", default=None, help="Filter by project ID")
    dec_p.add_argument("--command", default=None, help="Filter by command type")
    dec_p.add_argument("--limit", type=int, default=50, help="Max results (default: 50)")

    args = parser.parse_args()

    dispatch = {
        "health": cmd_health,
        "observations": cmd_observations,
        "decisions": cmd_decisions,
    }
    sys.exit(dispatch[args.command](args) or 0)


if __name__ == "__main__":
    main()
