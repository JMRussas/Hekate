#!/usr/bin/env python3
"""Backfill cost_usd in usage_log and tasks tables using current model_pricing config."""

import json
import sqlite3
import sys

DB_PATH = r"C:\Users\jruss\Documents\GitHub\Hekate\orchestration\data\orchestration.db"
CONFIG_PATH = r"C:\Users\jruss\Documents\GitHub\Hekate\orchestration\config.json"


def load_pricing():
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    return cfg.get("model_pricing", {})


def calculate_cost(pricing, model, prompt_tokens, completion_tokens):
    p = pricing.get(model, {})
    if not p:
        return 0.0
    input_cost = (prompt_tokens / 1_000_000) * p.get("input_per_mtok", 0)
    output_cost = (completion_tokens / 1_000_000) * p.get("output_per_mtok", 0)
    return round(input_cost + output_cost, 6)


def main():
    pricing = load_pricing()
    print(f"Loaded pricing for {len(pricing)} models: {', '.join(pricing.keys())}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    # Backfill usage_log
    rows = conn.execute(
        "SELECT id, model, prompt_tokens, completion_tokens FROM usage_log "
        "WHERE (prompt_tokens > 0 OR completion_tokens > 0) AND cost_usd = 0"
    ).fetchall()

    updated = 0
    skipped = 0
    for row_id, model, pt, ct in rows:
        cost = calculate_cost(pricing, model, pt, ct)
        if cost > 0:
            conn.execute("UPDATE usage_log SET cost_usd = ? WHERE id = ?", (cost, row_id))
            updated += 1
        else:
            skipped += 1

    print(f"usage_log: updated {updated}, skipped {skipped} (no pricing match)")

    # Backfill tasks table
    task_rows = conn.execute(
        "SELECT id, model_used, prompt_tokens, completion_tokens FROM tasks "
        "WHERE (prompt_tokens > 0 OR completion_tokens > 0) AND cost_usd = 0 "
        "AND model_used IS NOT NULL"
    ).fetchall()

    t_updated = 0
    for task_id, model, pt, ct in task_rows:
        cost = calculate_cost(pricing, model, pt, ct)
        if cost > 0:
            conn.execute("UPDATE tasks SET cost_usd = ? WHERE id = ?", (cost, task_id))
            t_updated += 1

    print(f"tasks: updated {t_updated}")

    conn.commit()

    # Verify
    print("\n=== Post-backfill cost summary ===")
    for r in conn.execute(
        "SELECT model, COUNT(*), SUM(cost_usd) FROM usage_log "
        "GROUP BY model ORDER BY SUM(cost_usd) DESC"
    ).fetchall():
        print(f"  {r[0]}: {r[1]} calls, ${r[2]:.4f}")

    conn.close()


if __name__ == "__main__":
    main()
