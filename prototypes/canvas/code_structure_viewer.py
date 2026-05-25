"""Entry point — render an indexed codebase's namespace/class/method tree.

Run:    cd prototypes/canvas
        uv run python code_structure_viewer.py [project-name]
        # default project is DungeonCrawl (the DnD game)
"""
from __future__ import annotations

import sys

from code_structure_source import CodeStructureSource
from launch import run


DEFAULT_PROJECT = "DungeonCrawl"


def main() -> None:
    project = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROJECT
    run(CodeStructureSource(project))


if __name__ == "__main__":
    main()
