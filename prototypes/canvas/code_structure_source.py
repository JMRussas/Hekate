"""CodeStructureSource — render the namespace/class/method tree of an
indexed codebase.

Walks each file's root node via /api/node/{id}/children, descends into
namespace → class/struct → method/field, and stops at the method body.
Block/statement nodes are skipped to keep the diagram readable; the
call-graph view (deferred) is where statement-level edges become useful.

Concurrency-bounded HTTP fan-out: a codebase produces hundreds of
requests, so we cap the in-flight count. The graph mutates progressively
as files come back, so the renderer shows partial results immediately.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from context_store_code import CodeClient, DEFAULT_CTX_STORE, NodeChild
from graph import Graph, NodeKind
from source import CanvasSource


_KIND_MAP = {
    "compilation_unit": NodeKind.COMPILATION_UNIT,
    "namespace":        NodeKind.NAMESPACE,
    "class":            NodeKind.CLASS,
    "struct":           NodeKind.STRUCT,
    "method":           NodeKind.METHOD,
    "field":            NodeKind.FIELD,
    "constructor":      NodeKind.CONSTRUCTOR,
    "parameter":        NodeKind.PARAMETER,
}
_SKIP_KINDS = {"block", "statement", "using_directive"}


def _short_id(uuid: str) -> str:
    return uuid.split("-", 1)[0]


def _label(child: NodeChild) -> str:
    return child.name or child.summary or child.node_type


class CodeStructureSource(CanvasSource):
    def __init__(
        self,
        project_name: str,
        *,
        base_url: str = DEFAULT_CTX_STORE,
        concurrency: int = 8,
    ):
        self.project_name = project_name
        self.base_url = base_url
        self.concurrency = concurrency
        self.title = f"code · structure · {project_name}"

    async def load(self, graph: Graph) -> None:
        async with CodeClient(self.base_url) as client:
            project = await client.get_project_by_name(self.project_name)
            files = await client.list_files(project.id)
            files = [f for f in files if f.root_node_id]

            if not files:
                graph.set_status_line(f"{project.name}: no decomposed files")
                return

            project_nid = _short_id(project.id)
            graph.add_node(
                project_nid, NodeKind.DIRECTORY.value,
                config={"files": len(files)},
                intent=project.name,
            )

            graph.set_status_line(
                f"{project.name}: walking {len(files)} files (concurrency={self.concurrency})"
            )

            sem = asyncio.Semaphore(self.concurrency)
            done = 0
            lock = asyncio.Lock()

            async def walk_file(f) -> None:
                nonlocal done
                async with sem:
                    file_nid = _short_id(f.id)
                    filename = f.file_path.replace("\\", "/").rsplit("/", 1)[-1]
                    graph.add_node(
                        file_nid, NodeKind.FILE.value,
                        config={"nodes": f.node_count},
                        intent=filename,
                        parent_id=project_nid,
                    )
                    try:
                        await self._walk_children(client, graph, f.root_node_id, file_nid)
                    except Exception as e:
                        graph.set_status(file_nid, "failed", error=f"{type(e).__name__}: {e}")
                async with lock:
                    done += 1
                    graph.set_status_line(
                        f"{project.name}: {done}/{len(files)} files walked"
                    )

            await asyncio.gather(*(walk_file(f) for f in files))

        graph.mark_plan_complete()
        graph.set_status_line(
            f"{project.name}: {len(files)} files, {len(graph.nodes) - 1} structure nodes"
        )

    async def _walk_children(
        self,
        client: CodeClient,
        graph: Graph,
        node_id: str,
        parent_short_id: str,
    ) -> None:
        children = await client.get_children(node_id)
        for child in children:
            if child.node_type in _SKIP_KINDS:
                continue
            kind = _KIND_MAP.get(child.node_type, NodeKind.TASK)
            short = _short_id(child.id)
            graph.add_node(
                short, kind.value,
                config={"nodeType": child.node_type} if kind == NodeKind.TASK else {},
                intent=_label(child),
                parent_id=parent_short_id,
            )
            if kind in (NodeKind.NAMESPACE, NodeKind.CLASS, NodeKind.STRUCT):
                await self._walk_children(client, graph, child.id, short)
