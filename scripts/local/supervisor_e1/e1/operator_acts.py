"""HK-ISSUE-010 (LOCAL; plan 043 rev 3, root msg 1837): named operator acts under an explicit, configured policy.

This replaces direct use of the test-fixture `SetupClient` by the local coordinator. It exposes ONLY the acts that
plan-run uses, each mapped to exactly one existing route, with no general passthrough:
  plan (read) | create_plan | add_child | add_dependency (import) | decide | transition (operator moves: only
  `cancelled` / `todo`, the HK-005 fix-round reset) | revise (pin_spec).
The policy names the operator actor and the ONE project. A plan outside that project, a node not seen in a policy
plan view, or another transition is refused LOCALLY before any call. Every act is appended to a local JSONL log.

BOUNDARY: a trusted, single, local OS account on loopback. This is NOT server authentication: PlanStore still
trusts loopback, and `actor` is a label, not a credential. There is no secret or HMAC here (root msg 1837).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OPERATOR_TRANSITIONS = ("cancelled", "todo")


class OperatorRefused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(f"{code}: {detail}" if detail is not None else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class OperatorPolicy:
    actor: str                 # a LABEL recorded with each act (not a credential)
    project_id: str            # the ONE project this coordinator may act in

    def __post_init__(self):
        uuid.UUID(self.project_id)
        if not isinstance(self.actor, str) or not 1 <= len(self.actor) <= 64:
            raise OperatorRefused("policy_actor")


class OperatorSurface:
    def __init__(self, setup, policy: OperatorPolicy, log_path: Path):
        self._setup, self.policy, self._log = setup, policy, Path(log_path)
        self._root_of: dict[str, str] = {}            # node id -> root id, learned only from policy-project plan views

    def _record(self, act: str, target: str, status: int | None, **extra) -> None:
        self._log.parent.mkdir(parents=True, exist_ok=True)
        with open(self._log, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "actor": self.policy.actor, "act": act,
                                "target": target, "status": status, **extra}, sort_keys=True) + "\n")

    def _known(self, node: str) -> str:
        root = self._root_of.get(node)
        if root is None:
            raise OperatorRefused("node_outside_policy", node)
        return root

    # -- reads ---------------------------------------------------------------------------------------------------
    def plan(self, root: str):
        r = self._setup.plan(root)
        if r.status == 200:
            if r.body.get("projectId") != self.policy.project_id:
                raise OperatorRefused("plan_outside_policy", root)
            for n in r.body["nodes"]:
                self._root_of[n["id"]] = root
        return r

    # -- import ----------------------------------------------------------------------------------------------------
    def create_plan(self, root: str, project: str, name: str, key: str):
        if project != self.policy.project_id:
            raise OperatorRefused("project_outside_policy", project)
        r = self._setup.create_plan(root, project, name, key)
        if r.status == 200:
            self._root_of[root] = root
        self._record("create_plan", root, r.status)
        return r

    def add_child(self, parent: str, child: str, name: str, order: int, key: str, rev: int, value=None, attributes=None):
        root = self._known(parent)
        r = self._setup.add_child(parent, child, name, order, key, rev, value=value, attributes=attributes)
        if r.status == 200:
            self._root_of[child] = root
        self._record("add_child", child, r.status, parent=parent)
        return r

    def add_dependency(self, successor: str, predecessor: str, key: str, rev: int, gate: str | None = None):
        if self._known(successor) != self._known(predecessor):
            raise OperatorRefused("edge_across_plans", [successor, predecessor])
        r = self._setup.add_dependency(successor, predecessor, key, rev, gate)
        self._record("add_dependency", successor, r.status, predecessor=predecessor)
        return r

    # -- operator acts -----------------------------------------------------------------------------------------------
    def decide(self, node: str, payload: dict[str, Any]):
        self._known(node)
        r = self._setup.decide(node, dict(payload, actor=self.policy.actor))
        self._record("decide", node, r.status, decision=payload.get("decision"), evidenceRef=payload.get("evidenceRef"))
        return r

    def transition(self, node: str, payload: dict[str, Any]):
        self._known(node)
        if payload.get("to") not in OPERATOR_TRANSITIONS:
            raise OperatorRefused("transition_not_an_operator_act", payload.get("to"))
        r = self._setup.transition(node, dict(payload, actor=self.policy.actor))
        self._record("transition", node, r.status, to=payload.get("to"))
        return r

    def revise(self, node: str, value: str, expected_content_rev: int, key: str, rev: int):
        self._known(node)
        r = self._setup.revise(node, value, expected_content_rev, key, rev)
        self._record("revise", node, r.status, value=value[:200])
        return r
