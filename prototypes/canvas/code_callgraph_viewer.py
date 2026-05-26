"""Entry point — render a call graph rooted at the methods of one file.

Run:    cd prototypes/canvas
        uv run python code_callgraph_viewer.py CombatResolver.cs [hops] [project]
        # defaults: hops=2, project=DungeonCrawl
"""
from __future__ import annotations

import sys

from code_callgraph_source import CodeCallGraphSource
from launch import run


DEFAULT_PROJECT = "DungeonCrawl"
DEFAULT_HOPS = 2


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "usage: code_callgraph_viewer.py <file.cs> [hops] [project]\n"
            f"  hops    default {DEFAULT_HOPS}\n"
            f"  project default {DEFAULT_PROJECT}",
            file=sys.stderr,
        )
        sys.exit(2)

    seed_file = sys.argv[1]
    hops = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_HOPS
    project = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_PROJECT

    run(CodeCallGraphSource(project, seed_file, hops=hops))


if __name__ == "__main__":
    main()
