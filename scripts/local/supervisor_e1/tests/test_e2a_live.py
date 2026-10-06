"""E2a live: the evidence model wired into the EXISTING test supervisor through its optional journal
hook, against the real API on a new disposable database. Test-only; no durability claim.

Coherent C7 facts come from a FIXTURE-ONLY repeatable-read SQL snapshot of the disposable database
(e1/coherent.py), never from two independent API reads.
"""

import uuid

import pytest

from e1.coherent import CoherentFacts, prove_finish, read_coherent
from e1.evidence import Bounds, Facts, JournalRefused, ModelJournal, classify, derive_review
from e1.seam import parse_claim_envelope
from e1.supervisor import FakeWorker, Outcome, Supervisor, echo
from e1.wire import SupervisorClient
from helpers import key, ok
from test_supervisor_faults import FaultyClient
from test_supervisor_live import db_snapshot, make_plan, node_state, rev, revise

W = "supervisor-e1#A"


def journal_for(mj: ModelJournal, root: str, ck: str, timeline: list | None = None):
    def hook(kind, data):
        if timeline is not None:
            timeline.append(kind)
        return mj.append(root, ck, kind, data)          # the Record (its `degraded` flag reaches the supervisor)
    return hook


class TimelineClient(SupervisorClient):
    """Records each EFFECT (claim POST, transition) into the shared timeline."""

    def __init__(self, base, timeline):
        super().__init__(base)
        self.timeline = timeline

    def claim(self, *a, **kw):
        self.timeline.append("EFFECT:claim")
        return super().claim(*a, **kw)

    def transition(self, node, payload):
        self.timeline.append("EFFECT:" + payload["to"])
        return super().transition(node, payload)


def view_facts(setup, root, node_id, held=None, **extra) -> Facts:
    plan = ok(setup.plan(root))
    n = next(x for x in plan["nodes"] if x["id"] == node_id)
    leaf = next((x for x in plan["readiness"]["leaves"] if x["nodeId"] == node_id), None)
    done_for = held is not None and n["work"] == "done" and n["attemptId"] == held["attemptId"] and n["attemptEpoch"] == held["attemptEpoch"]
    return Facts(reader=W, node_done_for_attempt=done_for, review_class=derive_review(n, leaf).review_class, **extra)


# --------------------------------------------------------------------------- ordering and refusal

def test_every_intent_is_recorded_before_its_effect(harness, setup):
    p = make_plan(harness, setup)
    timeline: list[str] = []
    mj = ModelJournal(W)
    ck = key()

    def worker(pkg, run):
        timeline.append("EFFECT:dispatch")
        return echo(pkg, run)
    s = Supervisor(TimelineClient(harness.base_url, timeline), FakeWorker(worker), journal=journal_for(mj, p.root, ck, timeline))
    r = s.run(p.root, ck, "att", None)
    assert r.outcome is Outcome.FINISHED
    assert [x.kind for x in mj.streams[(p.root, ck)].records] == [
        "claim_intent", "claimed", "launch_intent", "launched", "exited", "result_captured", "finish_intent", "finish_outcome"]
    assert timeline.index("claim_intent") < timeline.index("EFFECT:claim")
    assert timeline.index("launch_intent") < timeline.index("EFFECT:dispatch")
    assert timeline.index("finish_intent") < timeline.index("EFFECT:done")
    assert all(r.payload.get("modelOnly") for r in mj.streams[(p.root, ck)].records if r.kind in ("launched", "exited"))


@pytest.mark.parametrize("refuse,expect_claim,expect_dispatch,work", [
    ("claim_intent", False, False, "todo"),
    ("launch_intent", True, False, "in_progress"),
    ("finish_intent", True, True, "in_progress"),
])
def test_a_refused_intent_prevents_its_effect(harness, setup, refuse, expect_claim, expect_dispatch, work):
    p = make_plan(harness, setup)
    ck = key()
    dispatched = []

    def hook(kind, data):
        if kind == refuse:
            raise JournalRefused("stream_cap", "test refusal")
    s = Supervisor(SupervisorClient(harness.base_url), FakeWorker(lambda pkg, run: (dispatched.append(1), echo(pkg, run))[1]),
                   journal=hook)
    before = db_snapshot(harness, p.root)
    r = s.run(p.root, ck, "att", None)
    assert (r.outcome, r.reason, r.error) == (Outcome.NEEDS_OPERATOR, f"journal_refused:{refuse}", "JournalRefused")
    assert bool(dispatched) == expect_dispatch
    assert node_state(setup, p.root, p.target)["work"] == work
    if not expect_claim:
        assert db_snapshot(harness, p.root) == before                       # no claim was sent at all


def test_real_model_cap_refuses_launch_before_dispatch_and_still_records_outcomes(harness, setup):
    p = make_plan(harness, setup)
    ck = key()
    mj = ModelJournal(W, Bounds(per_stream=5))           # claim uses 4 slots; launch would need 4 more
    worker = FakeWorker(lambda pkg, run: pytest.fail("must not dispatch"))
    r = Supervisor(SupervisorClient(harness.base_url), worker, journal=journal_for(mj, p.root, ck)).run(p.root, ck, "att", None)
    assert r.reason == "journal_refused:launch_intent" and worker.dispatches == []
    kinds = [x.kind for x in mj.streams[(p.root, ck)].records]
    assert kinds == ["claim_intent", "claimed"]           # the claim outcome was recorded under its reservation
    mj.append(p.root, ck, "operator_resolution", {"decision": "release", "reason": "launch refused by cap"})   # reserve still usable


@pytest.mark.parametrize("failing", ["launched", "exited", "result_captured"])
def test_any_failed_outcome_record_stops_before_the_finish_transition(harness, setup, failing):
    p = make_plan(harness, setup)
    ck = key()
    timeline: list[str] = []
    mj = ModelJournal(W)

    def hook(kind, data):
        if kind == failing:
            raise RuntimeError("journal write failed")
        return mj.append(p.root, ck, kind, data)
    r = Supervisor(TimelineClient(harness.base_url, timeline), FakeWorker(lambda pkg, run: echo(pkg, run)), journal=hook).run(p.root, ck, "att", None)
    assert r.outcome is Outcome.NEEDS_OPERATOR and r.reason.startswith(f"outcome_unrecorded:{failing}")
    assert "EFFECT:done" not in timeline and r.held_finish is None              # no transition attempted
    assert node_state(setup, p.root, p.target)["work"] == "in_progress"


def test_degraded_outcome_evidence_stops_before_the_finish_transition(harness, setup):
    p = make_plan(harness, setup)
    ck = key()
    timeline: list[str] = []
    mj = ModelJournal(W)
    big_artifact = "sha-" + "x" * 600                                           # > 512-byte ref cap -> invalidPayload fallback
    r = Supervisor(TimelineClient(harness.base_url, timeline), FakeWorker(lambda pkg, run: echo(pkg, run, artifact_ref=big_artifact)),
                   journal=journal_for(mj, p.root, ck)).run(p.root, ck, "att", None)
    rec = mj.streams[(p.root, ck)].records[-1]
    assert rec.kind == "result_captured" and rec.degraded                       # recorded only as bounded fault evidence
    assert r.reason.startswith("outcome_unrecorded:result_captured") and r.journal_errors == ["result_captured:degraded"]
    assert "EFFECT:done" not in timeline and node_state(setup, p.root, p.target)["work"] == "in_progress"


def test_an_unrecordable_outcome_stops_the_run_before_any_further_effect(harness, setup):
    p = make_plan(harness, setup)
    ck = key()

    def hook(kind, data):
        if kind == "result_captured":
            raise RuntimeError("journal unavailable")
    r = Supervisor(SupervisorClient(harness.base_url), FakeWorker(lambda pkg, run: echo(pkg, run)), journal=hook).run(p.root, ck, "att", None)
    assert r.outcome is Outcome.NEEDS_OPERATOR and "journal_outcome_unrecorded" in r.reason
    assert r.reason.startswith("outcome_unrecorded:result_captured")
    assert r.journal_errors == ["result_captured:RuntimeError"] and r.held_finish is None
    assert node_state(setup, p.root, p.target)["work"] == "in_progress"


# --------------------------------------------------------------------------- crash-prefix classification (real states)

def test_c1_lost_claim_reply_with_a_committed_matching_receipt_is_a_replay(harness, setup):
    p = make_plan(harness, setup)
    ck = key()
    mj = ModelJournal(W)
    Supervisor(FaultyClient(harness.base_url, lose_claim_reply=True), FakeWorker(lambda pkg, run: echo(pkg, run)),
               journal=journal_for(mj, p.root, ck)).run(p.root, ck, "att", "e1:ref")
    prefix = mj.prefix(p.root, ck, 1)                                   # crash right after claim_intent
    got = SupervisorClient(harness.base_url).get_claim(p.root, ck)
    env = parse_claim_envelope(got.raw, expected_root=p.root, expected_key=ck)
    held = prefix[0].payload
    matches = (env.receipt.attempt_id, env.receipt.executor_ref, env.receipt.actor) == (held["attemptId"], held["executorRef"], held["actor"])
    c = classify(prefix, Facts(reader=W, claim_found=True, claim_receipt_matches=matches))
    assert c.label == "C1:committed_replay" and c.automatic == ()


def test_c1_a_404_is_only_not_observed(harness, setup):
    p = make_plan(harness, setup)
    ck = key()
    mj = ModelJournal(W)
    Supervisor(FaultyClient(harness.base_url, malformed_claim_5xx=True), FakeWorker(lambda pkg, run: echo(pkg, run)),
               journal=journal_for(mj, p.root, ck)).run(p.root, ck, "att", None)
    assert SupervisorClient(harness.base_url).get_claim(p.root, ck).status == 404
    c = classify(mj.prefix(p.root, ck, 1), Facts(reader=W, claim_found=False))
    assert c.label == "C1:not_observed" and "operator_permit_new_claim" not in c.allowed


@pytest.mark.parametrize("n,label", [(2, "C2"), (3, "C3"), (6, "C6")])
def test_mid_run_prefixes_classify_without_writes(harness, setup, n, label):
    p = make_plan(harness, setup)
    ck = key()
    mj = ModelJournal(W)
    Supervisor(SupervisorClient(harness.base_url), FakeWorker(lambda pkg, run: echo(pkg, run, exit_code=7)),
               journal=journal_for(mj, p.root, ck)).run(p.root, ck, "att", None)
    before = db_snapshot(harness, p.root)
    assert classify(mj.prefix(p.root, ck, n), view_facts(setup, p.root, p.target)).label == label
    assert db_snapshot(harness, p.root) == before


# --------------------------------------------------------------------------- C7 via the coherent fixture snapshot

def finished(harness, setup, client=None, artifact="sha-c7"):
    p = make_plan(harness, setup)
    ck = key()
    mj = ModelJournal(W)
    s = Supervisor(client or SupervisorClient(harness.base_url), FakeWorker(lambda pkg, run: echo(pkg, run, artifact_ref=artifact)),
                   journal=journal_for(mj, p.root, ck))
    return p, ck, mj, s, s.run(p.root, ck, "att", "e1:c7")


def c7(harness, setup, p, ck, mj, r):
    facts_ = read_coherent(harness.psql, p.target)
    proof = prove_finish(facts_, r.held_finish, r.package)
    return proof, classify(mj.prefix(p.root, ck, 7), view_facts(setup, p.root, p.target, r.held_finish, finish_proof=proof))


def test_c7_proved_and_still_proved_after_a_same_key_unchanged_replay(harness, setup):
    p, ck, mj, s, r = finished(harness, setup)
    proof, c = c7(harness, setup, p, ck, mj, r)
    assert (proof, c.label) == ("proved", "C7:proved")
    resp, why = s.replay_held_finish(r)
    assert why == "replayed" and ok(resp)["outcome"] == "unchanged"   # no new event, no revision bump
    assert c7(harness, setup, p, ck, mj, r)[0] == "proved"


def test_c7_superseded_by_a_later_decision(harness, setup):
    p, ck, mj, s, r = finished(harness, setup)
    ok(setup.decide(p.target, {"decision": "accepted", "reviewedContentRevision": 1, "reviewedArtifactRef": "sha-c7",
                               "reviewedAttemptEpoch": 1, "evidenceRef": "rev", "operationKey": key(),
                               "expectedStateRevision": rev(setup, p.root, p.target), "actor": "e1-verifier"}))
    assert c7(harness, setup, p, ck, mj, r)[1].label == "C7:superseded"


def test_c7_superseded_by_a_structural_revision_bump_with_no_later_event(harness, setup):
    p, ck, mj, s, r = finished(harness, setup)
    events_before = len(read_coherent(harness.psql, p.target).events)
    sib = str(uuid.uuid4())                                                  # a new sibling in THIS plan
    ok(setup.add_child(p.root, sib, "sibling", 5, key(), rev(setup, p.root, p.root)))
    ok(setup.add_dependency(p.target, sib, key(), rev(setup, p.root, p.target), gate="completed"))   # bumps target revision
    f = read_coherent(harness.psql, p.target)
    assert len(f.events) == events_before                                    # no new attempt event
    assert prove_finish(f, r.held_finish, r.package) == "superseded"


def test_c7_unconfirmed_then_intervening_and_the_e1_heuristic_is_only_conservative(harness, setup):
    p = make_plan(harness, setup)
    ck = key()
    mj = ModelJournal(W)
    s = Supervisor(FaultyClient(harness.base_url, fail_finish_before_send=True),
                   FakeWorker(lambda pkg, run: echo(pkg, run, artifact_ref="sha-u")), journal=journal_for(mj, p.root, ck))
    r = s.run(p.root, ck, "att", "e1:u")
    assert r.reason == "uncertain:finish_reply"
    proof, c = c7(harness, setup, p, ck, mj, r)
    assert (proof, c.label) == ("unconfirmed", "C7:unconfirmed") and "finish_committed" in c.unknowns
    # Another actor finishes the same attempt with the same artifact under a DIFFERENT key.
    held = r.held_finish
    ok(setup.transition(p.target, {"to": "done", "attemptId": held["attemptId"], "attemptEpoch": held["attemptEpoch"],
                                   "artifactRef": held["artifactRef"], "executorRef": held["executorRef"],
                                   "operationKey": key(), "expectedStateRevision": held["expectedStateRevision"], "actor": "someone-else"}))
    proof, c = c7(harness, setup, p, ck, mj, r)
    assert (proof, c.label) == ("intervening", "C7:intervening")              # never attributed to this stream
    # The E1 view heuristic wrongly believes "ours", but the server refuses the resend: conservative, not proof.
    s._client.f["fail_finish_before_send"] = False
    before = db_snapshot(harness, p.root)
    resp, why = s.replay_held_finish(r)
    assert why == "replayed" and (resp.status, resp.code) == (409, "stale_revision")
    assert db_snapshot(harness, p.root) == before


def test_c7_partial_match_or_incomplete_facts_are_proof_missing(harness, setup):
    p, ck, mj, s, r = finished(harness, setup)
    f = read_coherent(harness.psql, p.target)
    assert prove_finish(f, dict(r.held_finish, artifactRef="sha-other"), r.package) == "proof_missing"
    incomplete = CoherentFacts(f.node, f.events[:-1], f.event_count, False)
    assert prove_finish(incomplete, r.held_finish, r.package) == "proof_missing"


# --------------------------------------------------------------------------- review derivation from real states

def leaf_and_node(setup, root, node_id):
    plan = ok(setup.plan(root))
    n = next(x for x in plan["nodes"] if x["id"] == node_id)
    return n, next((x for x in plan["readiness"]["leaves"] if x["nodeId"] == node_id), None)


def test_candidates_and_eligibility_follow_real_planstore_states(harness, setup):
    p, ck, mj, s, r = finished(harness, setup, artifact="sha-rv")
    d = derive_review(*leaf_and_node(setup, p.root, p.target))
    assert (d.review_class, d.accept_eligible) == ("candidate", "unverified:prerequisites")
    revise(setup, p.root, p.pred, "pred spec drifted")                       # upstream drift: gates no longer hold
    d = derive_review(*leaf_and_node(setup, p.root, p.target))
    assert (d.review_class, d.accept_eligible) == ("candidate", "ineligible:gates")

    p2, ck2, mj2, s2, r2 = finished(harness, setup, artifact="sha-rj")
    ok(setup.decide(p2.target, {"decision": "rejected", "reviewedContentRevision": 1, "reviewedArtifactRef": "sha-rj",
                                "reviewedAttemptEpoch": 1, "evidenceRef": "rev", "operationKey": key(),
                                "expectedStateRevision": rev(setup, p2.root, p2.target), "actor": "e1-verifier"}))
    assert derive_review(*leaf_and_node(setup, p2.root, p2.target)).review_class == "terminal_rejected"

    p3, ck3, mj3, s3, r3 = finished(harness, setup, artifact="sha-ac")
    ok(setup.decide(p3.target, {"decision": "accepted", "reviewedContentRevision": 1, "reviewedArtifactRef": "sha-ac",
                                "reviewedAttemptEpoch": 1, "evidenceRef": "rev", "operationKey": key(),
                                "expectedStateRevision": rev(setup, p3.root, p3.target), "actor": "e1-verifier"}))
    assert derive_review(*leaf_and_node(setup, p3.root, p3.target)).review_class == "accepted"
    revise(setup, p3.root, p3.target, "target spec v2")                      # accepted + own content change -> stale
    assert derive_review(*leaf_and_node(setup, p3.root, p3.target)).review_class == "operator_classification"

    p4, ck4, mj4, s4, r4 = finished(harness, setup, artifact="sha-ro")
    ok(setup.transition(p4.target, {"to": "in_progress", "attemptId": "att-2", "operationKey": key(),
                                    "expectedStateRevision": rev(setup, p4.root, p4.target), "actor": "e1-setup"}))   # reopen
    assert derive_review(*leaf_and_node(setup, p4.root, p4.target)).review_class == "not_done"
    ok(setup.transition(p4.target, {"to": "cancelled", "operationKey": key(),
                                    "expectedStateRevision": rev(setup, p4.root, p4.target), "actor": "e1-setup"}))
    assert derive_review(*leaf_and_node(setup, p4.root, p4.target)).review_class == "not_done"


def test_a_review_request_goes_moot_after_a_matching_decision_or_reopen(harness, setup):
    p, ck, mj, s, r = finished(harness, setup, artifact="sha-m")
    review = [p.root, p.target, r.held_finish["attemptId"], r.held_finish["attemptEpoch"], "sha-m"]
    mj.append(p.root, ck, "review_requested", {"review": review, "lead": "lead-1"})
    prefix = lambda: mj.prefix(p.root, ck, len(mj.streams[(p.root, ck)].records))
    assert classify(prefix(), view_facts(setup, p.root, p.target, r.held_finish)).label == "C10"
    ok(setup.decide(p.target, {"decision": "accepted", "reviewedContentRevision": 1, "reviewedArtifactRef": "sha-m",
                               "reviewedAttemptEpoch": 1, "evidenceRef": "rev", "operationKey": key(),
                               "expectedStateRevision": rev(setup, p.root, p.target), "actor": "e1-verifier"}))
    assert classify(prefix(), view_facts(setup, p.root, p.target, r.held_finish)).label == "review:moot"
