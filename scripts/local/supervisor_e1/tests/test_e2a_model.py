"""E2a offline: the pure evidence model (plan 026 rev 5). No I/O, no durability claim."""

import json

import pytest

from e1.evidence import (AT_REPR_MAX, MAX_AT, MAX_SEQ, RECORD_MAX_BYTES, Bounds, Classification, Facts, JournalRefused, ModelJournal,
                         ReviewWorkflow, classify, derive_review, evidence_digest, worst_reserved_record_size)

ROOT, KEY = "r-1", "k-1"
W = "writer-A"
FULL = ["claim_intent", "claimed", "launch_intent", "launched", "exited", "result_captured", "finish_intent", "finish_outcome"]


def j(**bounds) -> ModelJournal:
    return ModelJournal(W, Bounds(**bounds) if bounds else None)


def payload(kind):
    return {"outcome": "applied"} if kind == "finish_outcome" else {"k": kind}


def run_to(mj: ModelJournal, kinds, root=ROOT, key=KEY):
    for k in kinds:
        mj.append(root, key, k, payload(k))


def facts(**kw):
    return Facts(reader=W, **kw)


# --------------------------------------------------------------------------- classification

@pytest.mark.parametrize("n,f,label", [
    (0, facts(), "C0"),
    (1, facts(), "C1:not_observed"),
    (1, facts(claim_found=False), "C1:not_observed"),                       # a 404 is "not observed" only
    (1, facts(claim_found=False, server_completion_confirmed=True), "C1:confirmed_not_committed"),
    (1, facts(claim_found=True, claim_receipt_matches=True), "C1:committed_replay"),
    (1, facts(claim_found=True, claim_receipt_matches=False), "C1:foreign_receipt"),
    (2, facts(), "C2"),
    (3, facts(), "C3"),
    (4, facts(), "C4"),
    (5, facts(), "C5"),
    (6, facts(), "C6"),
    (7, facts(finish_proof="proved", node_done_for_attempt=True), "C7:proved"),
    (7, facts(finish_proof="proved", node_done_for_attempt=False), "C7:proof_missing"),
    (7, facts(finish_proof="superseded"), "C7:superseded"),
    (7, facts(finish_proof="unconfirmed"), "C7:unconfirmed"),
    (7, facts(finish_proof="intervening"), "C7:intervening"),
    (7, facts(finish_proof=None), "C7:proof_missing"),
    (8, facts(node_done_for_attempt=True, review_class="candidate"), "C8"),
    (8, facts(node_done_for_attempt=True, review_class="terminal_rejected"), "C8:terminal_rejected"),
    (8, facts(node_done_for_attempt=True, review_class="operator_classification"), "C8:operator_classification"),
    (8, facts(node_done_for_attempt=False, review_class="candidate"), "C8:not_confirmed_done"),   # global candidate, not ours
])
def test_every_crash_prefix_classifies_to_operator_only_resolutions(n, f, label):
    mj = j()
    run_to(mj, FULL)
    c = classify(mj.prefix(ROOT, KEY, n), f)
    assert c.label == label
    assert c.automatic == ()
    for action in c.allowed:
        assert not action.startswith(("auto", "retry", "relaunch", "reassign")), action


def test_404_never_recommends_a_fresh_key():
    mj = j()
    run_to(mj, ["claim_intent"])
    c = classify(mj.prefix(ROOT, KEY, 1), facts(claim_found=False))
    assert "operator_permit_new_claim" not in c.allowed and "claim_committed" in c.unknowns


def test_unconfirmed_finish_is_not_proof_of_absence():
    mj = j()
    run_to(mj, FULL[:7])
    c = classify(mj.prefix(ROOT, KEY, 7), facts(finish_proof="unconfirmed"))
    assert "finish_committed" in c.unknowns and "await_confirmed_server_completion" in c.allowed


@pytest.mark.parametrize("outcome", [{"outcome": "proved"}, {"outcome": "applied", "invalidPayload": True},
                                     {"outcome": "rejected:stale_revision"}, {"blob": "x" * 5000}])
def test_only_a_valid_applied_or_unchanged_outcome_confirms_done(outcome):
    mj = j()
    run_to(mj, FULL[:7])
    mj.append(ROOT, KEY, "finish_outcome", outcome)
    c = classify(mj.prefix(ROOT, KEY, 8), facts(node_done_for_attempt=True, review_class="candidate"))
    assert c.label == "C8:not_confirmed_done" and "request_review" not in c.allowed


def test_global_planstore_review_fact_is_reported_separately_from_this_streams_handoff():
    mj = j()
    run_to(mj, FULL)
    c = classify(mj.prefix(ROOT, KEY, 8), facts(node_done_for_attempt=False, review_class="candidate"))
    assert c.label == "C8:not_confirmed_done" and c.planstore_review == "candidate" and "request_review" not in c.allowed


def test_trailing_fault_classifies_by_the_interrupted_step():
    mj = j()
    run_to(mj, ["claim_intent", "claimed", "launch_intent"])
    mj.append(ROOT, KEY, "fault", {"phase": "worker_raised"})
    assert classify(mj.prefix(ROOT, KEY, 4), facts()).label == "C3"


def test_foreign_writer_prefix_is_refused_and_cannot_be_appended_to():
    mj = j()
    run_to(mj, ["claim_intent", "claimed"])
    c = classify(mj.prefix(ROOT, KEY, 2), Facts(reader="writer-B"))
    assert c.label == "foreign" and c.allowed == ("operator_resolve_foreign_stream",)
    with pytest.raises(JournalRefused) as e:
        mj.append(ROOT, KEY, "launch_intent", {}, writer="writer-B")
    assert e.value.code == "foreign_writer"


def test_restart_replay_rebuilds_the_same_classification_from_the_prefix_alone():
    mj = j()
    run_to(mj, FULL[:7])
    before = classify(mj.prefix(ROOT, KEY, 7), facts(finish_proof="unconfirmed"))
    survived = list(mj.prefix(ROOT, KEY, 7))   # "crash": only the prefix is kept (conceptual, no durability)
    del mj
    assert classify(survived, facts(finish_proof="unconfirmed")) == before


def review_stream(mj, *extra):
    run_to(mj, FULL)
    mj.append(ROOT, KEY, "review_requested", {"review": list(REVIEW), "lead": "lead-1"})
    for k in extra:
        mj.append(ROOT, KEY, k, {"review": list(REVIEW)})
    return mj.prefix(ROOT, KEY, len(mj.streams[(ROOT, KEY)].records))


@pytest.mark.parametrize("f,label", [
    (facts(node_done_for_attempt=True, review_class="candidate"), "C10"),
    (facts(node_done_for_attempt=False, review_class="candidate"), "review:moot"),          # reopened / other attempt
    (facts(node_done_for_attempt=True, review_class="accepted"), "review:moot"),            # decided
    (facts(node_done_for_attempt=True, review_class="terminal_rejected"), "review:moot"),
    (facts(node_done_for_attempt=True, review_class="operator_classification"), "review:operator_classification"),
])
def test_review_states_use_current_planstore_facts(f, label):
    mj = j()
    assert classify(review_stream(mj), f).label == label


def test_every_notify_intent_counts_against_k_even_with_unknown_outcome():
    mj = j(notify_max=3)
    prefix = review_stream(mj, "notify_intent", "notify_intent", "notify_intent")   # no outcomes recorded
    c = classify(prefix, facts(node_done_for_attempt=True, review_class="candidate"), Bounds(notify_max=3))
    assert c.label == "C9:bound_reached" and c.allowed == ("operator_queue",)


# --------------------------------------------------------------------------- bounds

def test_intent_reserves_terminal_and_fault_slots_so_outcomes_always_fit():
    mj = j(per_stream=8)
    mj.append(ROOT, KEY, "claim_intent", {})          # 1 + 1 terminal + 2 fault = 4 slots
    assert mj.streams[(ROOT, KEY)].count() == 4
    mj.append(ROOT, KEY, "claimed", {})               # consumes a reserved slot: still 4
    mj.append(ROOT, KEY, "review_requested", {"x": 1})   # 5
    with pytest.raises(JournalRefused) as e:          # launch needs 1 + 3 = 4 more: 9 > 8
        mj.append(ROOT, KEY, "launch_intent", {})
    assert e.value.code == "stream_cap"
    mj.append(ROOT, KEY, "fault", {"phase": "x"})     # the reserved fault/operator slots still serve
    mj.append(ROOT, KEY, "operator_resolution", {"decision": "release", "reason": "r"})


def test_started_effect_outcomes_are_never_refused_even_at_the_cap():
    mj = j(per_stream=8)
    run_to(mj, ["claim_intent", "claimed"])
    mj.append(ROOT, KEY, "launch_intent", {})
    assert mj.streams[(ROOT, KEY)].count() == 8       # full, but every outcome is pre-reserved
    for k in ("launched", "exited", "result_captured", "fault", "operator_resolution"):
        mj.append(ROOT, KEY, k, {"k": k})
    with pytest.raises(JournalRefused):
        mj.append(ROOT, KEY, "fault", {"phase": "beyond reserve"})   # reserve exhausted and stream full


def test_workflow_records_never_consume_reserved_slots():
    mj = j(per_stream=4)
    mj.append(ROOT, KEY, "claim_intent", {})          # intent + claimed + 2 fault = 4 (full)
    with pytest.raises(JournalRefused) as e:
        mj.append(ROOT, KEY, "review_progress", {"note": "x"})
    assert e.value.code == "stream_cap"
    mj.append(ROOT, KEY, "claimed", {})               # the reservation still serves its outcome


def test_terminal_without_an_intent_is_refused():
    mj = j()
    mj.append(ROOT, KEY, "claim_intent", {})
    with pytest.raises(JournalRefused) as e:
        mj.append(ROOT, KEY, "finish_outcome", {})
    assert e.value.code == "no_open_intent"


@pytest.mark.parametrize("data,code", [
    ({"artifactRef": "x" * 513}, "byte_cap"),
    ({"note": "x" * 1025}, "byte_cap"),
    ({"command": "x" * 1025}, "byte_cap"),
    ({"blob": "x" * 1025}, "byte_cap"),
    ({"list": ["x" * 1025]}, "byte_cap"),                               # lists are checked
    ({"nested": {"a": {"b": {"c": {"d": {"e": 1}}}}}}, "payload_shape"),   # depth
    ({"many": list(range(33))}, "payload_shape"),
    ({"f": 1.5}, "payload_shape"),                                       # no floats
    ({"big": 2**63}, "payload_shape"),
    ({"a": "x" * 1000, "b": "x" * 1000, "c": "x" * 1000}, "record_cap"),   # aggregate > RECORD_MAX_BYTES
])
def test_payloads_are_typed_and_bounded(data, code):
    mj = j()
    with pytest.raises(JournalRefused) as e:
        mj.append(ROOT, KEY, "claim_intent", data)
    assert e.value.code == code


def test_largest_legal_record_fits_and_size_is_exact():
    mj = j()
    data = {"a": "x" * 600, "b": "y" * 600, "artifactRef": "z" * 512}
    rec = mj.append(ROOT, KEY, "claim_intent", data)
    assert rec.size <= RECORD_MAX_BYTES
    assert rec.size == len(json.dumps({"kind": rec.kind, "writer": rec.writer, "root": rec.root, "key": rec.claim_key,
                                       "seq": rec.seq, "at": rec.at, "data": rec.data}, separators=(",", ":")).encode())


def test_an_outcome_that_cannot_fit_becomes_bounded_fault_evidence_under_its_reservation():
    mj = j()
    run_to(mj, ["claim_intent"])
    rec = mj.append(ROOT, KEY, "claimed", {"blob": "x" * 5000})        # illegal shape: NOT refused
    assert rec.kind == "claimed" and rec.payload == {"invalidPayload": True, "code": "byte_cap"}
    assert rec.degraded is True                                         # adapters surface this to the supervisor
    assert mj.streams[(ROOT, KEY)].reserved == 2                      # only the fault reserve remains


def test_global_byte_cap_holds_with_worst_case_reservations_near_full():
    b = Bounds(global_bytes=8 * RECORD_MAX_BYTES, global_records=1000)  # 16384 bytes
    mj = ModelJournal(W, b)
    mj.append(ROOT, "a", "claim_intent", {"note": "x" * 1000})        # ~1.1 KB + 3 reserved x 2048 (worst case)
    mj.append(ROOT, "b", "claim_intent", {"note": "x" * 1000})        # same again: ~14.5 KB, outcomes still reserved
    assert mj.total_bytes() <= b.global_bytes
    with pytest.raises(JournalRefused) as e:
        mj.append(ROOT, "c", "claim_intent", {"note": "x" * 1000})    # its worst-case reservation would not fit
    assert e.value.code == "global_cap"
    # The largest legal outcomes of started effects are always recordable and never exceed the cap.
    for k, field in (("claimed", "note"), ("fault", "note"), ("operator_resolution", "reason")):
        for key in ("a", "b"):
            mj.append(ROOT, key, k, {field: "x" * 1000, "artifactRef": "y" * 512})
            assert mj.total_bytes() <= b.global_bytes


def test_metadata_near_the_record_cap_refuses_the_intent_before_any_effect_and_leaves_the_model_unchanged():
    mj = j()
    root = "r" * 1950                                                  # writer/root/key nearly fill a record
    assert worst_reserved_record_size(W, root, KEY) > RECORD_MAX_BYTES
    before = model_state(mj)
    with pytest.raises(JournalRefused) as e:
        mj.append(root, KEY, "claim_intent", {})
    assert e.value.code == "metadata_headroom" and model_state(mj) == before


LONGEST_CLOCKS = [MAX_AT, 0.12345678901234568, 2.2250738585072014e-308, 1.2345678901234567e-05]


def test_the_longest_clock_serializations_fit_the_declared_width():
    assert max(len(json.dumps(v)) for v in LONGEST_CLOCKS) == AT_REPR_MAX


@pytest.mark.parametrize("now", LONGEST_CLOCKS)
def test_the_fallback_always_fits_for_any_admitted_intent_at_the_metadata_limit(now):
    # Grow the root until one more character would breach the headroom, then prove the worst fallback
    # fits with the LONGEST clock serialization (not just MAX_AT) and the largest seq.
    root = "r"
    while worst_reserved_record_size(W, root + "r", KEY) <= RECORD_MAX_BYTES:
        root += "r"
    mj = ModelJournal(W, now=now)
    mj._seq = MAX_SEQ - 4                                              # claimed + 2 fault reserves land at MAX_SEQ
    mj.append(root, KEY, "claim_intent", {})
    rec = mj.append(root, KEY, "claimed", {"blob": "x" * 5000})          # oversized: fallback, never refused
    assert rec.degraded and rec.size <= RECORD_MAX_BYTES
    for k in ("fault", "operator_resolution"):
        r = mj.append(root, KEY, k, {"blob": "x" * 5000})
        assert r.degraded and r.size <= RECORD_MAX_BYTES
    assert r.seq == MAX_SEQ


@pytest.mark.parametrize("now", [-1.0, MAX_AT * 2, float("inf"), float("nan"), True, "5"])
def test_the_model_clock_must_be_finite_and_bounded(now):
    with pytest.raises(JournalRefused) as e:
        ModelJournal(W, now=now)
    assert e.value.code == "clock"
    mj = j()
    with pytest.raises(JournalRefused):
        mj.now = now                                                   # refused on assignment, never at an outcome
    assert mj.now == 0.0


def test_intent_admission_reserves_sequence_capacity_for_its_terminal_and_fault_slots():
    mj = j()
    mj._seq = MAX_SEQ - 3                                              # claim_intent needs 1 + claimed + 2 faults = 4
    before = model_state(mj)
    with pytest.raises(JournalRefused) as e:
        mj.append(ROOT, KEY, "claim_intent", {})
    assert e.value.code == "seq_exhausted" and model_state(mj) == before and mj._seq == MAX_SEQ - 3

    mj._seq = MAX_SEQ - 4
    mj.append(ROOT, KEY, "claim_intent", {})
    with pytest.raises(JournalRefused) as e:                           # ordinary records cannot use reserved seqs
        mj.append(ROOT, "other", "claim_intent", {})
    assert e.value.code == "seq_exhausted"
    assert mj.append(ROOT, KEY, "claimed", {}).seq == MAX_SEQ - 2
    assert mj.append(ROOT, KEY, "fault", {"code": "c"}).seq == MAX_SEQ - 1
    assert mj.append(ROOT, KEY, "operator_resolution", {"decision": "abandoned_no_effects", "reason": "r",
                                                        "reconciliationRef": "recon-1"}).seq == MAX_SEQ


@pytest.mark.parametrize("bad", [dict(per_stream=0), dict(per_stream=True), dict(notify_max=-1), dict(global_bytes=10),
                                 dict(compact_after=0), dict(summary_retention=float("nan")), dict(global_records="9")])
def test_bounds_reject_bad_values(bad):
    with pytest.raises(ValueError):
        Bounds(**bad)


def test_resolve_needs_explicit_evidence_and_never_releases_outstanding_reservations_silently():
    mj = j()
    run_to(mj, ["claim_intent", "claimed", "launch_intent"])
    with pytest.raises(JournalRefused) as e:
        mj.resolve(ROOT, KEY)
    assert e.value.code == "unresolved_outstanding" and mj.streams[(ROOT, KEY)].reserved > 0
    mj.append(ROOT, KEY, "operator_resolution", {"decision": "release", "reason": "reply unknown"})
    with pytest.raises(JournalRefused):
        mj.resolve(ROOT, KEY)                                   # a release with an unknown reply is not terminal
    mj.append(ROOT, KEY, "operator_resolution", {"decision": "confirmed_released", "reason": "r", "reconciliationRef": "recon-1"})
    mj.resolve(ROOT, KEY)
    assert mj.streams[(ROOT, KEY)].reserved == 0


def test_unresolved_cap_refuses_new_claims_and_never_evicts_unresolved():
    mj = j(unresolved_streams=2)
    run_to(mj, ["claim_intent"], key="a")
    run_to(mj, ["claim_intent"], key="b")
    with pytest.raises(JournalRefused) as e:
        mj.append(ROOT, "c", "claim_intent", {})
    assert e.value.code == "unresolved_cap" and {k for _, k in mj.streams} == {"a", "b"}


def test_global_count_cap_counts_everything_and_evicts_only_resolved_data():
    mj = j(global_records=11, compact_after=10.0, summary_retention=100.0)
    run_to(mj, ["claim_intent", "claimed"], key="old")
    mj.append(ROOT, "old", "operator_resolution", {"decision": "confirmed_released", "reason": "r", "reconciliationRef": "recon-1"})
    mj.resolve(ROOT, "old")                                            # 3 records retained, no reservations
    run_to(mj, ["claim_intent", "claimed"], key="live")               # 2 records + 2 reserved = 4 -> total 7
    mj.append(ROOT, "new", "claim_intent", {})                        # needs 4 -> 11 (fits exactly)
    assert mj.total_count() == 11
    before = model_state(mj)
    with pytest.raises(JournalRefused) as e:
        mj.append(ROOT, "newer", "claim_intent", {})                  # compacting "old" would free 2: not enough
    assert e.value.code == "global_cap"
    assert model_state(mj) == before                                  # refused: NOTHING evicted or compacted


def test_compaction_and_summary_retention_touch_only_resolved_streams():
    mj = j(compact_after=10.0, summary_retention=50.0)
    run_to(mj, ["claim_intent", "claimed"], key="done")
    mj.append(ROOT, "done", "operator_resolution", {"decision": "confirmed_released", "reason": "r", "reconciliationRef": "recon-1"})
    mj.resolve(ROOT, "done")
    run_to(mj, ["claim_intent"], key="open")
    mj.now = 15.0
    mj.compact()
    assert mj.streams[(ROOT, "done")].summary is not None and mj.streams[(ROOT, "open")].summary is None
    mj.now = 60.0
    mj.compact()
    assert (ROOT, "done") not in mj.streams and (ROOT, "open") in mj.streams


def model_state(mj):
    return sorted((k, s.summary, tuple(r.seq for r in s.records), s.reserved, s.resolved_at) for k, s in mj.streams.items())


def test_a_refusal_after_eviction_planning_leaves_the_model_unchanged():
    mj = j(global_records=8, compact_after=10.0, summary_retention=100.0)
    run_to(mj, ["claim_intent", "claimed"], key="old")
    mj.append(ROOT, "old", "operator_resolution", {"decision": "confirmed_released", "reason": "r", "reconciliationRef": "recon-1"})
    mj.resolve(ROOT, "old")
    run_to(mj, ["claim_intent", "claimed"], key="live")               # 3 + 4 = 7
    before = model_state(mj)
    with pytest.raises(JournalRefused):
        mj.append(ROOT, "x", "claim_intent", {})                    # needs 4: even compacting "old" leaves 5 + 4 = 9 > 8
    assert model_state(mj) == before                                  # the plan was never applied


@pytest.mark.parametrize("finish,decided,ok_", [
    ({"outcome": "applied"}, True, True),
    ({"outcome": "unchanged"}, True, True),
    ({"outcome": "applied"}, False, False),                            # no authoritative decision yet
    ({"outcome": "rejected:stale_revision"}, True, False),
    ({"outcome": "unknown"}, True, False),
    ({"blob": "x" * 5000}, True, False),                               # invalidPayload fallback
])
def test_resolve_by_finish_needs_a_positive_valid_outcome_and_a_decision(finish, decided, ok_):
    mj = j()
    run_to(mj, ["claim_intent", "claimed", "launch_intent", "launched", "exited", "result_captured", "finish_intent"])
    mj.append(ROOT, KEY, "finish_outcome", finish)
    if ok_:
        mj.resolve(ROOT, KEY, planstore_decided=decided)
        assert mj.streams[(ROOT, KEY)].resolved_at is not None
    else:
        with pytest.raises(JournalRefused):
            mj.resolve(ROOT, KEY, planstore_decided=decided)


@pytest.mark.parametrize("resolution,ok_", [
    ({"decision": "confirmed_released", "reconciliationRef": "recon"}, True),
    ({"decision": "abandoned_no_effects", "reconciliationRef": "recon"}, True),
    ({"decision": "confirmed_finished", "reconciliationRef": "recon"}, True),
    ({"decision": "confirmed_released"}, False),                       # no reconciliation reference
    ({"decision": "release"}, False),                                  # reply unknown
    ({"decision": "retry_permitted_once", "reconciliationRef": "recon"}, False),
    ({"decision": "new_claim_permitted", "reconciliationRef": "recon"}, False),
])
def test_only_terminal_operator_resolutions_resolve(resolution, ok_):
    mj = j()
    run_to(mj, ["claim_intent", "claimed"])
    mj.append(ROOT, KEY, "operator_resolution", resolution)
    if ok_:
        mj.resolve(ROOT, KEY)
    else:
        with pytest.raises(JournalRefused):
            mj.resolve(ROOT, KEY)
    c = classify(mj.prefix(ROOT, KEY, 3), facts())
    assert (c.label == "resolved") == ok_
    if not ok_:
        assert c.label.startswith("C2+permission:")                    # permission, not resolution


# --------------------------------------------------------------------------- review workflow

REVIEW = ("r-1", "n-1", "att", 1, "sha-1")


def wf():
    return ReviewWorkflow(ack_window=10.0, progress_window=20.0, notify_max=2)


def live(_k):
    return "candidate"


@pytest.mark.parametrize("current", ["accepted", "terminal_rejected", "not_done", "operator_classification"])
def test_an_already_queued_review_leaves_the_queue_when_it_stops(current):
    w = wf()
    w.request(REVIEW, "lead-1", 0.0)
    for t in (10.0, 20.0, 30.0):
        w.wake(t, live)
    assert REVIEW in w.queue                                           # escalated to the operator queue
    w.wake(40.0, lambda k: current)
    assert REVIEW not in w.queue and REVIEW not in w.reviews


def test_a_queued_old_epoch_leaves_the_queue_when_a_new_epoch_replaces_it():
    w = wf()
    w.request(REVIEW, "lead-1", 0.0)
    for t in (10.0, 20.0, 30.0):
        w.wake(t, live)
    reopened = ("r-1", "n-1", "att", 2, "sha-2")
    w.request(reopened, "lead-1", 31.0)
    w.wake(32.0, lambda k: "candidate" if k == reopened else "not_done")
    assert w.queue == [] and set(w.reviews) == {reopened}


def test_escalation_goes_to_the_same_lead_then_the_operator_queue_never_reassigned():
    w = wf()
    w.request(REVIEW, "lead-1", 0.0)
    assert w.wake(5.0, live) == []
    assert w.wake(10.0, live) == [("notify_same_lead", REVIEW, "lead-1")]
    assert w.wake(20.0, live) == [("notify_same_lead", REVIEW, "lead-1")]
    assert w.wake(30.0, live) == [("operator_queue", REVIEW, "lead-1")]
    assert w.wake(99.0, live) == [] and w.reviews[REVIEW].lead == "lead-1"


@pytest.mark.parametrize("current,kind", [("accepted", "moot"), ("terminal_rejected", "moot"), ("not_done", "moot"),
                                          ("operator_classification", "operator_classification")])
def test_wake_stops_reviews_that_planstore_no_longer_shows_as_candidates(current, kind):
    w = wf()
    w.request(REVIEW, "lead-1", 0.0)
    out = w.wake(50.0, lambda k: current)
    assert out == [(kind, REVIEW, "")] and REVIEW not in w.reviews


def test_a_new_epoch_or_artifact_is_a_different_review_and_the_old_one_goes_moot():
    w = wf()
    w.request(REVIEW, "lead-1", 0.0)
    reopened = ("r-1", "n-1", "att", 2, "sha-2")                       # same attemptId reused, new epoch/artifact
    w.request(reopened, "lead-1", 5.0)
    assert len(w.reviews) == 2 and w.reviews[reopened].deadline == 15.0
    out = w.wake(16.0, lambda k: "candidate" if k == reopened else "not_done")
    assert ("moot", REVIEW, "") in out and REVIEW not in w.reviews
    assert ("notify_same_lead", reopened, "lead-1") in out


def test_acknowledgment_is_lead_only_and_idempotent():
    w = wf()
    w.request(REVIEW, "lead-1", 0.0)
    with pytest.raises(ValueError):
        w.acknowledge(REVIEW, "lead-2", 1.0)
    w.acknowledge(REVIEW, "lead-1", 1.0)                               # deadline 21
    w.acknowledge(REVIEW, "lead-1", 15.0)                              # repeated: no new deadline
    assert w.reviews[REVIEW].deadline == 21.0


def test_progress_requires_the_acknowledged_lead_and_new_evidence_content():
    w = wf()
    w.request(REVIEW, "lead-1", 0.0)
    d1 = evidence_digest(b"finding: test X fails on input Y")
    with pytest.raises(ValueError):
        w.progress(REVIEW, "lead-1", 1, d1, 2.0)                       # not acknowledged yet
    w.acknowledge(REVIEW, "lead-1", 1.0)                               # deadline 21
    with pytest.raises(ValueError):
        w.progress(REVIEW, "lead-2", 1, d1, 2.0)                       # wrong lead
    assert w.progress(REVIEW, "lead-1", 1, d1, 5.0) is True            # deadline 25
    assert w.progress(REVIEW, "lead-1", 1, evidence_digest(b"other"), 6.0) is False   # same checkpoint id
    assert w.progress(REVIEW, "lead-1", 2, d1, 7.0) is False           # new id, same evidence content
    assert w.progress(REVIEW, "lead-1", 3, evidence_digest(b"finding: test X fails on input Y"), 8.0) is False  # new ref, same content
    assert w.reviews[REVIEW].deadline == 25.0
    assert w.progress(REVIEW, "lead-1", 4, evidence_digest(b"fix proposed in commit abc"), 9.0) is True
    assert w.reviews[REVIEW].deadline == 29.0


def test_rehydration_uses_the_original_deadline_anchors():
    mj = j()
    run_to(mj, FULL)
    mj.now = 100.0
    mj.append(ROOT, KEY, "review_requested", {"review": list(REVIEW), "lead": "lead-1"})
    mj.now = 105.0
    mj.append(ROOT, KEY, "notify_intent", {"review": list(REVIEW), "dedupKey": "d1"})
    rehydrated = ReviewWorkflow.rehydrate(mj.streams[(ROOT, KEY)].records, ack_window=10.0, progress_window=20.0, notify_max=2)
    r = rehydrated.reviews[REVIEW]
    assert (r.requested_at, r.sends, r.deadline) == (100.0, 1, 115.0)   # anchored to the records, not "now"


# --------------------------------------------------------------------------- review derivation

def node(**kw):
    base = {"work": "done", "effectiveAcceptance": "none", "contentRevision": 1, "attemptContentRevision": 1,
            "attemptPrereqDigest": "a" * 64, "artifactRef": "sha"}
    base.update(kw)
    return base


@pytest.mark.parametrize("n,leaf,cls,elig", [
    (node(), {"gatesHold": True}, "candidate", "unverified:prerequisites"),
    (node(), {"gatesHold": False}, "candidate", "ineligible:gates"),
    (node(artifactRef=None), {"gatesHold": True}, "candidate", "ineligible:no_artifact"),
    (node(contentRevision=2), {"gatesHold": True}, "candidate", "ineligible:content_drift"),
    (node(attemptPrereqDigest=None, attemptContentRevision=None), {"gatesHold": True}, "candidate", "ineligible:unpinned_attempt"),
    (node(effectiveAcceptance="rejected"), {"gatesHold": True}, "terminal_rejected", "n/a"),
    (node(effectiveAcceptance="stale"), {"gatesHold": True}, "operator_classification", "n/a"),
    (node(effectiveAcceptance="accepted"), {"gatesHold": True}, "accepted", "n/a"),
    (node(work="in_progress"), {"gatesHold": True}, "not_done", "n/a"),
])
def test_candidates_and_accept_eligibility_are_derived_separately(n, leaf, cls, elig):
    d = derive_review(n, leaf)
    assert (d.review_class, d.accept_eligible) == (cls, elig)


def test_classification_has_no_automatic_actions():
    assert Classification("x", (), ()).automatic == ()
