"""CodeCallGraphSource — render an indexed codebase's CALLS edges.

Static — takes a seed (file name) and a hop count, walks outgoing CALLS
edges BFS, adds incoming CALLS for any node reached, then renders.
No interactive expansion yet.

Seeds: a file name (e.g. 'CombatResolver.cs') in the indexed project.
The source walks the file's structure to find every method node, then
those methods are the BFS frontier.

Edges in this view are *flow* edges (graph.connect), not containment.
Methods render as flat nodes (no parent), and CALLS show as arrows.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Optional

from context_store_code import CodeClient, DEFAULT_CTX_STORE, Edge, NodeChild
from graph import Graph, NodeKind
from source import CanvasSource


_METHOD_KINDS = {"method", "constructor"}
_STRUCTURAL_KINDS = {"compilation_unit", "namespace", "class", "struct"}


def _short_id(uuid: str) -> str:
    return uuid.split("-", 1)[0]


def _node_kind(node_type: str) -> NodeKind:
    if node_type == "method":
        return NodeKind.METHOD
    if node_type == "constructor":
        return NodeKind.CONSTRUCTOR
    if node_type == "field":
        return NodeKind.FIELD
    if node_type == "class":
        return NodeKind.CLASS
    if node_type == "struct":
        return NodeKind.STRUCT
    return NodeKind.TASK


class CodeCallGraphSource(CanvasSource):
    def __init__(
        self,
        project_name: str,
        seed_file: str,
        *,
        hops: int = 2,
        base_url: str = DEFAULT_CTX_STORE,
    ):
        self.project_name = project_name
        self.seed_file = seed_file
        self.hops = hops
        self.base_url = base_url
        self.title = f"code · calls · {project_name} · {seed_file} (hops={hops})"

    async def load(self, graph: Graph) -> None:
        async with CodeClient(self.base_url) as client:
            project = await client.get_project_by_name(self.project_name)
            files = await client.list_files(project.id)

            seed = self._find_file(files)
            if seed is None or not seed.root_node_id:
                graph.set_status_line(f"seed file {self.seed_file!r} not found in {project.name}")
                return

            graph.set_status_line(f"finding methods in {self.seed_file}…")
            seed_methods = await self._collect_methods(client, seed.root_node_id)
            if not seed_methods:
                graph.set_status_line(f"{self.seed_file}: no methods found")
                return

            for m in seed_methods:
                self._add_method(graph, m, seed=True)

            frontier: deque[tuple[str, int]] = deque((m.id, 0) for m in seed_methods)
            seen: set[str] = {m.id for m in seed_methods}
            edge_count = 0

            while frontier:
                nid, depth = frontier.popleft()
                if depth >= self.hops:
                    continue
                try:
                    edges = await client.get_edges(nid)
                except Exception as e:
                    graph.set_status(_short_id(nid), "failed", error=f"{type(e).__name__}: {e}")
                    continue

                for edge in edges:
                    if edge.edge_type != "CALLS" or not edge.target_id:
                        continue
                    target = edge.target_id
                    if target not in seen:
                        self._add_external_node(graph, edge)
                        seen.add(target)
                        if depth + 1 < self.hops:
                            frontier.append((target, depth + 1))

                    if edge.direction == "outgoing":
                        self._connect(graph, nid, target)
                    else:
                        self._connect(graph, target, nid)
                    edge_count += 1

                graph.set_status_line(
                    f"{self.seed_file}: {len(graph.nodes)} nodes, {edge_count} call edges (hop {depth + 1}/{self.hops})"
                )

        graph.mark_plan_complete()
        graph.set_status_line(
            f"{self.seed_file}: {len(graph.nodes)} methods, {len(graph.edges)} CALLS — hops={self.hops}"
        )

    def _find_file(self, files):
        target = self.seed_file.replace("\\", "/").lower()
        for f in files:
            name = f.file_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if name == target or f.file_path.replace("\\", "/").lower().endswith("/" + target):
                return f
        return None

    async def _collect_methods(self, client: CodeClient, root_id: str) -> list[NodeChild]:
        """DFS the file's structure, collecting every method/constructor."""
        methods: list[NodeChild] = []
        stack: list[str] = [root_id]
        while stack:
            cur = stack.pop()
            children = await client.get_children(cur)
            for child in children:
                if child.node_type in _METHOD_KINDS:
                    methods.append(child)
                elif child.node_type in _STRUCTURAL_KINDS:
                    stack.append(child.id)
        return methods

    def _add_method(self, graph: Graph, node: NodeChild, *, seed: bool) -> None:
        short = _short_id(node.id)
        if short in graph.nodes:
            return
        graph.add_node(
            short, _node_kind(node.node_type).value,
            config={"seed": True} if seed else {},
            intent=node.name or node.node_type,
        )

    def _add_external_node(self, graph: Graph, edge: Edge) -> None:
        if not edge.target_id:
            return
        short = _short_id(edge.target_id)
        if short in graph.nodes:
            return
        kind = _node_kind(edge.target_type or "method")
        graph.add_node(
            short, kind.value,
            config={"nodeType": edge.target_type} if edge.target_type and kind == NodeKind.TASK else {},
            intent=edge.target_name or edge.target_type or short,
        )

    def _connect(self, graph: Graph, src_full: str, dst_full: str) -> None:
        src = _short_id(src_full)
        dst = _short_id(dst_full)
        if src not in graph.nodes or dst not in graph.nodes:
            return
        for e in graph.edges:
            if e.from_id == src and e.to_id == dst:
                return
        graph.connect(src, dst)
