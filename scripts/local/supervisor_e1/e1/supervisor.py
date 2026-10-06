"""Test-only supervisor and scripted fake worker (plan 023 E1a). Not production code.

The supervisor is the ONLY writer of claim, finish and release, and its client exposes
nothing else. Rules (plan 023 §3.1):
- dispatch only on a FRESH claim (replayed == false) in this session; a replayed receipt or an
  explicit "uncertain" operator state is never permission to dispatch, whatever stillCurrent says;
- success is judged from typed outcome fields only (never prose) plus full correlation;
- before finishing: stillCurrent, receipt unchanged, and the node's CURRENT stateRevision read
  only for compare-and-set (content/context come only from the receipt);
- PlanStore is the final authority; any rejection becomes needs_operator with no retry;
- transition idempotency is last-operation only: the supervisor holds the original finish
  payload for an immediate replay and never re-sends after an intervening mutation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Callable

from .exact import INT64_MAX, WireError, counter
from .seam import Envelope, OpaqueWorkPackage, opaque_package, parse_claim_envelope
from .wire import Response, SupervisorClient

ACTOR = "supervisor-e1"


@dataclass(frozen=True)
class RunEnvelope:
    """A local hand-off. run_id is fresh per dispatch and is never written to PlanStore."""
    run_id: str
    package_token: str


@dataclass(frozen=True)
class WorkResult:
    # correlation (every field must equal the package / run)
    run_id: str
    package_token: str
    root_id: str
    node_id: str
    claim_key: str
    attempt_id: str
    attempt_epoch: int
    executor_ref: str | None
    content_digest: str
    prereq_digest: str
    # outcome (typed; prose is never consulted)
    exit_code: int
    timed_out: bool
    killed: bool
    structured_result: dict[str, Any] | None
    artifact_ref: str | None
    prose: str = ""


CORRELATION = ("run_id", "package_token", "root_id", "node_id", "claim_key", "attempt_id", "attempt_epoch",
               "executor_ref", "content_digest", "prereq_digest")


def echo(pkg: OpaqueWorkPackage, run: RunEnvelope, **outcome: Any) -> WorkResult:
    """A WorkResult that correlates exactly; tests override single fields to break it."""
    base: dict[str, Any] = dict(run_id=run.run_id, package_token=pkg.token, root_id=pkg.root_id, node_id=pkg.node_id,
                                claim_key=pkg.claim_key, attempt_id=pkg.attempt_id, attempt_epoch=pkg.attempt_epoch,
                                executor_ref=pkg.executor_ref, content_digest=pkg.content_digest, prereq_digest=pkg.prereq_digest,
                                exit_code=0, timed_out=False, killed=False, structured_result={"status": "ok"},
                                artifact_ref="sha-e1", prose="")
    base.update(outcome)
    return WorkResult(**base)


Script = Callable[[OpaqueWorkPackage, RunEnvelope], WorkResult]


class FakeWorker:
    """In-process scripted worker. Launches nothing. Counts dispatches."""

    def __init__(self, script: Script):
        self._script = script
        self.dispatches: list[RunEnvelope] = []

    def __call__(self, pkg: OpaqueWorkPackage, run: RunEnvelope) -> WorkResult:
        self.dispatches.append(run)
        return self._script(pkg, run)


class Outcome(Enum):
    FINISHED = "finished"
    NO_READY_WORK = "no_ready_work"
    NEEDS_OPERATOR = "needs_operator"


@dataclass
class SupervisedRun:
    outcome: Outcome
    reason: str
    envelope: Envelope | None = None
    package: OpaqueWorkPackage | None = None
    run: RunEnvelope | None = None
    result: WorkResult | None = None
    finish: Response | None = None
    held_finish: dict[str, Any] | None = None   # original payload, for an immediate replay only
    notes: list[str] = field(default_factory=list)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def malformed_fields(res: WorkResult) -> list[str]:
    """Runtime type validation (dataclass hints are not enforced). Any entry means: never finish."""
    bad: list[str] = []
    for f in ("run_id", "package_token", "root_id", "node_id", "claim_key", "attempt_id", "content_digest", "prereq_digest"):
        v = getattr(res, f)
        if not isinstance(v, str) or not v.strip():
            bad.append(f)
    if res.executor_ref is not None and (not isinstance(res.executor_ref, str) or not res.executor_ref.strip()):
        bad.append("executor_ref")
    if not _is_int(res.attempt_epoch) or not 1 <= res.attempt_epoch <= INT64_MAX:
        bad.append("attempt_epoch")
    if not _is_int(res.exit_code):
        bad.append("exit_code")
    for f in ("timed_out", "killed"):
        if not isinstance(getattr(res, f), bool):
            bad.append(f)
    if res.structured_result is not None and not isinstance(res.structured_result, dict):
        bad.append("structured_result")
    if res.artifact_ref is not None and (not isinstance(res.artifact_ref, str) or not res.artifact_ref.strip()):
        bad.append("artifact_ref")
    if not isinstance(res.prose, str):
        bad.append("prose")
    return bad


def correlation_mismatches(pkg: OpaqueWorkPackage, run: RunEnvelope, res: WorkResult) -> list[str]:
    """Strict: equal value AND equal type (so True never matches epoch 1)."""
    expected = echo(pkg, run)
    out = []
    for f in CORRELATION:
        a, b = getattr(res, f), getattr(expected, f)
        if type(a) is not type(b) or a != b:
            out.append(f)
    return out


def is_success(res: WorkResult) -> str | None:
    """None when the typed outcome is a success; otherwise the non-success shape. Prose is ignored.
    Call only after malformed_fields() is empty."""
    if res.timed_out is not False:
        return "timed_out"
    if res.killed is not False:
        return "killed"
    if res.exit_code != 0:
        return "nonzero_exit"
    if not isinstance(res.structured_result, dict) or res.structured_result.get("status") != "ok":
        return "missing_structured_result"
    if res.artifact_ref is None:
        return "missing_artifact"
    return None


def current_node_state(plan: Response, root: str, node: str) -> dict[str, Any]:
    if plan.status != 200 or not isinstance(plan.body, dict) or plan.body.get("rootId") != root:
        raise WireError("unexpected_shape", f"plan read failed ({plan.status})")
    for n in plan.body.get("nodes", []):
        if isinstance(n, dict) and n.get("id") == node:
            counter(n.get("stateRevision"), "node.stateRevision")
            counter(n.get("attemptEpoch"), "node.attemptEpoch")
            return n
    raise WireError("unexpected_shape", f"node {node} not in plan {root}")


class Supervisor:
    def __init__(self, client: SupervisorClient, worker: FakeWorker, *, operator_state: dict[str, str] | None = None):
        self._client = client
        self._worker = worker
        self.operator_state = dict(operator_state or {})
        self.before_finish: Callable[[], None] | None = None   # test hook (race case only)

    def run(self, root: str, claim_key: str, attempt_id: str, executor_ref: str | None) -> SupervisedRun:
        resp = self._client.claim(root, claim_key, attempt_id, executor_ref, ACTOR)
        if resp.status != 200:
            return SupervisedRun(Outcome.NEEDS_OPERATOR, f"claim_failed:{resp.status}:{resp.code}")
        env = parse_claim_envelope(resp.raw, expected_root=root, expected_key=claim_key)
        if env.receipt.outcome == "no_ready_work":
            return SupervisedRun(Outcome.NO_READY_WORK, "no_ready_work", env)
        if self.operator_state.get(claim_key) == "uncertain":
            return SupervisedRun(Outcome.NEEDS_OPERATOR, "uncertain", env)
        if env.replayed:
            return SupervisedRun(Outcome.NEEDS_OPERATOR, "replayed_claim", env)
        if env.receipt.attempt_id != attempt_id or env.receipt.executor_ref != executor_ref:
            return SupervisedRun(Outcome.NEEDS_OPERATOR, "receipt_request_mismatch", env)
        # Even a fresh claim must be current at hand-off (parse_claim_envelope already proved the
        # current attempt equals the receipt whenever stillCurrent is true). Not a launch authority:
        # it only refuses; it never makes a replay dispatchable.
        if not env.still_current or env.current is None or env.current.work != "in_progress":
            return SupervisedRun(Outcome.NEEDS_OPERATOR, "not_current_at_dispatch", env)

        pkg = opaque_package(env)
        run = RunEnvelope(f"run-{uuid.uuid4()}", pkg.token)
        out = SupervisedRun(Outcome.NEEDS_OPERATOR, "", env, pkg, run)
        res = self._worker(pkg, run)
        out.result = res
        if malformed := malformed_fields(res):
            out.reason = "malformed_result:" + ",".join(malformed)
            return out
        if mism := correlation_mismatches(pkg, run, res):
            out.reason = "result_correlation_mismatch:" + ",".join(mism)
            return out
        if shape := is_success(res):
            out.reason = "non_success:" + shape
            return out

        # Preconditions: re-read the receipt (a READ) and the node's current revision (CAS only).
        again = self._client.get_claim(root, claim_key)
        if again.status != 200:
            out.reason = f"claim_read_failed:{again.status}:{again.code}"
            return out
        env2 = parse_claim_envelope(again.raw, expected_root=root, expected_key=claim_key)
        if env2.receipt != env.receipt:
            out.reason = "receipt_changed"
            return out
        if not env2.still_current:
            out.reason = "not_still_current"
            return out
        node = current_node_state(self._client.get_plan(root), root, pkg.node_id)
        if node.get("work") != "in_progress" or node.get("attemptId") != pkg.attempt_id or node.get("attemptEpoch") != pkg.attempt_epoch:
            out.reason = "attempt_not_current"
            return out

        if self.before_finish is not None:
            self.before_finish()
        payload: dict[str, Any] = {
            "to": "done", "attemptId": pkg.attempt_id, "attemptEpoch": pkg.attempt_epoch, "artifactRef": res.artifact_ref,
            "operationKey": f"supervisor:{claim_key}:finish", "expectedStateRevision": node["stateRevision"], "actor": ACTOR,
        }
        if pkg.executor_ref is not None:
            payload["executorRef"] = pkg.executor_ref
        out.held_finish = dict(payload)
        fin = self._client.transition(pkg.node_id, payload)
        out.finish = fin
        if fin.status == 200 and isinstance(fin.body, dict) and fin.body.get("outcome") == "applied":
            out.outcome, out.reason = Outcome.FINISHED, "finished"
        else:
            out.reason = f"finish_rejected:{fin.status}:{fin.code}"   # PlanStore is final; no retry
        return out

    def replay_held_finish(self, run: SupervisedRun) -> tuple[Response | None, str]:
        """Immediate replay of the ORIGINAL finish payload, sent ONLY while current state proves our
        finish is still the node's last operation (Done, same attempt/epoch/artifact/executor, and
        stateRevision == held expectedStateRevision + 1). Otherwise nothing is sent: reconcile."""
        if run.held_finish is None or run.package is None:
            raise ValueError("no held finish")
        pkg, held = run.package, run.held_finish
        node = current_node_state(self._client.get_plan(pkg.root_id), pkg.root_id, pkg.node_id)
        ours = (node.get("work") == "done" and node.get("attemptId") == held["attemptId"]
                and node.get("attemptEpoch") == held["attemptEpoch"] and node.get("artifactRef") == held["artifactRef"]
                and node.get("executorRef") == held.get("executorRef")
                and node.get("stateRevision") == held["expectedStateRevision"] + 1)
        if not ours:
            return None, "reconcile:intervening_mutation"
        return self._client.transition(pkg.node_id, dict(held)), "replayed"

    def release(self, run: SupervisedRun, *, operator: str, reason: str) -> Response:
        """Explicit operator instruction only. Reads the current revision for CAS; never automatic."""
        if not operator or not reason:
            raise ValueError("a release needs an explicit operator and reason")
        pkg = run.package or (opaque_package(run.envelope) if run.envelope else None)
        if pkg is None:
            raise ValueError("nothing to release")
        node = current_node_state(self._client.get_plan(pkg.root_id), pkg.root_id, pkg.node_id)
        run.notes.append(f"release by {operator}: {reason}")
        return self._client.transition(pkg.node_id, {
            "to": "todo", "attemptId": pkg.attempt_id, "attemptEpoch": pkg.attempt_epoch,
            "operationKey": f"supervisor:{pkg.claim_key}:release", "expectedStateRevision": node["stateRevision"], "actor": ACTOR,
        })


def correlation_fields() -> tuple[str, ...]:
    """WorkResult correlation inventory (asserted complete by tests)."""
    names = {f.name for f in fields(WorkResult)}
    assert set(CORRELATION) <= names
    return CORRELATION
