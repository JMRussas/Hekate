"""Entry point — renders a context-store plan on the canvas.

Most of the logic is in plan_source.py + launch.py. This file just picks the
plan id (CLI arg or default) and hands a PlanSource to the launcher.

Run:    cd prototypes/canvas
        uv run python hekate_viewer.py [plan-uuid]
        # default plan is the Odin one; override CTX_STORE_URL via env.
"""
from __future__ import annotations

import sys

from launch import run
from plan_source import PlanSource


DEFAULT_PLAN_ID = "2b368d62-b76a-4c7b-a835-5601cd61f072"  # "Odin — Orchestration Brain God"


def main() -> None:
    plan_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PLAN_ID
    run(PlanSource(plan_id))


if __name__ == "__main__":
    main()
