"""Graph data model + mutation ops.

The agent's planning output channel is mutations on this graph. Every plan and
every repair flows through these few operations — there is no other way to
express intent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class NodeKind(str, Enum):
    # Behavioral kinds — used by the planning agent for execution-time intent.
    TOOL_CALL = "tool_call"
    SUB_AGENT = "sub_agent"
    TRANSFORM = "transform"
    CHECK = "check"
    # Structural kinds — mirror the context store's plan ontology so the
    # Hekate viewer can render real plans without a translation layer.
    PLAN = "plan"
    PLAN_PHASE = "plan_phase"
    TASK = "task"
    QUESTION = "question"
    RISK = "risk"
    # Filesystem kinds — synthetic, used by CodeFileSource to nest files by
    # directory when the context store only stores flat absolute paths.
    DIRECTORY = "directory"
    FILE = "file"
    # Code kinds — mirror the context store's C# decomposer ontology.
    COMPILATION_UNIT = "compilation_unit"
    NAMESPACE = "namespace"
    CLASS = "class"
    STRUCT = "struct"
    METHOD = "method"
    FIELD = "field"
    CONSTRUCTOR = "constructor"
    PARAMETER = "parameter"


class NodeStatus(str, Enum):
    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Node:
    id: str
    kind: NodeKind
    config: dict
    intent: str = ""
    status: NodeStatus = NodeStatus.PLANNED
    result: Optional[str] = None
    error: Optional[str] = None
    # Containment: a node optionally lives *inside* another node. This
    # captures plan->phase->task hierarchies, code package->class->method,
    # service->component, and so on. None means the node is a containment
    # root (no parent).
    parent_id: Optional[str] = None


@dataclass
class Edge:
    """A flow edge (causal/dependency). Containment is NOT represented here —
    it lives on Node.parent_id. Flow edges can cross containment boundaries."""
    from_id: str
    to_id: str
    from_port: str = "out"   # "out" | "result" | "error" | "pass" | "fail"


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    plan_complete: bool = False
    status_line: str = ""   # short agent-state string for the renderer header
    version: int = 0        # bumped on every mutation so renderers can poll cheaply

    # ---- mutations (the agent's tool surface) ----

    def add_node(
        self,
        id: str,
        kind: str,
        config: dict,
        intent: str = "",
        parent_id: Optional[str] = None,
    ) -> None:
        if id in self.nodes:
            raise ValueError(f"node {id!r} already exists")
        if parent_id is not None and parent_id not in self.nodes:
            raise ValueError(f"unknown containment parent {parent_id!r}")
        self.nodes[id] = Node(
            id=id, kind=NodeKind(kind), config=config, intent=intent,
            parent_id=parent_id,
        )
        self._bump()

    def connect(self, from_id: str, to_id: str, from_port: str = "out") -> None:
        if from_id not in self.nodes:
            raise ValueError(f"unknown source node {from_id!r}")
        if to_id not in self.nodes:
            raise ValueError(f"unknown target node {to_id!r}")
        self.edges.append(Edge(from_id=from_id, to_id=to_id, from_port=from_port))
        self._bump()

    def remove_node(self, id: str) -> None:
        """Remove a node, its incident flow edges, and recursively any
        contained descendants (a parent owns its sub-graph)."""
        if id not in self.nodes:
            return
        # Snapshot containment children before we mutate.
        for child_id in [cid for cid, n in self.nodes.items() if n.parent_id == id]:
            self.remove_node(child_id)
        del self.nodes[id]
        self.edges = [e for e in self.edges if e.from_id != id and e.to_id != id]
        self._bump()

    def replace_node(self, id: str, kind: str, config: dict) -> None:
        if id not in self.nodes:
            raise ValueError(f"unknown node {id!r}")
        old = self.nodes[id]
        self.nodes[id] = Node(
            id=id, kind=NodeKind(kind), config=config, intent=old.intent,
        )
        self._bump()

    def annotate(self, id: str, intent: str) -> None:
        self.nodes[id].intent = intent
        self._bump()

    def mark_plan_complete(self) -> None:
        self.plan_complete = True
        self._bump()

    def set_status_line(self, s: str) -> None:
        self.status_line = s
        self._bump()

    # ---- runtime status (emitted by the executor, not the agent) ----

    def set_status(
        self,
        id: str,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        n = self.nodes[id]
        n.status = NodeStatus(status)
        if result is not None:
            n.result = result
        if error is not None:
            n.error = error
        self._bump()

    # ---- queries: flow (edges) ----

    def parents(self, id: str) -> list[tuple[str, str]]:
        """Flow parents — nodes with an outgoing edge to this one."""
        return [(e.from_id, e.from_port) for e in self.edges if e.to_id == id]

    def children(self, id: str) -> list[tuple[str, str]]:
        """Flow children — nodes this one has an edge to."""
        return [(e.to_id, e.from_port) for e in self.edges if e.from_id == id]

    def roots(self) -> list[str]:
        """Flow roots — nodes with no incoming flow edges."""
        has_parent = {e.to_id for e in self.edges}
        return [nid for nid in self.nodes if nid not in has_parent]

    # ---- queries: containment (parent_id) ----

    def contained(self, id: str) -> list[str]:
        """Direct containment children of `id`, in graph insertion order."""
        return [nid for nid, n in self.nodes.items() if n.parent_id == id]

    def descendants(self, id: str) -> list[str]:
        """All transitively contained nodes (DFS, insertion-order)."""
        out: list[str] = []
        stack = [id]
        # `id` itself is excluded; we walk its sub-graph only.
        while stack:
            cur = stack.pop()
            for cid in self.contained(cur):
                out.append(cid)
                stack.append(cid)
        return out

    def containment_roots(self) -> list[str]:
        """Top-level nodes (no containment parent), in insertion order."""
        return [nid for nid, n in self.nodes.items() if n.parent_id is None]

    def depth(self, id: str) -> int:
        """Containment depth — 0 for root, 1 for direct child of a root, etc."""
        d = 0
        cur = self.nodes[id].parent_id
        while cur is not None:
            d += 1
            cur = self.nodes[cur].parent_id
        return d

    def topo_order(self) -> list[str]:
        """Kahn's algorithm. Stable on insertion order within a layer."""
        in_deg = {nid: 0 for nid in self.nodes}
        for e in self.edges:
            in_deg[e.to_id] = in_deg.get(e.to_id, 0) + 1
        ready = [nid for nid in self.nodes if in_deg[nid] == 0]
        out: list[str] = []
        while ready:
            nid = ready.pop(0)
            out.append(nid)
            for child_id, _ in self.children(nid):
                in_deg[child_id] -= 1
                if in_deg[child_id] == 0:
                    ready.append(child_id)
        # Cycles: append any leftovers so they still render
        for nid in self.nodes:
            if nid not in out:
                out.append(nid)
        return out

    def _bump(self) -> None:
        self.version += 1
