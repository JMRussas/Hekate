"""HK-ISSUE-012 (plan 038; TEST-ONLY, offline): a valid decision that belongs to a STRICTLY OLDER attempt
epoch leaves the current Done attempt unreviewed, so its exact ReviewKey is a review candidate. Every
other mismatching decision stays operator classification, an older accepted decision is never
`decided`, and the three derivations (acts.review_state, recovery.snapshot_review,
evidence.derive_review) agree. PlanStore is unchanged (plan 012 keeps the record as history)."""

import pytest

from e1 import acts as A
from e1 import handoff as H
from e1.evidence import derive_review, older_attempt_decision
from e1.recovery import snapshot_review
from test_e2c_model import LS1, LS2, act, done, fa, model, rkey
from test_e2d_model import PINS, TASK, evidence_of, pending_of, transition

KEY2 = rkey(LS1, attemptEpoch=2)          # round 2 REUSES attempt id "att-1" (the epoch is the proof)


def older(decision="rejected", **over) -> dict:
    """Round 2 Done (epoch 2) carrying round 1's decision (epoch 1) as history."""
    return done(**{"attempt_epoch": 2, "acc_decision": decision, "acc_attempt_id": "att-1", "acc_attempt_epoch": 1,
                   "acc_content_revision": 1, "acc_artifact_ref": "sha-round-1", **over})


EXACT = dict(acc_decision="accepted", acc_attempt_id="att-1", acc_attempt_epoch=1, acc_content_revision=1, acc_artifact_ref="sha-art")


# ------------------------------------------------------------------------ the guard itself

@pytest.mark.parametrize("decision, dep, cur, want", [
    ("rejected", 1, 2, True), ("accepted", 1, 2, True), ("accepted", 1, 9, True),
    ("rejected", 2, 2, False),                                          # same epoch
    ("rejected", 3, 2, False),                                          # future epoch
    ("rejected", None, 2, False), ("rejected", True, 2, False), ("rejected", False, 2, False),
    ("rejected", 0, 2, False), ("rejected", -1, 2, False), ("rejected", "1", 2, False), ("rejected", 1.0, 2, False),
    ("rejected", 1, True, False), ("rejected", 1, None, False), ("rejected", 1, "2", False),
    ("maybe", 1, 2, False), (None, 1, 2, False), (["rejected"], 1, 2, False),
])
def test_older_attempt_decision_guard(decision, dep, cur, want):
    assert older_attempt_decision(decision, dep, cur) is want


# ------------------------------------------------------------------------ acts.review_state

@pytest.mark.parametrize("n, key, want", [
    (older("rejected"), KEY2, "candidate"),
    (older("accepted"), KEY2, "candidate"),                             # never `decided`: an old approval is not revived
    (older("accepted", acc_artifact_ref="sha-art"), KEY2, "candidate"),  # same artifact, older epoch: still unreviewed
    (older("rejected", attempt_id="att-2"), rkey(LS1, attemptId="att-2", attemptEpoch=2), "candidate"),
    (older("rejected", acc_attempt_epoch=None), KEY2, "operator_classification"),
    (older("rejected", acc_attempt_epoch=True), KEY2, "operator_classification"),
    (older("rejected", acc_attempt_epoch=0), KEY2, "operator_classification"),
    (older("rejected", acc_attempt_epoch=-1), KEY2, "operator_classification"),
    (older("rejected", acc_attempt_epoch=3), KEY2, "operator_classification"),      # future epoch
    (older("rejected", acc_attempt_epoch="1"), KEY2, "operator_classification"),
    (older("maybe"), KEY2, "operator_classification"),                               # invalid decision value
    (done(**dict(EXACT, acc_decision="maybe")), rkey(LS1), "operator_classification"),  # invalid value is never decided
    (done(**EXACT), rkey(LS1), "decided"),
    (done(**dict(EXACT, acc_decision="rejected")), rkey(LS1), "decided"),
    (done(**dict(EXACT, content_revision=2)), rkey(LS1), "operator_classification"),               # same epoch, content drift
    (done(**dict(EXACT, acc_decision="rejected", content_revision=2)), rkey(LS1), "operator_classification"),
    (done(**dict(EXACT, acc_artifact_ref="sha-other")), rkey(LS1), "operator_classification"),     # same epoch, artifact drift
    (done(**dict(EXACT, acc_attempt_id="att-0")), rkey(LS1), "operator_classification"),           # same epoch, id drift
    (done(), rkey(LS1), "candidate"),
    (older("rejected"), rkey(LS1), "moot"),                              # a key for round 1 is not the current attempt
])
def test_review_state(n, key, want):
    assert A.review_state(n, key) == want


# ------------------------------------------------------------------------ the three derivations agree

def view_of(n: dict) -> tuple[dict, dict]:
    """The plan-view form of a node row: effectiveAcceptance as PlanRules.RawAcceptance derives it."""
    acc = None
    eff = "none"
    if n.get("acc_decision") is not None:
        acc = {"decision": n["acc_decision"], "contentRevision": n["acc_content_revision"], "artifactRef": n["acc_artifact_ref"],
               "attemptId": n.get("acc_attempt_id"), "attemptEpoch": n["acc_attempt_epoch"]}
        current = (n["acc_content_revision"] == n["content_revision"] and n["acc_artifact_ref"] == n["artifact_ref"]
                   and n["acc_attempt_epoch"] == n["attempt_epoch"])
        eff = n["acc_decision"] if current else "stale"
    view = {"work": n["work_status"], "effectiveAcceptance": eff, "acceptance": acc, "contentRevision": n["content_revision"],
            "attemptEpoch": n["attempt_epoch"], "artifactRef": n["artifact_ref"], "attemptContentRevision": n["attempt_content_revision"],
            "attemptPrereqDigest": n["attempt_prereq_digest"]}
    return view, {"gatesHold": True}


REACHABLE = {
    "none": (done(), rkey(LS1)),
    "exact_accepted": (done(**EXACT), rkey(LS1)),
    "exact_rejected": (done(**dict(EXACT, acc_decision="rejected")), rkey(LS1)),
    "older_rejected": (older("rejected"), KEY2),
    "older_accepted": (older("accepted"), KEY2),
    "older_accepted_same_artifact": (older("accepted", acc_artifact_ref="sha-art"), KEY2),
    "same_epoch_content_drift_accepted": (done(**dict(EXACT, content_revision=2)), rkey(LS1)),
    "same_epoch_content_drift_rejected": (done(**dict(EXACT, acc_decision="rejected", content_revision=2)), rkey(LS1)),
}


@pytest.mark.parametrize("name", sorted(REACHABLE))
def test_acts_recovery_and_evidence_agree(name):
    n, key = REACHABLE[name]
    a = A.review_state(n, key)
    r = snapshot_review(n)
    e = derive_review(*view_of(n)).review_class
    assert (a == "candidate") == (r == "candidate") == (e == "candidate"), (a, r, e)
    assert (a == "operator_classification") == (r == "operator_classification") == (e == "operator_classification"), (a, r, e)
    if name.startswith("older_"):
        assert (a, r, e) == ("candidate", "candidate", "candidate")
        assert derive_review(*view_of(n)).accept_eligible == "unverified:prerequisites"   # pins/gates still checked


@pytest.mark.parametrize("decision", ["accepted", "rejected"])
def test_same_epoch_other_attempt_id_fails_closed_in_acts_and_recovery(decision):
    """Root review (msg 1474): a same-epoch decision naming ANOTHER attempt id is invalid by
    PlanRules.ValidateState (PlanRules.cs 159), so it is unreachable through PlanStore; a raw row
    showing it (corruption, a trigger bypass) must never be decided by either raw-row derivation.
    evidence.derive_review is not applicable: PlanStore refuses to load such a snapshot, so no plan
    view is produced for it."""
    n = done(**dict(EXACT, acc_decision=decision, acc_attempt_id="att-other"))
    assert A.review_state(n, rkey(LS1)) == "operator_classification"
    assert snapshot_review(n) == "operator_classification"
    assert snapshot_review(done(**dict(EXACT, acc_decision=decision))) == "decided_unverified"      # the exact id still decides


def test_a_stale_view_without_a_provable_older_record_stays_operator_classification():
    v, leaf = view_of(older("rejected"))
    for bad in (None, {}, dict(v["acceptance"], attemptEpoch=None), dict(v["acceptance"], attemptEpoch=True),
                dict(v["acceptance"], attemptEpoch=2), dict(v["acceptance"], attemptEpoch=5), dict(v["acceptance"], decision="x")):
        assert derive_review(dict(v, acceptance=bad), leaf).review_class == "operator_classification"


# ------------------------------------------------------------------------ the review workflow on round 2

@pytest.mark.parametrize("decision", ["rejected", "accepted"])
def test_round_two_review_request_ack_and_handoff(decision):
    n = older(decision)
    m = model()
    d = m.request_review(KEY2, fa(n))
    assert d.outcome == "accepted"
    rid = A.review_id(KEY2)
    assert m.intake(act("review_acknowledged", KEY2, 1), fa(n)).outcome == "accepted"
    req = {"attemptKey": {k: KEY2[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": KEY2["artifactRef"]}
    cb = m.read(req, fa(n))
    assert cb["current"]["review"]["value"] not in ("operator_classification", "ended", "moot")
    # E2d prepare (mandatory proof needs a live candidate) and the commit decision.
    cls = A.review_state(n, KEY2)
    t = transition(m, rid)
    pkg = H.build(role="lead", cb=cb, planstore_class=cls, pins=PINS, basis_check=[cb["basis"]], pending=pending_of(m),
                  evidence=evidence_of(m, rid), task=TASK, transition=t)
    assert pkg.manifest["state"]["mandatory"]["planstoreClass"] == "candidate"
    fresh = H.semantic_set("lead", cb, pending_of(m), TASK["packageRef"], t["predecessorBindingId"], cls, PINS)
    c = H.decide_commit(m.view(), rid, t, pkg.candidate_digest, None, fresh, fresh)
    assert c.outcome == "accepted" and c.append[1]["leadSession"] == LS2


def test_round_two_becomes_decided_only_by_its_own_decision():
    n = older("rejected")
    assert A.review_state(n, KEY2) == "candidate"
    decided = dict(n, acc_decision="accepted", acc_attempt_epoch=2, acc_artifact_ref="sha-art")
    assert A.review_state(decided, KEY2) == "decided" and snapshot_review(decided) == "decided_unverified"
    m = model()
    assert m.request_review(KEY2, fa(decided)).outcome == "refused"     # a decided attempt is no longer a candidate
