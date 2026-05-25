"""Smoke test — load both code sources against the DungeonCrawl project
and print structural counts without opening a TUI. Catches import errors,
schema mismatches, and the obvious 'I forgot a NodeKind glyph' break."""
from __future__ import annotations

import asyncio

from code_file_source import CodeFileSource
from code_structure_source import CodeStructureSource
from graph import Graph, NodeKind


def summarize(graph: Graph, label: str) -> None:
    kind_counts: dict[str, int] = {}
    for n in graph.nodes.values():
        kind_counts[n.kind.value] = kind_counts.get(n.kind.value, 0) + 1
    print(f"\n=== {label} ===")
    print(f"  status: {graph.status_line}")
    print(f"  total nodes: {len(graph.nodes)}")
    for kind, count in sorted(kind_counts.items()):
        print(f"    {kind:20s} {count}")


async def main() -> None:
    g1 = Graph()
    print("loading CodeFileSource(DungeonCrawl)...")
    await CodeFileSource("DungeonCrawl").load(g1)
    summarize(g1, "CodeFileSource")

    g2 = Graph()
    print("\nloading CodeStructureSource(DungeonCrawl)...")
    await CodeStructureSource("DungeonCrawl").load(g2)
    summarize(g2, "CodeStructureSource")


if __name__ == "__main__":
    asyncio.run(main())
