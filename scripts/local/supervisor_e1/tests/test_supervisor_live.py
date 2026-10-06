"""Live E1a cases against the real plan-contract API on a new disposable database.

Setup (SetupClient) builds plans and plays operator/verifier; the supervisor only ever has
SupervisorClient. Every case asserts what PlanStore did or did not record.
"""

import uuid
from dataclasses import dataclass, replace

import pytest

from helpers import key, ok
from e1 import supervisor as sup
from e1.seam import opaque_package, parse_claim_envelope
from e1.supervisor import CORRELATION, FakeWorker, Outcome, Supervisor, echo
from e1.wire import SupervisorClient


@dataclass
class Plan:
    root: str
    pred: str
    target: str


def node_state(setup, root, node):
    return next(n for n in ok(setup.plan(root))["nodes"] if n["id"] == node)


def rev(setup, root, node) -> int:
    return node_state(setup, root, node)["stateRevision"]


def make_plan(harness, setup, *, pred_done: bool = True) -> Plan:
    """root -> pred (order 0), target (order 1) with target depending on pred (default Accepted gate)."""
    p = Plan(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
    ok(setup.create_plan(p.root, harness.project_id, "E1 plan", key()))
    ok(setup.add_child(p.root, p.pred, "pred", 0, key(), 1, value="pred spec"))
    ok(setup.add_child(p.root, p.target, "target", 1, key(), 2, value="target spec",
                       attributes={"acceptance_criteria": "tests pass"}))
    ok(setup.add_dependency(p.target, p.pred, key(), 0))
    if pred_done:
        ok(setup.transition(p.pred, {"to": "in_progress", "attemptId": "p1", "operationKey": key(), "expectedStateRevision": 0, "actor": "e1-setup"}))
        ok(setup.transition(p.pred, {"to": "done", "attemptId": "p1", "attemptEpoch": 1, "artifactRef": "sha-p",
                                     "operationKey": key(), "expectedStateRevision": 1, "actor": "e1-setup"}))
        ok(setup.decide(p.pred, {"decision": "accepted", "reviewedContentRevision": 1, "reviewedArtifactRef": "sha-p",
                                 "reviewedAttemptEpoch": 1, "evidenceRef": "ev-p", "operationKey": key(),
                                 "expectedStateRevision": 2, "actor": "e1-verifier"}))
    return p


def db_snapshot(harness, root) -> str:
    return harness.psql(
        "SELECT concat_ws('|', "
        f"(SELECT string_agg(row_to_json(s)::jsonb::text, ',' ORDER BY node_id) FROM plan_node_state s WHERE root_node_id = '{root}'), "
        f"(SELECT count(*) FROM plan_attempt_events WHERE root_node_id = '{root}'), "
        f"(SELECT count(*) FROM plan_claim_receipts WHERE root_node_id = '{root}'))")


def events(setup, node):
    return ok(setup.events(node))["events"]


def revise(setup, root, node, value):
    n = node_state(setup, root, node)
    ok(setup.revise(node, value, n["contentRevision"], key(), n["stateRevision"]))


# --------------------------------------------------------------------------- happy path

def test_happy_path_finishes_with_cas_and_verifier_decides_separately(harness, setup, client):
    p = make_plan(harness, setup)
    worker = FakeWorker(lambda pkg, run: echo(pkg, run, artifact_ref="sha-target"))
    s = Supervisor(client, worker)
    ck = key()
    r = s.run(p.root, ck, "att-1", "e1:run-ref")
    assert (r.outcome, r.reason) == (Outcome.FINISHED, "finished"), r.reason
    assert len(worker.dispatches) == 1
    n = node_state(setup, p.root, p.target)
    assert (n["work"], n["artifactRef"], n["executorRef"]) == ("done", "sha-target", "e1:run-ref")
    ev = events(setup, p.target)
    assert [e["kind"] for e in ev] == ["attempt_started", "attempt_finished"]
    assert ev[0]["claimKey"] == ck and ev[1]["operationKey"] == f"supervisor:{ck}:finish"
    assert ev[1]["actor"] == sup.ACTOR and r.held_finish["expectedStateRevision"] == n["stateRevision"] - 1   # CAS on the revision read just before
    # The independent verifier, not the supervisor, records the decision.
    ok(setup.decide(p.target, {"decision": "accepted", "reviewedContentRevision": 1, "reviewedArtifactRef": "sha-target",
                               "reviewedAttemptEpoch": 1, "evidenceRef": "review", "operationKey": key(),
                               "expectedStateRevision": rev(setup, p.root, p.target), "actor": "e1-verifier"}))
    assert [e["actor"] for e in events(setup, p.target) if e["kind"] == "decision_recorded"] == ["e1-verifier"]
    assert all(type(e["seq"]) is int and type(e["attemptEpoch"]) is int for e in events(setup, p.target))


def test_no_ready_work_dispatches_nothing(harness, setup, client):
    p = make_plan(harness, setup, pred_done=False)
    ok(setup.transition(p.pred, {"to": "in_progress", "attemptId": "busy", "operationKey": key(), "expectedStateRevision": 0, "actor": "e1-setup"}))
    worker = FakeWorker(lambda pkg, run: pytest.fail("must not dispatch"))
    r = Supervisor(client, worker).run(p.root, key(), "att", None)
    assert r.outcome is Outcome.NO_READY_WORK and worker.dispatches == []


# --------------------------------------------------------------------------- drift / staleness

def test_upstream_drift_during_the_run_is_needs_operator_without_finish(harness, setup, client):
    p = make_plan(harness, setup)

    def drifting(pkg, run):
        revise(setup, p.root, p.pred, "pred spec v2")     # upstream changes while "working"
        return echo(pkg, run)
    r = Supervisor(client, FakeWorker(drifting)).run(p.root, key(), "att", None)
    assert (r.outcome, r.reason) == (Outcome.NEEDS_OPERATOR, "not_still_current")
    assert r.finish is None and node_state(setup, p.root, p.target)["work"] == "in_progress"
    assert [e["kind"] for e in events(setup, p.target)] == ["attempt_started"]


def test_own_content_revision_is_needs_operator_and_a_forced_finish_is_stale_content(harness, setup, client):
    p = make_plan(harness, setup)

    def revising(pkg, run):
        revise(setup, p.root, p.target, "target spec v2")
        return echo(pkg, run)
    r = Supervisor(client, FakeWorker(revising)).run(p.root, key(), "att", None)
    assert (r.outcome, r.reason) == (Outcome.NEEDS_OPERATOR, "not_still_current")
    forced = setup.transition(p.target, {"to": "done", "attemptId": "att", "attemptEpoch": 1, "artifactRef": "x",
                                         "operationKey": key(), "expectedStateRevision": rev(setup, p.root, p.target), "actor": "e1-setup"})
    assert (forced.status, forced.code) == (409, "stale_content")


def test_race_between_precheck_and_finish_is_rejected_by_planstore_and_not_retried(harness, setup, client):
    p = make_plan(harness, setup)
    s = Supervisor(client, FakeWorker(lambda pkg, run: echo(pkg, run)))
    s.before_finish = lambda: revise(setup, p.root, p.pred, "pred spec raced")
    r = s.run(p.root, key(), "att", None)
    assert (r.outcome, r.reason) == (Outcome.NEEDS_OPERATOR, "finish_rejected:409:stale_prerequisites")
    assert [e["kind"] for e in events(setup, p.target)] == ["attempt_started"]   # exactly one finish attempt, rejected


def test_stale_expected_state_revision_is_409_without_retry(harness, setup, client, monkeypatch):
    p = make_plan(harness, setup)
    real = sup.current_node_state
    calls = []

    def stale(plan, root, node):
        n = dict(real(plan, root, node))
        n["stateRevision"] -= 1
        calls.append(n["stateRevision"])
        return n
    monkeypatch.setattr(sup, "current_node_state", stale)
    r = Supervisor(client, FakeWorker(lambda pkg, run: echo(pkg, run))).run(p.root, key(), "att", None)
    assert (r.outcome, r.reason) == (Outcome.NEEDS_OPERATOR, "finish_rejected:409:stale_revision")
    assert len(calls) == 1 and node_state(setup, p.root, p.target)["work"] == "in_progress"


# --------------------------------------------------------------------------- correlation / outcome

@pytest.mark.parametrize("field", CORRELATION)
def test_each_correlation_field_mismatch_is_rejected_before_any_write(harness, setup, client, field):
    p = make_plan(harness, setup)

    def wrong(pkg, run):
        good = echo(pkg, run)
        value = getattr(good, field)
        if field == "supplied_sha256":   # a well-formed but different hash (None for the E1a package)
            bad = "a" * 64 if value != "a" * 64 else "b" * 64
        else:
            bad = value + 1 if isinstance(value, int) else (value or "") + "-x"
        return replace(good, **{field: bad})
    s = Supervisor(client, FakeWorker(wrong))
    ck = key()
    s_run = s.run(p.root, ck, "att", "e1:ref")
    assert (s_run.outcome, s_run.reason) == (Outcome.NEEDS_OPERATOR, f"result_correlation_mismatch:{field}")
    assert s_run.finish is None and [e["kind"] for e in events(setup, p.target)] == ["attempt_started"]


@pytest.mark.parametrize("shape,outcome", [
    ("nonzero_exit", dict(exit_code=1, prose="All done, SUCCESS!")),
    ("timed_out", dict(timed_out=True, prose="finished successfully")),
    ("killed", dict(killed=True)),
    ("missing_structured_result", dict(structured_result=None, prose="ok")),
    ("missing_structured_result", dict(structured_result={"status": "maybe"})),
    ("missing_artifact", dict(artifact_ref=None)),
])
def test_non_success_shapes_never_finish_whatever_the_prose(harness, setup, client, shape, outcome):
    p = make_plan(harness, setup)
    r = Supervisor(client, FakeWorker(lambda pkg, run: echo(pkg, run, **outcome))).run(p.root, key(), "att", None)
    assert (r.outcome, r.reason) == (Outcome.NEEDS_OPERATOR, f"non_success:{shape}")
    assert node_state(setup, p.root, p.target)["work"] == "in_progress"


# --------------------------------------------------------------------------- uncertainty policy

def test_replayed_claim_never_dispatches_and_only_an_explicit_operator_release_writes(harness, setup, client):
    p = make_plan(harness, setup)
    ck = key()

    def crash(pkg, run):
        raise RuntimeError("supervisor crashed after hand-off")
    first = Supervisor(client, FakeWorker(crash)).run(p.root, ck, "att", "e1:ref")
    # The worker fault is captured, not raised: evidence retained, nothing finished or released.
    assert (first.outcome, first.reason, first.error) == (Outcome.NEEDS_OPERATOR, "uncertain:worker_raised", "RuntimeError")
    assert first.package is not None and first.run is not None and first.claim_request["claimKey"] == ck
    assert node_state(setup, p.root, p.target)["work"] == "in_progress"
    before = db_snapshot(harness, p.root)

    # A new supervisor (no journal in E1a: this is POLICY, not restart durability).
    worker = FakeWorker(lambda pkg, run: pytest.fail("must not dispatch on a replayed claim"))
    s = Supervisor(client, worker)
    r = s.run(p.root, ck, "att", "e1:ref")
    assert (r.outcome, r.reason) == (Outcome.NEEDS_OPERATOR, "replayed_claim")
    assert r.envelope.still_current is True and worker.dispatches == []
    assert db_snapshot(harness, p.root) == before                       # replay wrote nothing

    marked = Supervisor(client, worker, operator_state={ck: "uncertain"}).run(p.root, ck, "att", "e1:ref")
    assert (marked.outcome, marked.reason) == (Outcome.NEEDS_OPERATOR, "uncertain")
    assert db_snapshot(harness, p.root) == before

    with pytest.raises(ValueError):
        s.release(r, operator="", reason="")                             # never implicit
    rel = s.release(r, operator="e1-operator", reason="reconciled: no external effects")
    assert ok(rel)["outcome"] == "applied" and node_state(setup, p.root, p.target)["work"] == "todo"
    released = events(setup, p.target)[-1]
    assert (released["kind"], released["operationKey"]) == ("attempt_released", f"supervisor:{ck}:release")
    assert released["attemptPrereqDigest"] is not None                  # pins captured before clear


def test_package_identity_is_deterministic_and_run_ids_are_fresh(harness, setup, client):
    p = make_plan(harness, setup)
    ck = key()
    s = Supervisor(client, FakeWorker(lambda pkg, run: echo(pkg, run, exit_code=3)))   # stays InProgress
    first = s.run(p.root, ck, "att", None)
    again = parse_claim_envelope(client.get_claim(p.root, ck).raw, expected_root=p.root, expected_key=ck)
    assert opaque_package(again) == first.package
    p2 = make_plan(harness, setup)
    second = s.run(p2.root, key(), "att", None)
    assert first.run.run_id != second.run.run_id
    assert first.run.run_id not in repr(first.package)


def test_supervisor_client_has_no_setup_or_verifier_capabilities(client):
    for name in ("create_plan", "add_child", "add_dependency", "decide", "revise", "events"):
        assert not hasattr(client, name)
    with pytest.raises(ValueError):
        client.transition(str(uuid.uuid4()), {"to": "in_progress"})
    with pytest.raises(ValueError):
        SupervisorClient("http://example.com:5108")


# --------------------------------------------------------------------------- idempotency

def test_finish_idempotency_is_last_operation_only(harness, setup, client):
    p = make_plan(harness, setup)
    s = Supervisor(client, FakeWorker(lambda pkg, run: echo(pkg, run, artifact_ref="sha-1")))
    r = s.run(p.root, key(), "att", None)
    assert r.outcome is Outcome.FINISHED
    resp, why = s.replay_held_finish(r)                                  # immediate replay: our finish is the last op
    assert why == "replayed" and ok(resp)["outcome"] == "unchanged"

    # Raw API facts (fixture-only calls, not supervisor behaviour):
    changed = dict(r.held_finish, artifactRef="sha-2")
    reused = client.transition(p.target, changed)
    assert (reused.status, reused.code) == (409, "operation_key_reused")

    ok(setup.decide(p.target, {"decision": "rejected", "reviewedContentRevision": 1, "reviewedArtifactRef": "sha-1",
                               "reviewedAttemptEpoch": 1, "evidenceRef": "rev", "operationKey": key(),
                               "expectedStateRevision": rev(setup, p.root, p.target), "actor": "e1-verifier"}))
    before = db_snapshot(harness, p.root)
    resp, why = s.replay_held_finish(r)                                  # after an intervening mutation
    assert (resp, why) == (None, "reconcile:intervening_mutation")       # the supervisor sends NOTHING
    raw_late = client.transition(p.target, dict(r.held_finish))          # raw API: the old key is no longer idempotent
    assert (raw_late.status, raw_late.code) == (409, "stale_revision")
    assert db_snapshot(harness, p.root) == before


@pytest.mark.parametrize("field,value", [
    ("attempt_epoch", True), ("attempt_epoch", 1.0), ("attempt_epoch", 0), ("attempt_epoch", 2**63),
    ("exit_code", 0.0), ("exit_code", False), ("exit_code", None), ("exit_code", "0"),
    ("timed_out", None), ("timed_out", 0), ("killed", None), ("killed", "false"),
    ("artifact_ref", ""), ("artifact_ref", "   "), ("artifact_ref", 5),
    ("structured_result", "ok"), ("claim_key", None), ("run_id", ""), ("executor_ref", ""), ("prose", None),
])
def test_malformed_result_types_never_finish(harness, setup, client, field, value):
    p = make_plan(harness, setup)
    r = Supervisor(client, FakeWorker(lambda pkg, run: replace(echo(pkg, run), **{field: value}))).run(p.root, key(), "att", None)
    assert r.outcome is Outcome.NEEDS_OPERATOR and r.reason.startswith("malformed_result:") and field in r.reason
    assert node_state(setup, p.root, p.target)["work"] == "in_progress"
