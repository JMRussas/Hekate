"""Capture RAW plan-view fixtures for ChatAgent's C1a plan-run projection test (ChatAgent msg 1818).
OFFLINE: the disposable harness database only (dropped at the end); no persistent DB, no model.

    uv run python tests/capture_planrun_fixtures.py <out-dir>

Each state gets its own import (A <- B <- C, Accepted gates, specs pending: the contract does not read them),
built ONLY through the plan contract routes. For each state it writes:
  <state>.plan.raw.json   the EXACT GET /api/plan-contract/v1/plans/{root} response bytes (never re-serialized)
  <state>.claim.json      the outcome of a claim attempted right AFTER that capture: {"outcome", "nodeKey"}
and MANIFEST.json (sha256 of every file, the node keys -> ids, and what each state is).
S4 (a claimed attempt with no worker ACK) is NOT expressible in PlanStore: an ACK is a journal fact, not a contract
fact, so at the contract level S4 equals S1. It is skipped and recorded as such.
"""

import hashlib
import json
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from e1 import plan_import as PI  # noqa: E402
from e1.harness import Harness  # noqa: E402
from e1.wire import SetupClient, SupervisorClient  # noqa: E402


def doc(title: str) -> bytes:
    return json.dumps({"version": PI.VERSION, "title": title, "nodes": [
        {"key": "a", "name": "A", "spec": None, "after": []},
        {"key": "b", "name": "B", "spec": None, "after": ["a"]},
        {"key": "c", "name": "C", "spec": None, "after": ["b"]}]}).encode("utf-8")


class Capture:
    def __init__(self, base_url: str, project: str, out: Path):
        self.s, self.c, self.project, self.out = SetupClient(base_url), SupervisorClient(base_url), project, out
        self.manifest: dict = {"states": {}, "files": {}}

    def plan(self, state: str, title: str) -> PI.ImportedPlan:
        p = PI.import_plan(self.s, self.project, doc(f"C1a fixture {title}"))
        self.manifest["states"][state] = {"root": p.root, "nodes": p.node_ids}
        return p

    def node(self, p, key):
        return next(n for n in self.s.plan(p.root).body["nodes"] if n["id"] == p.node_ids[key])

    def claim(self, p, key: str) -> dict:
        r = self.c.claim(p.root, f"cap-{uuid.uuid4().hex[:12]}", f"att-{key}-{uuid.uuid4().hex[:6]}", None, "c1a-capture").body["receipt"]
        assert r["outcome"] == "claimed" and r["nodeId"] == p.node_ids[key], r
        return r

    def finish(self, p, key: str, receipt: dict) -> None:
        n = self.node(p, key)
        r = self.c.transition(n["id"], {"to": "done", "attemptId": receipt["attemptId"], "attemptEpoch": receipt["attemptEpoch"],
                                        "artifactRef": f"artifact-{key}", "operationKey": uuid.uuid4().hex,
                                        "expectedStateRevision": n["stateRevision"], "actor": "c1a-capture"})
        assert r.status == 200, (r.status, r.code)

    def decide(self, p, key: str, receipt: dict, decision: str) -> None:
        n = self.node(p, key)
        r = self.s.decide(n["id"], {"decision": decision, "reviewedContentRevision": receipt["contentRevision"],
                                    "reviewedArtifactRef": f"artifact-{key}", "reviewedAttemptEpoch": receipt["attemptEpoch"],
                                    "evidenceRef": f"c1a-capture:{key}", "operationKey": uuid.uuid4().hex,
                                    "expectedStateRevision": n["stateRevision"], "actor": "c1a-capture"})
        assert r.status == 200, (r.status, r.code)

    def accept(self, p, key: str) -> None:
        rc = self.claim(p, key)
        self.finish(p, key, rc)
        self.decide(p, key, rc, "accepted")

    def snap(self, state: str, p) -> None:
        raw = self.s.plan(p.root).raw                                  # the EXACT response bytes
        claim = self.c.claim(p.root, f"probe-{uuid.uuid4().hex[:12]}", f"probe-{uuid.uuid4().hex[:6]}", None, "c1a-capture").body["receipt"]
        claim_doc = json.dumps({"outcome": claim["outcome"], "nodeKey": p.key_of(claim["nodeId"] or "")}, sort_keys=True).encode("utf-8") + b"\n"
        for name, data in ((f"{state}.plan.raw.json", raw), (f"{state}.claim.json", claim_doc)):
            (self.out / name).write_bytes(data)
            self.manifest["files"][name] = hashlib.sha256(data).hexdigest()


def main(out: Path) -> int:
    out.mkdir(parents=True)
    h = Harness()
    h.start()
    ok = False
    try:
        cap = Capture(h.base_url, h.project_id, out)
        # S1 -> S2 -> S5 on one root
        p = cap.plan("S1", "chain-1")
        cap.accept(p, "a")
        rb = cap.claim(p, "b")
        cap.snap("S1", p)                       # A accepted, B in progress, C blocked
        cap.manifest["states"]["S2"] = cap.manifest["states"]["S1"]
        cap.finish(p, "b", rb)
        cap.snap("S2", p)                       # B done awaiting review
        cap.decide(p, "b", rb, "accepted")
        cap.accept(p, "c")
        cap.manifest["states"]["S5"] = cap.manifest["states"]["S1"]
        cap.snap("S5", p)                       # all accepted
        # S3: A rejected
        p3 = cap.plan("S3", "chain-3")
        ra = cap.claim(p3, "a")
        cap.finish(p3, "a", ra)
        cap.decide(p3, "a", ra, "rejected")
        cap.snap("S3", p3)
        # S6: A accepted, then A's content revised (B blocked by a stale predecessor)
        p6 = cap.plan("S6", "chain-6")
        cap.accept(p6, "a")
        a = cap.node(p6, "a")
        r = cap.s.revise(a["id"], "revised after acceptance", a["contentRevision"], uuid.uuid4().hex, a["stateRevision"])
        cap.manifest["S6revise"] = {"status": r.status, "code": r.code}
        if r.status == 200:
            cap.snap("S6", p6)
        cap.manifest["S4"] = "skipped: a worker ACK is a supervisor-journal fact, not a PlanStore contract fact; at the contract level S4 equals S1"
        (out / "MANIFEST.json").write_text(json.dumps(cap.manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        ok = True
    finally:
        h.stop(keep_work=not ok)
    print(json.dumps(cap.manifest, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]).resolve()))
