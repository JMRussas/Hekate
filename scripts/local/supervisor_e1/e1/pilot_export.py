"""OPT-IN real-H1 review source and export hook for the pilot (plan 1532; shared contract 1549/1550). TEST-SCOPED.

- RealH1ReviewSource: the review handoff's Task is the PINNED real ChatAgent H1 (e1/h1_bridge, ChatAgent
  5255daa + Node 24.21.0) rendered on the RETAINED raw claim response bytes, exactly as the accepted
  interop_live test does; the composition re-runs the same pinned H1. It is labelled "chatagent-h1" ONLY
  when its builder IS e1.h1_bridge.build (identity, not a name) AND the bridge's verified runtime was
  captured; an injected builder is labelled "injected" and exportable only by tests (review 1593 #1).
- Exporter: the post-compose hook. Per round it writes ONE handoff-export.v0 directory
  (<out_root>/export-r<n>) with wanted=[] and retrieval=[]. Its provenance is DERIVED, never typed in
  (review 1593 #2): worker from that round's journal `exited` record + the host-declared execution kind;
  h1Bridge from the bridge's verified checkout/runtime; hekateCommit/clean from a controlled git query.
"""

from __future__ import annotations

import datetime
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from e1 import cli_worker as W
from e1 import export as X
from e1 import pilot as P

RULES = [{"path": "AGENTS.md", "revision": "pilot-export-v0", "text": "Review the artifact against the criteria; report evidence."}]
SYSTEM = "You are an independent reviewer of one Hekate pilot artifact."
FAST, DEEP = "Check the criteria.", "Check every criterion and cite the evidence."
BUDGET = {"windowTokens": 200_000, "maxHistoryTurns": 0, "safetyTokens": 0, "fastOutputTokens": 1000, "deepOutputTokens": 1000}
INJECTED = "injected-h1-NOT-chatagent"
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def utc_now_iso() -> str:
    """The ACTUAL capture time (review 1593 #4), second precision, UTC, ISO 8601 with Z."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class RealH1ReviewSource(P.ReviewTaskSource):
    """The review Task = the pinned real H1 output on the retained claim bytes (interop_live precedent)."""

    def __init__(self, build=None):
        from e1 import h1_bridge                                   # imported only on the opt-in path
        self._pinned = h1_bridge.build
        self._build = build if build is not None else h1_bridge.build
        self.runtime: dict[str, Any] | None = None
        self.captured_at: str | None = None

    @property
    def is_real(self) -> bool:
        """IDENTITY with the pinned bridge AND its verified runtime captured; never a name or a flag."""
        return self._build is self._pinned and isinstance(self.runtime, dict) and bool(self.runtime.get("h1Commit"))

    @property
    def name(self) -> str:                                          # read by the pilot at export time
        return X.REAL_H1 if self.is_real else INJECTED

    def options(self, claim_raw: bytes) -> dict[str, Any]:
        if self.captured_at is None:
            self.captured_at = utc_now_iso()
        return {"response": claim_raw.decode("utf-8"), "rules": RULES, "systemInstruction": SYSTEM,
                "roleInstructions": {"fast": FAST, "deep": DEEP}, "budget": BUDGET, "capturedAtIso": self.captured_at}

    def task_and_options(self, *, n, art, cfg, package_ref, claim_raw):
        self.captured_at = None                                     # one actual capture time per prepare
        opts = self.options(claim_raw)
        res = self._build(opts)
        if not (isinstance(res, dict) and res.get("ok") is True and isinstance(res.get("text"), str)
                and res.get("context", {}).get("messages") == [{"role": "user", "content": res["text"]}]):
            raise RuntimeError("real H1 did not render the review task")
        self.runtime = res.get("_runtime") if isinstance(res.get("_runtime"), dict) else None
        return {"text": res["text"], "instructions": {"system": SYSTEM, "fast": FAST, "deep": DEEP},
                "packageRef": package_ref}, opts

    def builder(self, task_text: str):
        return self._build                                          # the composition re-runs the same H1

    def bridge(self) -> dict[str, Any]:
        """h1Bridge DERIVED from the bridge's verified runtime (h1_bridge.verify_checkout + node_runtime)."""
        if not self.is_real:
            return {"chatagentCommit": None, "nodeVersion": None}
        return {"chatagentCommit": self.runtime["h1Commit"], "nodeVersion": self.runtime["version"]}


def git_identity(repo: Path) -> tuple[str, bool]:
    """(HEAD sha, clean incl. UNTRACKED files) of the producing checkout, via the controlled git env.
    Refuses unless HEAD is a full 40-hex sha (review 1593 #3)."""
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, env=W.git_env())
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=normal"], capture_output=True,
                            text=True, env=W.git_env())
    sha = head.stdout.strip()
    if head.returncode != 0 or status.returncode != 0 or not SHA40.fullmatch(sha):
        raise X.ExportRefused("hekate_identity")
    return sha, not status.stdout.strip()


def worker_from_journal(dsn: str, root: str, claim_key: str, execution_kind: str) -> dict[str, Any]:
    """The round's worker, DERIVED from its journal `exited` record and the host-declared kind."""
    import psycopg
    if execution_kind not in P.EXECUTION_KINDS:
        raise X.ExportRefused("execution_kind")
    with psycopg.connect(dsn) as c:
        rows = c.execute("SELECT data FROM supervisor_journal.records WHERE root = %s AND claim_key = %s AND kind = 'exited' "
                         "ORDER BY seq", (root, claim_key)).fetchall()
    data = rows[-1][0] if rows else None
    if isinstance(data, str):
        import json
        data = json.loads(data)
    if execution_kind == "simulated":
        if rows:
            raise X.ExportRefused("worker_mismatch", "a simulated round has no adapter `exited` record")
        return {"kind": "fake", "requestedModel": None, "reportedModels": [], "reportedModelsAuthenticated": False}
    if not isinstance(data, dict) or "requestedModel" not in data or "reportedModels" not in data:
        raise X.ExportRefused("worker_unrecorded", "no journal `exited` record with model fields for this round")
    return {"kind": execution_kind if execution_kind in P.REAL_KINDS else "fake", "requestedModel": data["requestedModel"],
            "reportedModels": list(data["reportedModels"]), "reportedModelsAuthenticated": False}


@dataclass
class Exporter:
    """The post-compose export hook. Every provenance field is derived (see module docstring)."""
    out_root: Path
    run_id: str
    hekate_repo: Path
    dsn: str
    execution_kind: str
    source: P.ReviewTaskSource
    reviewer_kind: str = "deterministic-verifier"
    test_only: bool = False
    published: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, ctx: P.ExportContext) -> None:
        real = ctx.review_source == X.REAL_H1 and getattr(self.source, "is_real", False)
        if not real and not self.test_only:
            raise X.ExportRefused("h1_builder", "only a pinned real-H1 composition is exportable outside tests")
        commit, clean = git_identity(self.hekate_repo)
        ident = ctx.fresh.review_identity
        declared = {
            "hekateCommit": commit, "hekateTreeClean": clean, "runId": self.run_id, "planRoot": ident["rootId"],
            "nodeId": ident["nodeId"], "attemptId": ident["attemptId"], "attemptEpoch": ident["attemptEpoch"],
            "candidateDigest": ctx.delivery.candidate_digest, "recordId": ctx.fresh.record_id,
            "viewDigest": ctx.composition.view_digest,
            "h1Bridge": self.source.bridge() if hasattr(self.source, "bridge") else {"chatagentCommit": None, "nodeVersion": None},
            "worker": worker_from_journal(self.dsn, ident["rootId"], ctx.record.claim_key, self.execution_kind),
            "reviewer": {"kind": self.reviewer_kind, "inputSha256": None},   # a deterministic verifier: offline only
            "synthetic": not real,
        }
        inp = X.ExportInputs(ctx.delivery, ctx.fresh, ctx.policy, ctx.destination, ctx.composition,
                             X.REAL_H1 if real else X.STUB_H1, declared)
        out = Path(self.out_root) / f"export-r{ctx.round}"
        hashes = X.publish(out, X.export_files(inp, allow_stub=self.test_only))
        self.published.append({"round": ctx.round, "dir": str(out), "indexSha256": hashes[X.INDEX],
                               "provenanceSha256": hashes["provenance.json"], "viewDigest": ctx.composition.view_digest})
