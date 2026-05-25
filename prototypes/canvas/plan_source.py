"""PlanSource — read a context-store plan and project it onto the canvas.

Maps the context store's structural nodeTypes (plan / plan_phase / task /
question / risk) directly onto our extended NodeKind enum, and projects
parent/child relationships as **containment** (node.parent_id), not as flow
edges. This is the change from the original hekate_viewer.py — now the
renderer shows nesting, not a flat tree.

Phase 1: load only. Phase 2 will add a subscribe() that subscribes to SSE
status events from the orchestration and emits set_status mutations.
"""
from __future__ import annotations

import os

import httpx

from graph import Graph, NodeKind, NodeStatus
from source import CanvasSource


DEFAULT_CTX_STORE = os.environ.get("CTX_STORE_URL", "http://192.168.1.164:5102")


_STATUS_MAP = {
    "pending":     NodeStatus.PLANNED,
    "ready":       NodeStatus.READY,
    "queued":      NodeStatus.READY,
    "running":     NodeStatus.RUNNING,
    "in_progress": NodeStatus.RUNNING,
    "dispatched":  NodeStatus.RUNNING,
    "completed":   NodeStatus.DONE,
    "done":        NodeStatus.DONE,
    "success":     NodeStatus.DONE,
    "failed":      NodeStatus.FAILED,
    "error":       NodeStatus.FAILED,
    "blocked":     NodeStatus.FAILED,
}


def _kind_from_node_type(node_type: str) -> NodeKind:
    """Map a context-store nodeType to a NodeKind. Unknowns fall back to TASK
    so the viewer stays robust as the context-store schema evolves."""
    try:
        return NodeKind(node_type)
    except ValueError:
        return NodeKind.TASK


def _status_from_attrs(node: dict) -> NodeStatus:
    raw = (node.get("attributes") or {}).get("status") or node.get("status") or ""
    return _STATUS_MAP.get(str(raw).lower(), NodeStatus.PLANNED)


def _short_id(uuid: str) -> str:
    return uuid.split("-", 1)[0]


def _intent_from(node: dict) -> str:
    name = node.get("name")
    if name:
        return name[:120]
    value = node.get("value") or ""
    return value.split("\n", 1)[0][:120]


class PlanSource(CanvasSource):
    def __init__(self, plan_id: str, *, base_url: str = DEFAULT_CTX_STORE):
        self.plan_id = plan_id
        self.base_url = base_url.rstrip("/")
        self.title = f"hekate — plan {plan_id[:8]}"

    async def load(self, graph: Graph) -> None:
        url = f"{self.base_url}/api/plan/{self.plan_id}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            plan = resp.json()

        name = (plan.get("name") or self.plan_id)[:80]
        self.title = f"hekate — {name}"

        self._walk(graph, plan, parent_short_id=None)
        graph.set_status_line(
            f"loaded plan {self.plan_id[:8]} — {len(graph.nodes)} nodes"
        )

    def _walk(self, graph: Graph, node: dict, parent_short_id: str | None) -> None:
        nid = _short_id(node["id"])
        kind = _kind_from_node_type(node.get("nodeType", "task"))

        attrs = node.get("attributes") or {}
        config: dict = {"nodeType": node.get("nodeType", "?")}
        for k in ("task_type", "complexity", "tier", "scope", "impact", "likelihood"):
            if k in attrs:
                config[k] = attrs[k]

        # Containment, not flow — the parent CONTAINS this node.
        graph.add_node(
            nid, kind.value, config,
            intent=_intent_from(node),
            parent_id=parent_short_id,
        )

        status = _status_from_attrs(node)
        if status != NodeStatus.PLANNED:
            value = (node.get("value") or "").strip()
            result = value if status == NodeStatus.DONE and value else None
            error = value if status == NodeStatus.FAILED and value else None
            graph.set_status(nid, status.value, result=result, error=error)

        for child in node.get("children", []):
            self._walk(graph, child, parent_short_id=nid)
