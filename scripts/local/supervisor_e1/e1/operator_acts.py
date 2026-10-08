"""HK-ISSUE-010 (LOCAL; plan 043 rev 3, root msg 1837): named operator acts under an explicit, configured policy.

This replaces direct use of the test-fixture `SetupClient` by the local coordinator. It exposes ONLY the acts that
plan-run uses, each mapped to exactly one existing route, with no general passthrough:
  plan (read) | create_plan | add_child | add_dependency (import) | decide | transition (operator moves: only
  `cancelled` / `todo` = the HK-005 fix-round reset) | revise (pin_spec).
There is no `release_inflight`: uncertain or in-flight work is never released automatically (root msg 1892).
The policy names the operator actor, the ONE project and the allowed plan ROOTS, derived from the validated import
(PI.identities(plan file, project).root), never hand-edited (root msg 1895). A plan outside that project, a node not seen in a policy plan view, or another transition is refused
LOCALLY before any call. Every act is logged as an INTENT before the call and an OUTCOME after it (review 1894 D7);
an intent without an outcome is an uncertain act, listed by uncertain_acts() and never silently retried.

BOUNDARY: a trusted, single, local OS account on loopback. This is NOT server authentication: PlanStore still
trusts loopback, and `actor` is a label, not a credential. There is no secret or HMAC here (root msg 1837).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

OPERATOR_TRANSITIONS = ("cancelled", "todo")


class OperatorRefused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(f"{code}: {detail}" if detail is not None else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class OperatorPolicy:
    actor: str                 # a LABEL recorded with each act (not a credential)
    project_id: str            # the ONE project (the coordinator marker's project)
    roots: frozenset[str]      # the plan roots allowed: DERIVED from the validated import (root msg 1895), never hand-edited

    def __post_init__(self):
        uuid.UUID(self.project_id)
        if not isinstance(self.actor, str) or not 1 <= len(self.actor) <= 64:
            raise OperatorRefused("policy_actor")
        if not self.roots or not all(isinstance(r, str) and str(uuid.UUID(r)) == r for r in self.roots):
            raise OperatorRefused("policy_roots")


class OperatorSurface:
    def __init__(self, setup, policy: OperatorPolicy, log_path: Path):
        self._setup, self.policy, self._log = setup, policy, Path(log_path)
        self._root_of: dict[str, str] = {}            # node id -> root id, learned only from policy-project plan views

    def _write(self, entry: dict[str, Any]) -> None:
        self._log.parent.mkdir(parents=True, exist_ok=True)
        with open(self._log, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(dict(entry, at=datetime.now(timezone.utc).isoformat(), actor=self.policy.actor), sort_keys=True))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())                     # the intent is DURABLE before the HTTP call (root msg 1904)

    def _act(self, act: str, target: str, call: Callable[[], Any], **extra) -> Any:
        act_id = uuid.uuid4().hex
        self._write({"phase": "intent", "id": act_id, "act": act, "target": target, **extra})
        r = call()
        self._write({"phase": "outcome", "id": act_id, "act": act, "target": target, "status": r.status})
        return r

    def uncertain_acts(self) -> list[dict[str, Any]]:
        """Everything the operator must classify before any dispatch (root msg 1904): intents with no outcome (a
        crash between the two) and partial or unparseable lines (a crash mid-write). Never retried automatically."""
        if not self._log.exists():
            return []
        rows, bad = [], []
        for i, line in enumerate(self._log.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
                if not isinstance(r, dict) or r.get("phase") not in ("intent", "outcome") or not isinstance(r.get("id"), str):
                    raise ValueError
                rows.append(r)
            except ValueError:
                bad.append({"phase": "unparseable", "line": i, "text": line[:200]})
        done = {r["id"] for r in rows if r["phase"] == "outcome"}
        return bad + [r for r in rows if r["phase"] == "intent" and r["id"] not in done]

    def _known(self, node: str) -> str:
        root = self._root_of.get(node)
        if root is None:
            raise OperatorRefused("node_outside_policy", node)
        return root

    # -- reads ---------------------------------------------------------------------------------------------------
    def plan(self, root: str):
        if root not in self.policy.roots:
            raise OperatorRefused("root_outside_policy", root)
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
        if root not in self.policy.roots:
            raise OperatorRefused("root_outside_policy", root)
        r = self._act("create_plan", root, lambda: self._setup.create_plan(root, project, name, key))
        if r.status == 200:
            self._root_of[root] = root
        return r

    def add_child(self, parent: str, child: str, name: str, order: int, key: str, rev: int, value=None, attributes=None):
        root = self._known(parent)
        r = self._act("add_child", child, lambda: self._setup.add_child(parent, child, name, order, key, rev, value=value,
                                                                        attributes=attributes), parent=parent)
        if r.status == 200:
            self._root_of[child] = root
        return r

    def add_dependency(self, successor: str, predecessor: str, key: str, rev: int, gate: str | None = None):
        if self._known(successor) != self._known(predecessor):
            raise OperatorRefused("edge_across_plans", [successor, predecessor])
        return self._act("add_dependency", successor, lambda: self._setup.add_dependency(successor, predecessor, key, rev, gate),
                         predecessor=predecessor)

    # -- operator acts -----------------------------------------------------------------------------------------------
    def decide(self, node: str, payload: dict[str, Any]):
        self._known(node)
        return self._act("decide", node, lambda: self._setup.decide(node, dict(payload, actor=self.policy.actor)),
                         decision=payload.get("decision"), evidenceRef=payload.get("evidenceRef"))

    def transition(self, node: str, payload: dict[str, Any]):
        self._known(node)
        if payload.get("to") not in OPERATOR_TRANSITIONS:
            raise OperatorRefused("transition_not_an_operator_act", payload.get("to"))
        return self._act("transition", node, lambda: self._setup.transition(node, dict(payload, actor=self.policy.actor)),
                         to=payload.get("to"))

    def revise(self, node: str, value: str, expected_content_rev: int, key: str, rev: int):
        self._known(node)
        return self._act("revise", node, lambda: self._setup.revise(node, value, expected_content_rev, key, rev), value=value[:200])
