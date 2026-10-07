"""E2c model (plan 030 rev 7 §12, sha256 52ef6784...; TEST-ONLY, offline): the act boundary over the
E2a ModelJournal. PlanStore facts are supplied by the test (route A = "as if read inside the locked
transaction"); tests/test_e2c_live.py makes route A real. Fixture constants only."""

import json
import uuid

import pytest

from e1 import acts as A
from e1.acts import (COUNTER_MAX, G, V, ActsModel, Malformed, PlanFacts, Refused, canonical, exec_id, parse_act,
                     parse_exec_key, parse_package, review_id)
from e1.evidence import ModelJournal

H = lambda c: c * 64                                     # noqa: E731 -- a 64-hex digest
ROOT, NODE = str(uuid.uuid4()), str(uuid.uuid4())
WS = f"hkw1:worker-a:{uuid.uuid4()}"
WS2 = f"hkw1:worker-b:{uuid.uuid4()}"
LS1, LS2, LS3 = (f"hkw1:lead-1:{uuid.uuid4()}" for _ in range(3))
PRE = H("e")


def package(**over) -> str:
    doc = {"kind": "supplied.v1", "suppliedSha256": H("a"), "instructions": {"system": H("b"), "fast": H("c"), "deep": H("d")},
           "contentRevision": 1, "contentDigest": H("f"), "prereqDigest": PRE}
    doc.update(over)
    return canonical(doc)


def ekey(**over) -> dict:
    k = {"rootId": ROOT, "nodeId": NODE, "attemptId": "att-1", "attemptEpoch": 1, "claimKey": "ck-1", "runId": "run-1",
         "packageRef": package(), "workerSession": WS}
    k.update(over)
    return k


def node(**over) -> dict:
    n = {"work_status": "in_progress", "attempt_id": "att-1", "attempt_epoch": 1, "executor_ref": WS, "content_revision": 1,
         "attempt_content_revision": 1, "attempt_prereq_digest": PRE, "artifact_ref": None, "acc_decision": None,
         "acc_content_revision": None, "acc_artifact_ref": None, "acc_attempt_epoch": None, "state_revision": 3}
    n.update(over)
    return n


def done(**over) -> dict:
    return node(**{"work_status": "done", "artifact_ref": "sha-art", **over})


def fa(n=None, rev=3, events=()) -> PlanFacts:
    return PlanFacts("A", n if n is not None else node(), revision=rev, basis={"tx": "route-a"}, events=tuple(events))


def fb(n=None, rev=3) -> PlanFacts:
    return PlanFacts("B", n if n is not None else node(), revision=rev, basis={"reads": [{"stateRevision": rev}]})


def act(kind, key, seq, cp=None, ev=None, **extra) -> str:
    d = {"kind": kind, "key": key, "actSeq": seq}
    if cp is not None or ev is not None:
        d["checkpointId"], d["evidenceDigest"] = cp, ev
    d.update(extra)
    return json.dumps(d)


def rkey(session, **over) -> dict:
    k = {"rootId": ROOT, "nodeId": NODE, "attemptId": "att-1", "attemptEpoch": 1, "artifactRef": "sha-art", "lead": "lead-1",
         "leadSession": session}
    k.update(over)
    return k


def model(now=100.0) -> ActsModel:
    return ActsModel(ROOT, "ck-1", now=now)


def dispatched(now=100.0, key=None) -> ActsModel:
    m = model(now)
    m.dispatch(key or ekey(), fa())
    return m


def status(m, req=None, facts=None, **kw):
    return m.read(req or {"executionKey": ekey()}, facts or fa(), **kw)["current"]


def phase(m, **kw):
    return status(m, **kw)["deadlinePhase"]["value"]


def ev(i: int) -> str:
    return f"{i:064x}"


# ======================================================================== 1. shapes and limits

def test_partial_keys_and_unknown_kinds_are_malformed_counter_only():
    m = dispatched()
    before = phase(m)
    partial = {k: v for k, v in ekey().items() if k != "workerSession"}
    for raw in (act("worker_ack", partial, 1), act("worker_nap", ekey(), 1), act("worker_ack", ekey(), 1, extra=1), "[]", "{"):
        assert m.intake(raw, fa()).outcome == "malformed"
    assert m.counters.values["_stream"]["malformed"] == 5 and phase(m) == before


@pytest.mark.parametrize("text,code", [
    (package().replace(",", ", "), "package_not_jcs"),
    (json.dumps(json.loads(package())), "package_not_jcs"),                       # key order not canonical
    (canonical({**json.loads(package()), "extra": 1}), "fields"),
    (canonical({k: v for k, v in json.loads(package()).items() if k != "contentDigest"}), "fields"),
    (canonical({**json.loads(package()), "kind": "supplied.v2"}), "package_kind"),
    (package(contentRevision=0), "integer_range"),
    (package(prereqDigest="sha256:" + PRE), "string"),
    (package(prereqDigest=PRE.upper()), "format"),
    (canonical({"kind": "stored.v1", "storeRef": "has space", "sha256": H("a")}), "format"),
    (canonical({"kind": "stored.v1", "storeRef": "s3://x", "sha256": H("a"), "packageToken": "t"}), "fields"),
])
def test_package_ref_wire_shape(text, code):
    with pytest.raises(Malformed) as e:
        parse_package(text)
    assert e.value.code == code


def test_representative_full_supplied_package_and_stored_form_parse():
    full = package()
    assert parse_package(full)["kind"] == "supplied.v1" and len(full) < 1024
    stored = canonical({"kind": "stored.v1", "sha256": H("a"), "storeRef": "pkg-store://e2c/1"})
    assert parse_package(stored)["kind"] == "stored.v1"


@pytest.mark.parametrize("field,value,ok", [
    ("attemptEpoch", 0, False), ("attemptEpoch", 1, True), ("attemptEpoch", 2**53 - 1, True), ("attemptEpoch", 2**53, False),
    ("attemptEpoch", True, False), ("attemptEpoch", "1", False), ("attemptEpoch", None, False),
])
def test_attempt_epoch_bounds(field, value, ok):
    k = ekey(**{field: value})
    if ok:
        assert parse_exec_key(k)[field] == value
    else:
        with pytest.raises(Malformed):
            parse_exec_key(k)


@pytest.mark.parametrize("token", ["0", "true", "false", '"1"', "null", "1.0", "1.5", "1e3", str(2**53)])
def test_integer_tokens_are_rejected_never_coerced(token):
    raw = act("worker_ack", ekey(), 1).replace('"actSeq": 1', f'"actSeq": {token}')
    with pytest.raises(Malformed):
        parse_act(raw)
    raw = act("worker_progress", ekey(), 2, 7, ev(1)).replace('"checkpointId": 7', f'"checkpointId": {token}')
    with pytest.raises(Malformed):
        parse_act(raw)


def test_integer_maximum_is_accepted():
    assert parse_act(act("worker_ack", ekey(), 2**53 - 1)).act_seq == 2**53 - 1
    assert parse_act(act("worker_progress", ekey(), 2, 2**53 - 1, ev(1))).checkpoint_id == 2**53 - 1


def test_oversize_payload_is_malformed():
    big = act("worker_ack", ekey(), 1)[:-1] + ', "pad": "' + "x" * (16 * 1024) + '"}'
    with pytest.raises(Malformed) as e:
        parse_act(big)
    assert e.value.code == "payload_size"


@pytest.mark.parametrize("executor", [None, "worker-a", "hkw1:Worker-A:" + str(uuid.uuid4()), WS2])
def test_null_nonconforming_or_other_executor_ref_is_not_dispatchable(executor):
    m = model()
    with pytest.raises(Refused) as e:
        m.dispatch(ekey(), fa(node(executor_ref=executor)))
    assert e.value.code == "not_dispatchable" and (exec_id(ekey()), "not_dispatchable") in m.queue
    assert not m.view().dispatch                                   # nothing recorded


def test_session_not_byte_equal_to_executor_ref_is_foreign():
    m = dispatched()
    d = m.intake(act("worker_ack", ekey(workerSession=WS2), 1), fa())
    # A different session is a different ExecutionKey: there is no dispatch for it byte for byte.
    assert d.outcome in ("foreign_session", "stale")
    assert phase(m)["phase"] == "ack"


# ======================================================================== 2. §6 outcomes (counter only, no deadline effect)

def accept_ack(m, seq=1, at=None):
    if at is not None:
        m.now = at
    d = m.intake(act("worker_ack", ekey(), seq), fa())
    assert d.outcome == "accepted", d
    return d


def test_section6_outcomes_are_counter_only_and_never_move_the_deadline():
    m = dispatched()
    accept_ack(m, at=200.0)
    m.now = 300.0
    assert m.intake(act("worker_progress", ekey(), 2, 1, ev(1)), fa()).outcome == "accepted"
    anchor = phase(m)
    m.now = 400.0
    cases = [
        (act("worker_progress", ekey(), 3, 2, ev(2)), fa(node(work_status="todo")), "stale"),
        (act("worker_progress", ekey(), 3, 2, ev(2)), fa(node(content_revision=2)), "stale"),            # stale_content
        (act("worker_progress", ekey(), 2, 1, ev(1)), fa(), "duplicate"),
        (act("worker_progress", ekey(), 2, 5, ev(9)), fa(), "conflict"),
        (act("worker_ack", ekey(), 9), fa(), "conflict"),                                               # re-ACK
        (act("worker_progress", ekey(), 1, 5, ev(5)), fa(), "out_of_order"),
        (act("worker_progress", ekey(), 4, 1, ev(4)), fa(), "not_novel"),                               # checkpoint not greater
        (act("worker_progress", ekey(), 4, 9, ev(1)), fa(), "not_novel"),                               # replayed old digest
    ]
    for raw, facts, want in cases:
        assert m.intake(raw, facts).outcome == want, want
        assert phase(m) == anchor
    c = m.counters.values[exec_id(ekey())]
    assert c == {"stale": 2, "duplicate": 1, "conflict": 2, "out_of_order": 1, "not_novel": 2}
    assert {r for _, r in m.queue} == {"conflict"}                 # one entry per key per reason


def test_duplicate_returns_the_original_record():
    m = dispatched()
    first = accept_ack(m)
    dup = m.intake(act("worker_ack", ekey(), 1), fa())
    assert dup.outcome == "duplicate" and dup.original.kind == "worker_ack" and dup.original.payload["actId"] == first.append[1]["actId"]


def test_progress_before_ack_is_out_of_order():
    m = dispatched()
    assert m.intake(act("worker_progress", ekey(), 1, 1, ev(1)), fa()).outcome == "out_of_order"


def test_novelty_unknown_fails_closed_with_one_operator_entry():
    m = dispatched()
    accept_ack(m)
    m.corrupt = "chain:9"
    for i in range(3):
        assert m.intake(act("worker_progress", ekey(), 2 + i, 1 + i, ev(1 + i)), fa()).outcome == "novelty_unknown"
    assert [r for _, r in m.queue].count("novelty_unknown") == 1


def test_route_b_is_stale_when_its_read_shows_stale():
    m = dispatched()
    assert m.intake(act("worker_ack", ekey(), 1), fb(node(work_status="todo"))).outcome == "stale"


# ======================================================================== 3. deadline phases

def test_worker_phases_ack_then_first_progress_then_progress():
    m = dispatched(now=100.0)
    p = phase(m)
    assert (p["phase"], p["anchorKind"], p["anchorAt"], p["deadline"]) == ("ack", "dispatch_intent", 100.0, 1000.0)
    accept_ack(m, at=200.0)
    p = phase(m)
    assert (p["phase"], p["anchorKind"], p["anchorAt"], p["deadline"]) == ("first_progress", "worker_ack", 200.0, 2000.0)
    m.now = 2500.0
    assert status(m)["status"]["value"] == "first_progress_overdue"
    assert m.intake(act("worker_progress", ekey(), 2, 1, ev(1)), fa()).outcome == "accepted"
    p = phase(m)                                                   # a late act anchors the next phase at its own `at`
    assert (p["phase"], p["anchorAt"], p["deadline"]) == ("progress", 2500.0, 4300.0)
    m.now = 2600.0
    assert m.intake(act("worker_progress", ekey(), 3, 2, ev(2)), fa()).outcome == "accepted"
    assert phase(m)["anchorAt"] == 2600.0 and status(m)["status"]["value"] == "progress_self_reported"


def test_ack_overdue_then_satisfied():
    m = dispatched(now=0.0)
    assert status(m, now=900.0)["status"]["value"] == "ack_overdue"
    accept_ack(m, at=950.0)
    assert phase(m)["anchorAt"] == 950.0


def test_one_live_deadline_and_phase_ends_with_the_stream():
    m = dispatched()
    accept_ack(m)
    assert phase(m, facts=fa(done()))["phase"] == "ended"


def test_review_phases_and_late_act_anchoring():
    m, rid, root = requested_review(now=1000.0, session=LS1)
    p = rphase(m)
    assert (p["phase"], p["anchorKind"], p["anchorAt"]) == ("ack", "review_requested", 1000.0)
    m.now = 1100.0
    assert m.intake(act("review_acknowledged", rkey(LS1), 1), fa(done())).outcome == "accepted"
    assert (rphase(m)["phase"], rphase(m)["anchorAt"]) == ("first_progress", 1100.0)
    m.now = 3500.0
    assert rread(m)["current"]["review"]["value"] == "review_overdue"
    assert m.intake(act("review_progress", rkey(LS1), 2, 1, ev(1)), fa(done())).outcome == "accepted"
    assert (rphase(m)["phase"], rphase(m)["anchorAt"]) == ("progress", 3500.0)


# ======================================================================== 4. confirm_act keeps the original anchor

def test_confirm_act_anchors_at_the_original_observation_at_never_the_confirmation():
    m = dispatched(now=100.0)
    m.now = 200.0
    obs = m.intake(act("worker_ack", ekey(), 1), fb())
    assert obs.outcome == "observed"
    assert phase(m)["phase"] == "ack"                              # unverified: no status advance
    m.now = 5000.0
    assert m.confirm(obs.append[1]["obsId"], "recon-1").outcome == "accepted"
    p = phase(m)
    assert (p["phase"], p["anchorAt"], p["deadline"]) == ("first_progress", 200.0, 2000.0)
    assert status(m)["status"]["value"] == "first_progress_overdue"    # already past: overdue at once
    assert m.confirm(obs.append[1]["obsId"], "recon-1").outcome == "duplicate"


def test_confirm_reruns_novelty_and_identity():
    m = dispatched()
    accept_ack(m)
    assert m.intake(act("worker_progress", ekey(), 2, 1, ev(1)), fb()).outcome == "observed"
    assert m.intake(act("worker_progress", ekey(), 3, 2, ev(1)), fb()).outcome == "observed"  # route B never advances bookkeeping
    obs = m.intake(act("worker_progress", ekey(), 10, 10, ev(2)), fb())
    assert obs.outcome == "observed"
    assert m.intake(act("worker_progress", ekey(), 4, 4, ev(2)), fa()).outcome == "accepted"   # same content, effective first
    assert m.confirm(obs.append[1]["obsId"], "recon").outcome == "not_novel"                    # re-checked at confirm time
    late = m.view().observations[exec_id(ekey())][0].payload["obsId"]
    assert m.confirm(late, "recon").outcome == "out_of_order"
    assert m.confirm("missing", "recon").outcome == "refused" and m.confirm(obs.append[1]["obsId"], " ").outcome == "refused"


# ======================================================================== 5. route B audit-only; matching revisions are not coherence

def test_route_b_observations_never_advance_status_or_deadlines_and_are_bounded():
    m = dispatched()
    accept_ack(m, at=150.0)
    before = phase(m)
    for i in range(V):
        assert m.intake(act("worker_progress", ekey(), 2 + i, 1 + i, ev(1 + i)), fb()).outcome == "observed"
    assert m.intake(act("worker_progress", ekey(), 99, 99, ev(99)), fb()).outcome == "observation_overflow"
    assert phase(m) == before and status(m)["progress"]["value"]["checkpoints"] == 0
    hist = m.read({"executionKey": ekey()}, fa(), limit=200)["historical"]["items"]
    assert sum(1 for h in hist if h["kind"] == "act_observation" and h["currentness"] == "unverified") == V


def test_matching_revisions_from_independent_reads_produce_no_current_section():
    m = dispatched()
    accept_ack(m)
    r = m.read({"executionKey": ekey()}, fb(rev=3))                # same revision as the route A facts
    assert r["current"] == "unknown" and r["basis"]["route"] == "B"
    assert m.read({"executionKey": ekey()}, fa(rev=3))["current"]["ack"]["proof"] == "complete"


def test_superseded_as_of_read_is_an_audit_record_only():
    m = dispatched()
    obs = m.intake(act("worker_ack", ekey(), 1), fb())
    before = phase(m)
    m.mj.append(ROOT, "ck-1", "superseded_as_of_read", {"observationId": obs.append[1]["obsId"], "laterRevision": 9})
    assert phase(m) == before


# ======================================================================== 6. restart (model: rehydrate from records only)

def test_rehydrating_from_the_records_keeps_original_anchors():
    m = dispatched(now=100.0)
    accept_ack(m, at=200.0)
    m.now = 300.0
    m.intake(act("worker_progress", ekey(), 2, 1, ev(1)), fa())
    recs = m.records()
    fresh = A.View(list(recs))
    m.now = 99_000.0                                               # "restart" much later
    assert A.read_cb(fresh, {"executionKey": ekey()}, fa(), now=99_000.0)["current"]["deadlinePhase"]["value"]["anchorAt"] == 300.0


# ======================================================================== 7. stream ends

def test_matching_finish_by_another_actor_is_finished_not_superseded():
    m = dispatched()
    accept_ack(m)
    s = status(m, facts=fa(done()))
    assert s["status"]["value"] == "finished" and s["ack"]["proof"] == "complete"
    assert m.record_end(ekey(), fa(done())).outcome == "accepted"
    assert m.intake(act("worker_progress", ekey(), 2, 1, ev(1)), fa(done())).outcome == "stale"   # late act
    assert status(m, facts=fa(done()))["ack"]["value"] is not None                                 # evidence kept


@pytest.mark.parametrize("n,events,reason", [
    (node(work_status="todo", attempt_id=None, executor_ref=None), [{"kind": "attempt_released", "attempt_id": "att-1", "attempt_epoch": 1}], "released"),
    (node(work_status="cancelled", attempt_id=None), [{"kind": "attempt_cancelled", "attempt_id": "att-1", "attempt_epoch": 1}], "cancelled"),
    (node(attempt_id="att-2", attempt_epoch=2), [{"kind": "attempt_reopened", "attempt_id": "att-2", "attempt_epoch": 2}], "reopened"),
    (node(attempt_id="att-3", attempt_epoch=3), [], "epoch_replaced"),
])
def test_supersession_cases_are_distinct(n, events, reason):
    m = dispatched()
    accept_ack(m)
    assert status(m, facts=fa(n, events=events))["status"]["value"] == f"superseded:{reason}"
    assert m.record_end(ekey(), fa(n, events=events)).outcome == "accepted"
    hist = m.read({"executionKey": ekey()}, fa(n, events=events))["historical"]["items"]
    assert any(h["kind"] == "superseded" and h["reason"] == reason for h in hist)


DECIDED = dict(acc_decision="accepted", acc_content_revision=1, acc_artifact_ref="sha-art", acc_attempt_id="att-1", acc_attempt_epoch=1)


def test_review_ends_on_an_exact_decision_and_is_moot_otherwise():
    m, rid, _ = requested_review(session=LS1)
    cur = rread(m, facts=fa(done(**DECIDED)))["current"]
    assert cur["review"]["value"] == "ended" and cur["acceptanceValidity"] == {"value": "unknown", "proof": "unverified"}
    assert rread(m, facts=fa(node(attempt_id="att-9", attempt_epoch=2)))["current"]["review"]["value"] == "moot"


@pytest.mark.parametrize("over", [{"acc_attempt_id": "att-0"}, {"acc_attempt_epoch": 7}, {"acc_artifact_ref": "sha-other"},
                                  {"acc_content_revision": 9}])
def test_a_decision_against_another_identity_is_operator_classification_not_ended(over):
    m, rid, _ = requested_review(session=LS1)
    n = done(**{**DECIDED, **over})
    cur = rread(m, facts=fa(n))["current"]
    assert cur["review"]["value"] == "operator_classification" and "acceptanceValidity" not in cur
    assert m.intake(act("review_acknowledged", rkey(LS1), 1), fa(n)).outcome == "stale"
    assert (rid, "operator_classification") in m.queue


# ======================================================================== 8. saturation

def test_g_cap_preserves_established_status_checkpoints_and_deadline():
    m = dispatched()
    accept_ack(m)
    for i in range(1, G + 1):
        m.now = 1000.0 + i
        assert m.intake(act("worker_progress", ekey(), 1 + i, i, ev(i)), fa()).outcome == "accepted"
    before = status(m)
    m.now = 5000.0
    assert m.intake(act("worker_progress", ekey(), 500, 500, ev(500)), fa()).outcome == "progress_overflow"
    after = status(m)
    assert after["progress"] == before["progress"] and after["progress"]["value"]["capReached"] is True
    assert after["deadlinePhase"]["value"]["anchorAt"] == 1000.0 + G and after["progress"]["proof"] == "complete"
    assert [r for _, r in m.queue].count("progress_overflow") == 1


def test_counter_saturation_is_flagged_and_diagnostic_only():
    m = dispatched()
    accept_ack(m)
    sk = exec_id(ekey())
    m.counters.values[sk] = {"not_novel": COUNTER_MAX - 1}
    before = status(m)
    for _ in range(3):
        m.intake(act("worker_ack", ekey(), 1), fa())                  # duplicates
    m.counters.bump(sk, "not_novel")
    m.counters.bump(sk, "not_novel")
    assert m.counters.values[sk]["not_novel"] == COUNTER_MAX and "not_novel" in m.counters.saturated[sk]
    after = status(m)
    assert after == before


def test_damaged_progress_bookkeeping_hides_only_progress():
    m = dispatched()
    accept_ack(m)
    m.intake(act("worker_progress", ekey(), 2, 1, ev(1)), fa())
    last = m.records()[-1].seq
    m.corrupt, m.corrupt_from = f"chain:{last}", last              # damage after the ACK, at the progress record
    s = status(m)
    assert s["ack"]["proof"] == "complete" and s["ack"]["value"] is not None
    assert s["progress"] == {"value": "unknown", "proof": "corrupt"}


def test_damage_reaching_the_ack_makes_ack_unknown():
    m = dispatched()
    accept_ack(m)
    ack_seq = m.records()[-1].seq
    m.corrupt, m.corrupt_from = f"chain:{ack_seq}", ack_seq
    s = status(m)
    assert s["ack"]["proof"] == "corrupt" and s["progress"]["proof"] == "corrupt"


# ======================================================================== 9. bindings

def requested_review(now=100.0, session=None):
    m = model(now)
    d = m.request_review(rkey(session), fa(done()))
    assert d.outcome == "accepted"
    return m, review_id(rkey(session)), d.append[1]["linkId"]


def rread(m, req=None, facts=None, **kw):
    return m.read(req or {"attemptKey": {k: rkey(None)[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art"},
                  facts or fa(done()), **kw)


def rphase(m):
    return rread(m)["current"]["deadlinePhase"]["value"]


def test_atomic_bind_and_ack_then_refusal_when_already_bound():
    m, rid, _ = requested_review(session=None)
    assert rread(m)["identity"]["reviewKey"]["leadSession"] is None
    assert m.intake(act("review_acknowledged", rkey(LS1), 1), fa(done())).outcome == "accepted"
    assert rread(m)["identity"]["reviewKey"]["leadSession"] == LS1
    assert m.intake(act("review_acknowledged", rkey(LS2), 1), fa(done())).outcome == "foreign_session"
    assert rread(m)["identity"]["reviewKey"]["leadSession"] == LS1


def test_rebind_never_reanchors_and_needs_a_gate():
    m, rid, root = requested_review(now=100.0, session=LS1)
    before = rphase(m)
    cur = m.view().chain(rid).current.link_id
    assert m.bind(rid, "review_rebind", cur, LS2, gate="release", gate_ref=LS3).outcome == "refused"
    m.now = 50_000.0
    assert m.bind(rid, "review_rebind", cur, LS2, gate="release", gate_ref=LS1).outcome == "accepted"
    after = rphase(m)                                             # binding changes identity, never anchors
    assert (after["phase"], after["anchorAt"], after["deadline"]) == (before["phase"], before["anchorAt"], before["deadline"])
    assert m.bind(rid, "review_rebind", cur, LS3, gate="operator", gate_ref="recon").outcome == "stale_binding"   # CAS


def test_rebind_gives_session_distinct_act_ids_without_resetting_anchors_or_novelty():
    m, rid, root = requested_review(now=100.0, session=LS1)
    m.now = 200.0
    assert m.intake(act("review_acknowledged", rkey(LS1), 1), fa(done())).outcome == "accepted"
    m.now = 300.0
    assert m.intake(act("review_progress", rkey(LS1), 2, 1, ev(1)), fa(done())).outcome == "accepted"
    a1 = parse_act(act("review_progress", rkey(LS1), 3, 2, ev(2)))
    a2 = parse_act(act("review_progress", rkey(LS2), 3, 2, ev(2)))
    assert a1.act_id != a2.act_id and a1.stream_key == a2.stream_key          # full-key ActId, stable bookkeeping key
    before = rphase(m)
    cur = m.view().chain(rid).current.link_id
    m.now = 400.0
    assert m.bind(rid, "review_rebind", cur, LS2, gate="release", gate_ref=LS1).outcome == "accepted"
    assert rphase(m) == before
    assert m.intake(act("review_progress", rkey(LS2), 2, 5, ev(5)), fa(done())).outcome == "out_of_order"   # seq not reset
    assert m.intake(act("review_progress", rkey(LS2), 3, 6, ev(1)), fa(done())).outcome == "not_novel"     # digest not reset
    assert m.intake(act("review_progress", rkey(LS1), 3, 6, ev(6)), fa(done())).outcome == "foreign_session"
    m.now = 500.0
    assert m.intake(act("review_progress", rkey(LS2), 3, 6, ev(6)), fa(done())).outcome == "accepted"
    assert (rphase(m)["phase"], rphase(m)["anchorAt"]) == ("progress", 500.0)


def test_missing_or_unrepresentable_planstore_facts_give_unknown_current():
    m = dispatched()
    accept_ack(m)
    r = m.read({"executionKey": ekey()}, PlanFacts("A", None))
    assert r["current"] == "unknown" and r["unknownReason"] == "no_planstore_facts"
    for name, v in (("attempt_epoch", 2**53), ("state_revision", 2**63 - 1), ("content_revision", True), ("attempt_content_revision", -1)):
        r = m.read({"executionKey": ekey()}, fa(node(**{name: v})))
        assert r["current"] == "unknown" and r["unknownReason"] == f"fact_out_of_range:{name}"
        assert m.intake(act("worker_progress", ekey(), 2, 1, ev(1)), fa(node(**{name: v}))).outcome == "stale"
    assert m.read({"executionKey": ekey()}, fa(rev=2**53))["unknownReason"] == "fact_out_of_range:revision"


def test_review_assigned_only_for_an_unbound_review():
    m, rid, root = requested_review(session=None)
    assert m.bind(rid, "review_assigned", root, LS1, gate="operator", gate_ref="recon").outcome == "accepted"
    cur = m.view().chain(rid).current.link_id
    assert m.bind(rid, "review_assigned", cur, LS2, gate="operator", gate_ref="recon").outcome == "refused"


def test_there_is_no_worker_rebind_inside_an_attempt():
    m = dispatched()
    accept_ack(m)
    d = m.intake(act("worker_progress", ekey(workerSession=WS2), 2, 1, ev(1)), fa(node(executor_ref=WS2)))
    assert d.outcome == "foreign_session"                          # the attempt's session is fixed by its executorRef


# ======================================================================== 10. C-B identity, binding chain and per-field proof

AK = {k: ekey()[k] for k in A.ATTEMPT_FIELDS}


def two_runs():
    m = dispatched()
    m.dispatch(ekey(runId="run-2", packageRef=package(suppliedSha256=H("9"))), fa())
    return m


def test_identity_two_runs_two_packages_and_session_mismatch():
    m = two_runs()
    with pytest.raises(Refused) as e:
        m.read({"attemptKey": AK}, fa())
    assert e.value.code == "ambiguous_key"
    r = m.read({"attemptKey": AK, "runId": "run-2"}, fa())
    assert r["identity"]["executionKey"] == ekey(runId="run-2", packageRef=package(suppliedSha256=H("9")))
    with pytest.raises(Refused) as e:
        m.read({"attemptKey": AK, "runId": "run-2", "packageRef": package()}, fa())
    assert (e.value.code, e.value.detail) == ("identity_mismatch", ["packageRef"])
    with pytest.raises(Refused) as e:
        m.read({"attemptKey": AK, "runId": "run-1", "workerSession": WS2}, fa())
    assert (e.value.code, e.value.detail) == ("identity_mismatch", ["workerSession"])
    with pytest.raises(Refused) as e:
        m.read({"executionKey": ekey(packageRef=package(suppliedSha256=H("9")))}, fa())
    assert e.value.code == "identity_mismatch"
    with pytest.raises(Refused) as e:
        m.read({"attemptKey": AK, "runId": "run-404"}, fa())
    assert e.value.code == "unknown_key"


def test_single_run_attempt_lookup_echoes_the_full_key():
    m = dispatched()
    assert m.read({"attemptKey": AK}, fa())["identity"]["executionKey"] == ekey()


def test_duplicate_run_mapping_is_ambiguous_never_resolved():
    m = dispatched()
    m.dispatch(ekey(packageRef=package(suppliedSha256=H("7"))), fa(), allow_duplicate_run=True)   # injected corruption
    with pytest.raises(Refused) as e:
        m.read({"attemptKey": AK, "runId": "run-1"}, fa())
    assert e.value.code == "ambiguous_key"
    d = m.intake(act("worker_ack", ekey(), 1), fa())
    assert d.outcome == "stale" and (exec_id(ekey()), "ambiguous_key") in m.queue


def test_malformed_requests_are_rejected():
    m = dispatched()
    for req in ({"attemptKey": {**AK, "attemptEpoch": 0}}, {"attemptKey": AK, "extra": 1}, {"nodeId": NODE}):
        with pytest.raises(Malformed):
            m.read(req, fa())


def test_review_chain_before_binding_after_bind_assign_and_rebind():
    m, rid, root = requested_review(session=None)
    assert rread(m)["identity"]["reviewKey"]["leadSession"] is None
    with pytest.raises(Refused) as e:
        rread(m, req={"reviewKey": rkey(LS1)})
    assert e.value.code == "identity_mismatch"
    anchors = rphase(m)
    assert m.intake(act("review_acknowledged", rkey(LS1), 1), fa(done())).outcome == "accepted"
    assert rread(m, req={"reviewKey": rkey(LS1)})["identity"]["reviewKey"]["leadSession"] == LS1
    for bad in (None, LS2):
        with pytest.raises(Refused) as e:
            rread(m, req={"reviewKey": rkey(bad)})
        assert e.value.code == "identity_mismatch"
    cur = m.view().chain(rid).current.link_id
    assert m.bind(rid, "review_rebind", cur, LS2, gate="operator", gate_ref="recon").outcome == "accepted"
    r = rread(m, req={"reviewKey": rkey(LS2)})
    assert r["identity"]["reviewKey"]["leadSession"] == LS2
    assert [h["leadSession"] for h in r["historical"]["items"] if h["kind"] == "binding"] == [None, LS1]
    with pytest.raises(Refused) as e:
        rread(m, req={"reviewKey": rkey(LS1)})
    assert e.value.code == "stale_binding"
    assert rphase(m)["anchorAt"] == anchors["anchorAt"] or rphase(m)["phase"] == "first_progress"


def test_review_assigned_lookup_echoes_assigned_session():
    m, rid, root = requested_review(session=None)
    m.bind(rid, "review_assigned", root, LS2, gate="operator", gate_ref="recon")
    assert rread(m)["identity"]["reviewKey"]["leadSession"] == LS2


def inject_link(m, rid, pred, session, kind="review_rebind"):
    m.mj.append(ROOT, "ck-1", kind, {"reviewId": rid, "predecessorBindingId": pred, "linkId": str(uuid.uuid4()), "lead": "lead-1",
                                     "leadSession": session, "gate": "operator", "gateRef": "inject"})


def test_forked_or_gapped_chain_is_ambiguous_and_never_picks_one():
    m, rid, root = requested_review(session=LS1)
    inject_link(m, rid, root, LS2)
    inject_link(m, rid, root, LS3)                                 # fork
    with pytest.raises(Refused) as e:
        rread(m)
    assert e.value.code == "ambiguous_binding"
    assert m.intake(act("review_acknowledged", rkey(LS2), 1), fa(done())).outcome == "stale"
    assert ((rid, "ambiguous_binding") in m.queue)
    m2, rid2, _ = requested_review(session=LS1)
    inject_link(m2, rid2, str(uuid.uuid4()), LS2)                  # gap
    with pytest.raises(Refused) as e:
        rread(m2)
    assert e.value.code == "ambiguous_binding"


def test_equal_and_regressing_at_resolve_by_linkage_and_sequence():
    m, rid, root = requested_review(now=500.0, session=LS1)
    m.now = 500.0                                                  # equal `at`
    assert m.bind(rid, "review_rebind", root, LS2, gate="operator", gate_ref="r1").outcome == "accepted"
    m.now = 10.0                                                   # regressing `at`
    cur = m.view().chain(rid).current.link_id
    assert m.bind(rid, "review_rebind", cur, LS3, gate="operator", gate_ref="r2").outcome == "accepted"
    assert rread(m)["identity"]["reviewKey"]["leadSession"] == LS3
    p = rphase(m)
    assert (p["phase"], p["anchorAt"]) == ("ack", 500.0)            # anchor = review_requested, unchanged


def test_successor_with_non_increasing_sequence_is_ambiguous():
    m, rid, root = requested_review(session=LS1)
    recs = m.records()
    req = next(r for r in recs if r.kind == "review_requested")
    fake = A.Record(req.seq, "review_rebind", req.writer, req.root, req.claim_key, req.at,
                    canonical({"reviewId": rid, "predecessorBindingId": root, "linkId": str(uuid.uuid4()), "lead": "lead-1",
                               "leadSession": LS2, "gate": "operator", "gateRef": "x"}))
    with pytest.raises(Refused) as e:
        A.read_cb(A.View(recs + [fake]), {"attemptKey": AK, "artifactRef": "sha-art"}, fa(done()), now=0.0)
    assert e.value.code == "ambiguous_binding"


def test_per_field_proof_missing_ack_outcome_vs_corrupt_progress():
    m = dispatched()
    sk = exec_id(ekey())
    s = m.read({"executionKey": ekey()}, fa(), pending={f"{sk}:ack": "not_observed"})["current"]
    assert s["ack"]["proof"] == "not_observed" and s["progress"]["proof"] == "not_observed"
    assert s["deadlinePhase"]["proof"] == "not_observed"
    accept_ack(m)
    m.intake(act("worker_progress", ekey(), 2, 1, ev(1)), fa())
    s = m.read({"executionKey": ekey()}, fa(), pending={f"{sk}:progress": "compacted"})["current"]
    assert s["ack"]["proof"] == "complete" and s["progress"]["proof"] == "compacted"


def test_truncated_historical_page_changes_no_field():
    m = dispatched()
    accept_ack(m)
    for i in range(5):
        m.transport("launched", f"pid-{i}")
    full = m.read({"executionKey": ekey()}, fa(), limit=200)
    page = m.read({"executionKey": ekey()}, fa(), limit=2)
    assert page["historical"]["truncated"] is True and page["historical"]["nextCursor"] is not None
    assert page["current"] == full["current"]
    nxt = m.read({"executionKey": ekey()}, fa(), limit=2, after=page["historical"]["nextCursor"])
    assert [h["seq"] for h in nxt["historical"]["items"]] == [h["seq"] for h in full["historical"]["items"]][2:4]


def test_compacting_an_unrelated_stream_changes_no_field():
    m = dispatched()
    accept_ack(m)
    other = ModelJournal("supervisor-e2c")
    before = status(m)
    other.append("other-root", "ck-x", "claim_intent", {})
    assert status(m) == before


def test_stale_basis_marks_the_affected_fields():
    m = dispatched()
    accept_ack(m)
    r = m.read({"executionKey": ekey()}, fa(rev=3), latest_revision=4)
    assert set(r["staleFields"]) == {"ack", "progress", "deadlinePhase", "status"}
    assert all(r["current"][f]["value"] == "unknown" for f in r["staleFields"])


def test_prerequisites_are_reported_unverified_separately():
    m = dispatched()
    assert status(m)["prerequisites"] == {"value": "unknown", "proof": "unverified"}


def test_page_limits():
    m = dispatched()
    for bad in (0, A.PAGE_MAX + 1, True, 1.5):
        with pytest.raises(Malformed):
            m.read({"executionKey": ekey()}, fa(), limit=bad)
    assert m.read({"executionKey": ekey()}, fa(), after=0)["historical"]["items"] == []


# ======================================================================== 11. transport and launch are not acts

def test_transport_ack_launched_and_completed_change_nothing():
    m = dispatched(now=100.0)
    before = status(m)
    m.dispatch_outcome(exec_id(ekey()), "delivered", "bridge:1161")
    for fact in ("bridge_ack", "launched", "completed"):
        m.transport(fact, f"{fact}-ref")
    s = status(m)
    assert s["deadlinePhase"] == before["deadlinePhase"] and s["ack"] == before["ack"]
    assert s["status"]["value"] == "delivered"                      # delivered is transport, never taken up
    hist = m.read({"executionKey": ekey()}, fa())["historical"]["items"]
    assert {h["fact"] for h in hist if h["kind"] == "transport_observed"} == {"bridge_ack", "launched", "completed"}


def test_an_act_for_another_stream_is_malformed():
    m = dispatched()
    for k in (ekey(rootId=str(uuid.uuid4())), ekey(claimKey="ck-other")):
        assert m.intake(act("worker_ack", k, 1), fa()).outcome == "malformed"
    assert m.intake(act("review_acknowledged", rkey(LS1, rootId=str(uuid.uuid4())), 1), fa(done())).outcome == "malformed"


def test_arbitrary_inbound_keys_share_one_bounded_counter_bucket():
    m = dispatched()
    for i in range(50):
        other = f"hkw1:worker-x:{uuid.uuid4()}"
        m.intake(act("worker_ack", ekey(workerSession=other, runId=f"run-x{i}"), 1), fa())
        m.intake(act("review_acknowledged", rkey(LS1, artifactRef=f"sha-{i}"), 1), fa(done(artifact_ref=f"sha-{i}")))
    assert set(m.counters.values) == {"_unknown_key"} and m.counters.values["_unknown_key"]["stale"] == 100
    assert {k for k, _ in m.queue} <= {"_unknown_key"}


def test_planstore_content_digest_conversion_is_explicit():
    assert A.content_digest_from_planstore("AB" * 32) == "ab" * 32
    for bad in ("ab" * 32, "AB" * 31, None, "G" * 64):
        with pytest.raises(Malformed):
            A.content_digest_from_planstore(bad)


def test_selector_and_facts_must_agree_with_the_resolved_identity():
    m = dispatched()
    other = str(uuid.uuid4())
    for sel, detail in (({"rootId": ROOT, "nodeId": other, "claimKey": "ck-1"}, ["nodeId"]),
                        ({"rootId": other, "nodeId": NODE, "claimKey": "ck-1"}, ["rootId"]),
                        ({"rootId": ROOT, "nodeId": NODE, "claimKey": "ck-x"}, ["claimKey"])):
        with pytest.raises(Refused) as e:
            A.read_cb(m.view(), {"executionKey": ekey()}, fa(), now=0.0, selector=sel)
        assert (e.value.code, e.value.detail) == ("selector_mismatch", detail)
    with pytest.raises(Refused) as e:
        A.read_cb(m.view(), {"executionKey": ekey()}, fa(node(node_id=other)), now=0.0)
    assert e.value.detail == ["facts.nodeId"]


def test_window_bounds():
    assert A.check_windows((900, 1800, 1800))
    for bad in ((59, 1800, 1800), (900, 86_401, 1800), (900.0, 1800, 1800)):
        with pytest.raises(Malformed):
            A.check_windows(bad)
