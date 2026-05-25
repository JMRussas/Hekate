"""Entry point — render an indexed codebase's file/directory tree.

Run:    cd prototypes/canvas
        uv run python code_files_viewer.py [project-name]
        # default project is DungeonCrawl (the DnD game)
"""
from __future__ import annotations

import sys

from code_file_source import CodeFileSource
from launch import run


DEFAULT_PROJECT = "DungeonCrawl"


def main() -> None:
    project = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROJECT
    run(CodeFileSource(project))


if __name__ == "__main__":
    main()
