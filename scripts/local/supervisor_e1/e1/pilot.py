"""Pilot DRY-RUN driver (HK-ISSUE-006; TEST-ONLY, disposable harness database). Composes the ACCEPTED
primitives into one supervised loop without changing any of them:

  claim -> dispatch (E2c) -> simulated worker (ACK/progress acts, artifact) -> finish
  -> review request -> fresh-conversation handoff (E2d review-lead rollover; delivery verified and
  revalidated by the accepted E2e consumer) -> simulated INDEPENDENT reviewer -> decide
  -> on reject, a bounded fix round -> accept or needs_operator.

Dry run only: the worker and the reviewer are in-process callables; nothing launches a process,
CLI or model, and nothing is committed or pushed. Every uncertain or non-success case stops as
`needs_operator` and writes nothing further.

Fix rounds use the HK-ISSUE-005 WORKAROUND, accepted for the dry run only (msg 1440): a rejected done
node goes done -> cancelled -> todo and is claimed again under a NEW claimKey (a new journal stream).
This is not a reopen-via-claim capability. Rounds are linked explicitly: round n's task text and its
run log name round n-1's claimKey, attempt epoch, artifact and decision evidence.

HK-ISSUE-012 (fixed by plan 038 in acts.review_state; PlanStore unchanged): the round-1 decision stays
on the node as history, and a valid decision from a strictly older attempt epoch now leaves round 2 a
review candidate. The `prior_decision_blocks_review` stop below remains as a fail-closed diagnostic for
any older decision the guard does not prove (malformed or same/future epoch).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from e1 import acts as A
from e1 import consumer as C
from e1 import consumer_durable as CD
from e1 import handoff as H
from e1.acts_durable import ActsJournal
from e1.handoff_durable import commit as handoff_commit
from e1.handoff_durable import prepare as handoff_prepare
from e1.wire import SetupClient, SupervisorClient

INSTRUCTIONS = {"system": "You are the pilot worker.", "fast": "Be brief.", "deep": "Be thorough."}
REVIEW_INSTRUCTIONS = {"system": "You are an independent reviewer.", "fast": "Check the criteria.", "deep": "Check every criterion."}
H1_BUDGET = {"windowTokens": 200_000, "maxHistoryTurns": 0, "safetyTokens": 0, "fastOutputTokens": 1000, "deepOutputTokens": 1000}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
MAX_ROUNDS_LIMIT = 3


class PilotRefused(Exception):
    """A typed refusal BEFORE any write (configuration)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- configuration ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PilotConfig:
    repo: Path                      # a git work tree
    base: str                       # any revision; resolved to a full commit SHA
    workspace: Path                 # an existing directory; the driver owns only workspace/pilot-<runId>/
    project_id: str                 # PlanStore project of the (disposable) plan
    task_text: str
    criteria: str
    max_rounds: int = 2
    supervisor: str = "supervisor-pilot"
    worker: str = "worker-a"
    lead: str = "lead-1"
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass(frozen=True)
class Resolved:
    cfg: PilotConfig
    base_sha: str
    run_dir: Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)


def _git_out(repo: Path, *args: str) -> str:
    p = _git(repo, *args)
    return p.stdout.strip() if p.returncode == 0 else ""


def resolve(cfg: PilotConfig) -> Resolved:
    """Validate every input; refuse (typed) before anything is written anywhere."""
    if not (isinstance(cfg.max_rounds, int) and not isinstance(cfg.max_rounds, bool) and 1 <= cfg.max_rounds <= MAX_ROUNDS_LIMIT):
        raise PilotRefused("config_rounds", f"max_rounds must be 1..{MAX_ROUNDS_LIMIT}")
    for name in ("supervisor", "worker", "lead"):
        if not A.PRINCIPAL.fullmatch(getattr(cfg, name)):
            raise PilotRefused("config_principal", name)
    if not re.fullmatch(r"[a-z0-9]{1,32}", cfg.run_id):
        raise PilotRefused("config_run_id")
    if not cfg.task_text.strip() or not cfg.criteria.strip():
        raise PilotRefused("config_task")
    try:
        uuid.UUID(cfg.project_id)
    except (ValueError, TypeError):
        raise PilotRefused("config_project") from None
    repo = Path(cfg.repo)
    if not repo.is_dir() or _git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise PilotRefused("config_repo", "not a git work tree")
    got = _git(repo, "rev-parse", "--verify", "--quiet", f"{cfg.base}^{{commit}}")
    base_sha = got.stdout.strip()
    if got.returncode != 0 or not SHA40.fullmatch(base_sha):
        raise PilotRefused("config_base", "base does not resolve to a commit")
    ws = Path(cfg.workspace)
    if not ws.is_dir():
        raise PilotRefused("config_workspace", "workspace must be an existing directory")
    run_dir = ws / f"pilot-{cfg.run_id}"
    if run_dir.exists():
        raise PilotRefused("config_workspace", "run directory already exists")
    return Resolved(cfg, base_sha, run_dir)


# --- simulated participants -------------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkOrder:
    round: int
    execution_key: dict[str, Any]
    task_text: str
    base_sha: str
    # HK-ISSUE-007 hook: live callbacks into the round's journal stream, for a REAL worker adapter
    # (e1/cli_worker.py). The simulated worker ignores them.
    journal: Callable[[str, dict[str, Any]], Any] | None = None      # append(kind, data): launch_intent / launched / exited / result_captured
    act: Callable[[str], Any] | None = None                         # intake(raw act JSON) -> Decision
    delivered: Callable[[], None] | None = None                     # the prompt reached the process: dispatch_outcome


@dataclass(frozen=True)
class WorkReport:
    status: str                      # ok | failed | unknown (an uncertain launch)
    artifact_sha: str | None = None
    checkpoints: int = 1
    reason: str | None = None        # why a non-ok report stopped
    attested: bool = False           # True: acts came from the worker (HEKATE-ACT) and the artifact is a real commit


@dataclass(frozen=True)
class ReviewOrder:
    """Everything the fresh reviewer sees: ONLY the verified, revalidated consumer view."""
    round: int
    artifact_ref: str
    view_part: str
    view_digest: str
    candidate_digest: str
    criteria: str


@dataclass(frozen=True)
class Verdict:
    decision: str                    # accepted | rejected | uncertain (stop for an operator; never decided)
    evidence: str


Worker = Callable[[WorkOrder], WorkReport]
Reviewer = Callable[[ReviewOrder], Verdict]


def dry_artifact(order: WorkOrder) -> str:
    """A deterministic stand-in commit SHA bound to (base, attempt). Not a real commit."""
    k = order.execution_key
    return sha(A.canonical([order.base_sha, k["rootId"], k["nodeId"], k["attemptId"], k["attemptEpoch"]]))[:40]


def dry_worker(order: WorkOrder) -> WorkReport:
    return WorkReport("ok", dry_artifact(order), checkpoints=2)


# --- the run record ------------------------------------------------------------------------------------

@dataclass
class RoundRecord:
    round: int
    claim_key: str
    attempt_id: str
    attempt_epoch: int | None = None
    run_id: str | None = None
    exec_id: str | None = None
    artifact_ref: str | None = None
    handoff_id: str | None = None
    candidate_digest: str | None = None
    view_digest: str | None = None
    decision: str | None = None
    evidence_ref: str | None = None
    previous: dict[str, Any] | None = None      # explicit cross-round link


@dataclass
class PilotResult:
    outcome: str                     # accepted | needs_operator
    reason: str | None
    root: str
    leaf: str
    base_sha: str
    rounds: list[RoundRecord]
    pending: list[dict[str, Any]] = field(default_factory=list)
    detail: Any = None


# --- the driver -----------------------------------------------------------------------------------------

def _ok(resp, what: str) -> dict[str, Any]:
    if resp.status != 200:
        raise _Stop(f"{what}_refused", {"status": resp.status, "code": resp.code})
    return resp.body


class _Stop(Exception):
    def __init__(self, reason: str, detail: Any = None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


class Pilot:
    def __init__(self, resolved: Resolved, setup: SetupClient, client: SupervisorClient, aj: ActsJournal, *,
                 worker: Worker = dry_worker, reviewer: Reviewer):
        self.r, self.cfg = resolved, resolved.cfg
        self.setup, self.client, self.aj = setup, client, aj
        self.worker, self.reviewer = worker, reviewer
        self.root, self.leaf = str(uuid.uuid4()), str(uuid.uuid4())
        self.rounds: list[RoundRecord] = []

    # -- PlanStore helpers
    def _node(self) -> dict[str, Any]:
        for n in _ok(self.setup.plan(self.root), "plan")["nodes"]:
            if n["id"] == self.leaf:
                return n
        raise _Stop("leaf_missing")

    def _transition(self, payload: dict[str, Any], *, operator: bool = False) -> dict[str, Any]:
        """The supervisor's own transitions go through SupervisorClient; operator moves (the HK-ISSUE-005
        workaround) through the operator/verifier surface, as in the accepted E1 role split."""
        n = self._node()
        via = self.setup if operator else self.client
        return _ok(via.transition(self.leaf, dict(payload, operationKey=uuid.uuid4().hex, expectedStateRevision=n["stateRevision"],
                                                  actor=self.cfg.supervisor)), f"transition_{payload['to']}")

    def _tick(self, dt: float = 10.0) -> None:
        self.aj.now += dt

    # -- setup
    def create_plan(self) -> None:
        _ok(self.setup.create_plan(self.root, self.cfg.project_id, f"pilot {self.cfg.run_id}", uuid.uuid4().hex), "create_plan")
        _ok(self.setup.add_child(self.root, self.leaf, "pilot task", 0, uuid.uuid4().hex, 1, value=self.cfg.task_text,
                                 attributes={"acceptance_criteria": self.cfg.criteria}), "add_child")

    # -- one round
    def _task_text(self, n: int, previous: dict[str, Any] | None) -> str:
        text = f"{self.cfg.task_text}\n\nAcceptance criteria: {self.cfg.criteria}\nBase: {self.r.base_sha}\nRound: {n}"
        if previous:
            text += (f"\nFix round for a rejected attempt: claimKey {previous['claimKey']}, attemptEpoch {previous['attemptEpoch']}, "
                     f"artifact {previous['artifactRef']}, decision evidence {previous['evidenceRef']}")
        return text

    def _package(self, receipt: dict[str, Any], text: str) -> str:
        return A.canonical({"kind": "supplied.v1", "suppliedSha256": sha(text),
                            "instructions": {k: sha(v) for k, v in INSTRUCTIONS.items()},
                            "contentRevision": receipt["contentRevision"], "contentDigest": receipt["contentDigest"].lower(),
                            "prereqDigest": receipt["prereqDigest"]})

    def run_round(self, n: int) -> RoundRecord:
        cfg = self.cfg
        rec = RoundRecord(n, f"pilot-{cfg.run_id}-r{n}", f"pilot-{cfg.run_id}-r{n}")
        if self.rounds:
            p = self.rounds[-1]
            rec.previous = {"claimKey": p.claim_key, "attemptEpoch": p.attempt_epoch, "artifactRef": p.artifact_ref,
                            "decision": p.decision, "evidenceRef": p.evidence_ref}
        self.rounds.append(rec)
        ws = f"hkw1:{cfg.worker}:{uuid.uuid4()}"
        ck = rec.claim_key

        # claim (intent recorded before the effect)
        self.aj.append(self.root, ck, "claim_intent", {"attemptId": rec.attempt_id, "executorRef": ws, "actor": cfg.supervisor})
        receipt = _ok(self.client.claim(self.root, ck, rec.attempt_id, ws, cfg.supervisor), "claim")["receipt"]
        if receipt["outcome"] != "claimed" or receipt["nodeId"] != self.leaf or receipt["attemptId"] != rec.attempt_id:
            raise _Stop("claim_mismatch", receipt.get("outcome"))
        rec.attempt_epoch = receipt["attemptEpoch"]
        self.aj.append(self.root, ck, "claimed", {"outcome": "claimed", "nodeId": self.leaf, "attemptId": rec.attempt_id,
                                                  "attemptEpoch": rec.attempt_epoch})

        # dispatch
        text = self._task_text(n, rec.previous)
        rec.run_id = f"pilot-{cfg.run_id}-run{n}"
        key = {"rootId": self.root, "nodeId": self.leaf, "attemptId": rec.attempt_id, "attemptEpoch": rec.attempt_epoch,
               "claimKey": ck, "runId": rec.run_id, "packageRef": self._package(receipt, text), "workerSession": ws}
        rec.exec_id = self.aj.dispatch(self.root, ck, key)

        # the worker (simulated, or a real adapter using the live callbacks)
        self._tick()
        live = {"delivered": False, "acks": 0}

        def journal(kind: str, data: dict[str, Any]) -> Any:
            self._tick(1)
            return self.aj.append(self.root, ck, kind, data)

        def act(raw: str) -> Any:
            self._tick(1)
            d = self.aj.intake(self.root, ck, raw)
            if d.outcome == "accepted" and json.loads(raw).get("kind") == "worker_ack":
                live["acks"] += 1
            return d

        def delivered() -> None:
            if not live["delivered"]:
                live["delivered"] = True
                self.aj.append(self.root, ck, "dispatch_outcome", {"exec": rec.exec_id, "outcome": "delivered", "messageId": None})

        try:
            report = self.worker(WorkOrder(n, dict(key), text, self.r.base_sha, journal=journal, act=act, delivered=delivered))
        except Exception as e:  # noqa: BLE001 -- any worker/adapter failure is a typed stop; run.json survives
            raise _Stop("worker_error", type(e).__name__) from None
        if report.status != "ok":
            # Uncertain or failed launch: fail closed. No finish; any open intent stays listed.
            raise _Stop("launch_uncertain" if report.status == "unknown" else "worker_failed", report.reason)
        art = report.artifact_sha
        if not (isinstance(art, str) and SHA40.fullmatch(art)):
            raise _Stop("artifact_malformed")
        if report.attested:
            # A real worker: its ACK must be worker-authored (spawn is `launched`, never an ACK), and the
            # artifact is re-verified here, independently of the adapter, as a real commit on the base.
            if not live["delivered"]:
                raise _Stop("not_delivered")
            if live["acks"] < 1:
                raise _Stop("no_worker_ack")
            repo = Path(self.cfg.repo)
            if (_git_out(repo, "cat-file", "-t", art) != "commit" or _git_out(repo, "rev-parse", f"{art}^") != self.r.base_sha):
                raise _Stop("artifact_not_bound", "artifact is not a commit whose parent is the base")
        else:
            delivered()
            acts = [{"kind": "worker_ack", "key": key, "actSeq": 1}]
            for i in range(1, report.checkpoints + 1):
                acts.append({"kind": "worker_progress", "key": key, "actSeq": i + 1, "checkpointId": i,
                             "evidenceDigest": sha(f"{rec.run_id}:{i}")})
            for a in acts:
                self._tick()
                d = self.aj.intake(self.root, ck, json.dumps(a))
                if d.outcome != "accepted":
                    raise _Stop("act_refused", {"kind": a["kind"], "outcome": d.outcome})
            if art != dry_artifact(WorkOrder(n, key, text, self.r.base_sha)):
                raise _Stop("artifact_not_bound", "artifact is not bound to (base, attempt)")
        rec.artifact_ref = art

        # finish, with the exact attempt
        self._transition({"to": "done", "attemptId": rec.attempt_id, "attemptEpoch": rec.attempt_epoch, "artifactRef": art})

        # review request bound to the current lead session, then the fresh-conversation handoff
        ls1, ls2 = (f"hkw1:{cfg.lead}:{uuid.uuid4()}" for _ in range(2))
        rk = {"rootId": self.root, "nodeId": self.leaf, "attemptId": rec.attempt_id, "attemptEpoch": rec.attempt_epoch,
              "artifactRef": art, "lead": cfg.lead, "leadSession": ls1}
        d = self.aj.request_review(self.root, ck, rk)
        if d.outcome != "accepted":
            acc = self._node().get("acceptance")
            if acc and acc.get("attemptEpoch") != rec.attempt_epoch:
                # A decision for another attempt that plan 038's guard could not prove older: the
                # review is operator classification, so stop and name the decision (fail closed).
                raise _Stop("prior_decision_blocks_review", {"priorDecision": acc, "attemptEpoch": rec.attempt_epoch})
            raise _Stop("review_request_refused", {"outcome": d.outcome, "reason": d.reason})
        review_task = {"text": f"Review artifact {art} for: {self.cfg.task_text}\nCriteria: {cfg.criteria}",
                       "instructions": REVIEW_INSTRUCTIONS, "packageRef": key["packageRef"]}
        try:
            prep = handoff_prepare(self.aj, self.root, ck, rk, prepare_id=str(uuid.uuid4()), target=ls2, gate="operator",
                                   gate_ref=f"pilot-{cfg.run_id}-fresh-reviewer-r{n}", task=review_task,
                                   conversation_ref=f"pilot-{cfg.run_id}-review-r{n}", now=self.aj.now)
        except H.Refused as e:
            raise _Stop("handoff_prepare_refused", e.code) from None
        d, receipt_h = handoff_commit(self.aj, prep, now=self.aj.now)
        if d.outcome != "accepted" or receipt_h is None:
            raise _Stop("handoff_commit_refused", d.outcome)
        rec.handoff_id, rec.candidate_digest = prep.transition["handoffId"], prep.package.candidate_digest
        h1_in = {"response": json.dumps({"round": n}), "rules": [], "systemInstruction": REVIEW_INSTRUCTIONS["system"],
                 "roleInstructions": {"fast": REVIEW_INSTRUCTIONS["fast"], "deep": REVIEW_INSTRUCTIONS["deep"]},
                 "budget": H1_BUDGET, "capturedAtIso": "2026-10-07T00:00:00Z"}
        delivery = CD.delivery(self.aj.dsn, rec.handoff_id, receipt_h, h1_in)
        try:
            fresh = CD.fresh(self.aj.dsn, C.verify_delivery(delivery))
            comp = C.compose(delivery, fresh, policy=C.PolicyStub(cfg.lead), destination=f"pilot-{cfg.run_id}-review-r{n}",
                             h1=C.h1_stub(review_task["text"]))
        except C.Refused as e:
            raise _Stop("handoff_refused", e.code) from None
        rec.view_digest = comp.view_digest

        # the independent reviewer sees only the verified view
        try:
            verdict = self.reviewer(ReviewOrder(n, art, comp.part, comp.view_digest, rec.candidate_digest, cfg.criteria))
        except Exception as e:  # noqa: BLE001 -- a reviewer execution failure is a typed stop, never a decision
            raise _Stop("reviewer_error", type(e).__name__) from None
        if verdict.decision == "uncertain":
            # The reviewer could not establish a verdict (e.g. an unconfirmed kill): stop for an operator.
            # No decision is recorded and no fix round is started (root review msg 1517).
            raise _Stop("review_uncertain", verdict.evidence[:2048])
        if verdict.decision not in ("accepted", "rejected") or not verdict.evidence.strip():
            raise _Stop("verdict_malformed")
        rec.evidence_ref = f"pilot-review:{sha(verdict.evidence)}"
        _ok(self.setup.decide(self.leaf, {"decision": verdict.decision, "reviewedContentRevision": receipt["contentRevision"],
                                          "reviewedArtifactRef": art, "reviewedAttemptEpoch": rec.attempt_epoch,
                                          "evidenceRef": rec.evidence_ref, "operationKey": uuid.uuid4().hex,
                                          "expectedStateRevision": self._node()["stateRevision"], "actor": cfg.lead}), "decide")
        rec.decision = verdict.decision
        return rec

    def run(self) -> PilotResult:
        self.r.run_dir.mkdir()
        outcome, reason, pending, detail = "needs_operator", "max_rounds", [], None
        try:
            self.create_plan()
            for n in range(1, self.cfg.max_rounds + 1):
                rec = self.run_round(n)
                if rec.decision == "accepted":
                    outcome, reason = "accepted", None
                    break
                if n < self.cfg.max_rounds:
                    # HK-ISSUE-005 workaround (dry run only): done -> cancelled -> todo, then a new claim.
                    self._transition({"to": "cancelled"}, operator=True)
                    self._transition({"to": "todo"}, operator=True)
        except _Stop as s:
            outcome, reason, detail = "needs_operator", s.reason, s.detail
            stopped = True
        except Exception as e:  # noqa: BLE001 -- an UNEXPECTED setup/transition/handoff/journal failure (msg 1545):
            outcome, reason, detail = "needs_operator", "unexpected_error", type(e).__name__   # a typed stop, type only
            stopped = True
        else:
            stopped = False
        if stopped and self.rounds and self.rounds[-1].claim_key:
            try:
                pending = self.pending(self.rounds[-1].claim_key)
            except Exception as e:  # noqa: BLE001 -- best effort; recorded, never masks the stop
                pending = [{"kind": "pending_unlistable", "error": type(e).__name__}]
        result = PilotResult(outcome, reason, self.root, self.leaf, self.r.base_sha, self.rounds, pending, detail)
        try:
            self.write_log(result)
        except Exception:  # noqa: BLE001 -- best effort; the result is still returned to the caller
            pass
        return result

    def pending(self, ck: str) -> list[dict[str, Any]]:
        """Open intents of the stream, from one snapshot (accepted E2d listing)."""
        import psycopg
        from psycopg.rows import dict_row

        from e1.durable import CONNECT_OPTIONS, read_stream_records
        from e1.handoff_durable import _aux
        with psycopg.connect(self.aj.dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                cur = conn.cursor()
                g = cur.execute("SELECT per_stream FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
                s = cur.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s", (self.root, ck)).fetchone()
                if s is None:
                    return []
                records, corrupt = read_stream_records(cur, self.root, ck, s, g["per_stream"])
                outstanding, _c, _q = _aux(cur, self.root, ck)
            finally:
                conn.execute("ROLLBACK")
        return H.pending_effects(records, corrupt, outstanding)

    def records_of(self, ck: str) -> list[str]:
        """The record kinds of one round's stream, in sequence order (read-only evidence)."""
        import psycopg
        with psycopg.connect(self.aj.dsn) as c:
            return [r[0] for r in c.execute("SELECT kind FROM supervisor_journal.records WHERE root = %s AND claim_key = %s ORDER BY seq",
                                            (self.root, ck))]

    def write_log(self, result: PilotResult) -> None:
        """The run log is the only file the driver writes, inside its own run directory."""
        doc = {"runId": self.cfg.run_id, "repo": str(self.cfg.repo), "baseSha": self.r.base_sha, "outcome": result.outcome,
               "reason": result.reason, "detail": result.detail, "root": result.root, "leaf": result.leaf, "pending": result.pending,
               "rounds": [asdict(r) for r in result.rounds], "dryRun": True,
               "workaround": "HK-ISSUE-005: fix rounds use done->cancelled->todo + a new claimKey (dry run only)"}
        (self.r.run_dir / "run.json").write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8", newline="\n")
