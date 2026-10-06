"""Loopback-only HTTP for the plan-contract API (plan 023 E1a; test-only).

Clients expose bounded capabilities only. SupervisorClient is the supervisor's whole write
surface (claim, finish/release transitions) plus the reads it needs; SetupClient is the
TEST FIXTURE's broader surface (create, add, revise, decide) and is never given to the
supervisor.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from .exact import loads_exact

PREFIX = "/api/plan-contract/v1"


@dataclass(frozen=True)
class Response:
    status: int
    raw: bytes
    body: Any  # parsed with loads_exact; None for an empty body

    @property
    def code(self) -> str | None:
        return self.body.get("code") if isinstance(self.body, dict) else None


def seg(value: str) -> str:
    """One URL path segment, fully escaped."""
    return quote(value, safe="")


class _Http:
    def __init__(self, base_url: str):
        parts = urlsplit(base_url)
        if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(f"loopback http only, got {base_url!r}")
        self._base = base_url.rstrip("/")

    def _send(self, method: str, path: str, payload: Any | None = None) -> Response:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self._base + PREFIX + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        return Response(status, raw, loads_exact(raw) if raw.strip() else None)


class SupervisorClient(_Http):
    """The supervisor's complete API surface. No create/revise/decide/dependency methods."""

    def claim(self, root: str, claim_key: str, attempt_id: str, executor_ref: str | None, actor: str) -> Response:
        body = {"claimKey": claim_key, "attemptId": attempt_id, "actor": actor}
        if executor_ref is not None:
            body["executorRef"] = executor_ref
        return self._send("POST", f"/plans/{seg(root)}/claims", body)

    def get_claim(self, root: str, claim_key: str) -> Response:
        return self._send("GET", f"/plans/{seg(root)}/claims/{seg(claim_key)}")

    def get_plan(self, root: str) -> Response:
        return self._send("GET", f"/plans/{seg(root)}")

    def transition(self, node: str, payload: dict[str, Any]) -> Response:
        if payload.get("to") not in ("done", "todo"):
            raise ValueError("the supervisor may only finish (done) or release (todo)")
        return self._send("POST", f"/nodes/{seg(node)}/transition", payload)


class SetupClient(_Http):
    """Test-fixture surface: builds plans and plays operator/verifier. Never handed to the supervisor."""

    def create_plan(self, root: str, project: str, name: str, key: str) -> Response:
        return self._send("POST", "/plans", {"rootId": root, "projectId": project, "name": name,
                                             "operationKey": key, "expectedStateRevision": 0, "actor": "e1-setup"})

    def add_child(self, parent: str, child: str, name: str, order: int, key: str, rev: int,
                  value: str | None = None, attributes: dict[str, str] | None = None) -> Response:
        body: dict[str, Any] = {"childId": child, "nodeType": "task", "name": name, "siblingOrder": order,
                                "operationKey": key, "expectedStateRevision": rev, "actor": "e1-setup"}
        if value is not None:
            body["value"] = value
        if attributes is not None:
            body["attributes"] = attributes
        return self._send("POST", f"/nodes/{seg(parent)}/children", body)

    def add_dependency(self, successor: str, predecessor: str, key: str, rev: int, gate: str | None = None) -> Response:
        body: dict[str, Any] = {"predecessorId": predecessor, "operationKey": key, "expectedStateRevision": rev, "actor": "e1-setup"}
        if gate is not None:
            body["gate"] = gate
        return self._send("POST", f"/nodes/{seg(successor)}/dependencies", body)

    def transition(self, node: str, payload: dict[str, Any]) -> Response:
        return self._send("POST", f"/nodes/{seg(node)}/transition", payload)

    def decide(self, node: str, payload: dict[str, Any]) -> Response:
        return self._send("POST", f"/nodes/{seg(node)}/decide", payload)

    def revise(self, node: str, value: str, expected_content_rev: int, key: str, rev: int) -> Response:
        return self._send("PUT", f"/nodes/{seg(node)}/content", {"value": value, "expectedContentRevision": expected_content_rev,
                                                                  "operationKey": key, "expectedStateRevision": rev, "actor": "e1-setup"})

    def plan(self, root: str) -> Response:
        return self._send("GET", f"/plans/{seg(root)}")

    def events(self, node: str) -> Response:
        return self._send("GET", f"/nodes/{seg(node)}/events")
