"""Async client for the context-store endpoints CodeFileSource and
CodeStructureSource use. Thin — just typed wrappers over httpx with the
shared base URL and a sensible default timeout."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import httpx


DEFAULT_CTX_STORE = os.environ.get("CTX_STORE_URL", "http://192.168.1.164:5102")


@dataclass
class Project:
    id: str
    name: str
    root_path: str


@dataclass
class CodeFile:
    id: str
    file_path: str
    root_node_id: Optional[str]
    node_count: int


@dataclass
class NodeChild:
    id: str
    node_type: str
    name: Optional[str]
    summary: Optional[str]
    sibling_order: int


@dataclass
class Edge:
    edge_type: str
    target_id: Optional[str]
    target_name: Optional[str]
    target_type: Optional[str]
    direction: str  # "outgoing" or "incoming"


class CodeClient:
    def __init__(self, base_url: str = DEFAULT_CTX_STORE, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "CodeClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def get_project_by_name(self, name: str) -> Project:
        resp = await self._client.get(f"{self.base_url}/api/projects", params={"name": name})
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, list):
            if not body:
                raise LookupError(f"no project named {name!r}")
            body = body[0]
        if isinstance(body, dict) and "error" in body:
            raise LookupError(body["error"])
        return Project(id=body["id"], name=body["name"], root_path=body.get("rootPath") or "")

    async def list_files(self, project_id: str) -> list[CodeFile]:
        resp = await self._client.get(f"{self.base_url}/api/code/files/{project_id}")
        resp.raise_for_status()
        return [
            CodeFile(
                id=f["id"],
                file_path=f["filePath"],
                root_node_id=f.get("rootNodeId"),
                node_count=int(f.get("nodeCount", 0)),
            )
            for f in resp.json()
        ]

    async def get_children(self, node_id: str) -> list[NodeChild]:
        resp = await self._client.get(f"{self.base_url}/api/node/{node_id}/children")
        resp.raise_for_status()
        return [
            NodeChild(
                id=n["id"],
                node_type=n["nodeType"],
                name=n.get("name"),
                summary=n.get("summary"),
                sibling_order=int(n.get("siblingOrder", 0)),
            )
            for n in resp.json()
        ]

    async def get_edges(self, node_id: str) -> list[Edge]:
        resp = await self._client.get(f"{self.base_url}/api/node/{node_id}/edges")
        resp.raise_for_status()
        return [
            Edge(
                edge_type=e["edgeType"],
                target_id=e.get("targetId"),
                target_name=e.get("targetName"),
                target_type=e.get("targetType"),
                direction=e["direction"],
            )
            for e in resp.json()
        ]
