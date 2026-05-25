"""Shared launcher — wires any CanvasSource to a CanvasApp.

Use:
    from launch import run
    from plan_source import PlanSource
    run(PlanSource("e48a86be-..."))

Internally: a fresh Graph is created, the source's load() runs to completion
(so the first render shows the initial state, not an empty canvas), then a
SourceApp is started with subscribe() running as a Textual worker so any
streaming mutations land in the same graph.
"""
from __future__ import annotations

import asyncio
import sys

from graph import Graph
from renderer import CanvasApp
from source import CanvasSource


class SourceApp(CanvasApp):
    """CanvasApp wired to a single CanvasSource's subscribe() loop."""

    def __init__(self, source: CanvasSource, graph: Graph):
        super().__init__(graph, title=source.title)
        self._source = source

    def on_mount(self) -> None:
        # subscribe() is the long-running stream (or a no-op for one-shot
        # sources). Either way it runs on Textual's event loop as a worker.
        self.run_worker(self._source.subscribe(self.graph), name="source")


def run(source: CanvasSource) -> None:
    """Load the source, then start the TUI. Blocks until the user quits.

    Errors during load() print to stderr and exit non-zero rather than
    starting an empty TUI — easier to debug than a silent black screen.
    """
    graph = Graph()
    try:
        asyncio.run(source.load(graph))
    except Exception as e:
        print(f"source load failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    SourceApp(source, graph).run()
