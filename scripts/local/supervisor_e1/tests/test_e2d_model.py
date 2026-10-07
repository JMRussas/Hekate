"""E2d model (plan 032 rev 6 §6 cases 1, 3 and the pure parts of 2 and 4; TEST-ONLY, offline). The C-B
reads come from the E2c ActsModel; the task part is H1-SHAPED test data, not a ChatAgent rendering."""

import copy
import uuid

import pytest

from e1 import acts as A
from e1 import handoff as H
from e1.acts import canonical
from test_e2c_model import (DECIDED, LS1, LS2, ROOT, act, done, ev, fa, package, requested_review, rkey, rread)

TASK = {"text": "Implement the target task.", "instructions": {"system": "sys", "fast": "fast", "deep": "deep"}, "packageRef": package()}
SRC_CONV, DEST_CONV = str(uuid.uuid4()), str(uuid.uuid4())


def setup(session=LS1, now=100.0):
    m, rid, root = requested_review(now=now, session=session)
    return m, rid, root


PREP = str(uuid.uuid4())
PINS = {"contentRevision": 1, "attemptContentRevision": 1, "attemptPrereqDigest": "e" * 64, "artifactRef": "sha-art", "dependencies": "d" * 64}


def test_stale_task_pins_refuse_at_prepare():
    m, rid, _ = setup()
    cb = rread(m)
    for pins, why in ((dict(PINS, contentRevision=2), "content_revised"), (dict(PINS, attemptContentRevision=None), "unpinned_attempt"),
                      (dict(PINS, attemptPrereqDigest="f" * 64), "package_pins")):
        with pytest.raises(H.Refused) as e:
            H.build(role="lead", cb=cb, planstore_class="candidate", pins=pins, basis_check=[cb["basis"]], pending=pending_of(m),
                    evidence=evidence_of(m, rid), task=TASK, transition=transition(m, rid))
        assert (e.value.code, e.value.detail) == ("stale_content", why)


def transition(m, rid, target=LS2, frm=LS1, gate="operator", gate_ref="recon", prepare_id=PREP):
    pred = m.view().chain(rid).current
    return dict(H.handoff_ids(ROOT, "ck-1", rid, pred.link_id, target, prepare_id), prepareId=prepare_id, root=ROOT, claimKey="ck-1", option="review_rebind",
                gate=gate, gateRef=gate_ref, predecessorBindingId=pred.link_id, fromSession=frm, targetSession=target,
                conversationRef="conv-new")


def pending_of(m, unconfirmed=()):
    st = m.mj.streams[(ROOT, "ck-1")]
    return H.pending_effects(m.records(), m.corrupt, st.outstanding, list(unconfirmed))


def evidence_of(m, rid):
    from e1.handoff_durable import _evidence
    return _evidence(m.view(), rid)


def cls_of(facts=None):
    return A.review_state((facts or fa(done())).node, rkey(LS1))


def make(m, rid, *, cb=None, note=None, imports=(), task=TASK, t=None, pending=None, evidence=None, cls=None):
    cb = cb or rread(m)
    return H.build(role="lead", cb=cb, planstore_class=cls or cls_of(), pins=PINS, basis_check=[cb["basis"]], pending=pending if pending is not None else pending_of(m),
                   evidence=evidence if evidence is not None else evidence_of(m, rid), task=task, transition=t or transition(m, rid),
                   note=note, imports=list(imports))


def source_store(texts):
    return {(SRC_CONV, f"e{i}"): {"messageId": f"m{i}", "contentHash": H.sha(t), "text": t,
                                  "provenance": "user-stated" if i % 2 == 0 else "assistant-claimed"} for i, t in enumerate(texts)}


def imp(i, text, conv=SRC_CONV, h=None):
    return {"ref": {"conversationId": conv, "eventId": f"e{i}", "messageId": f"m{i}", "contentHash": h or H.sha(text)}, "text": text}


def allow(principal, kind, target):
    return f"tok:{kind}:{principal}"


# ======================================================================== manifest binds every delivered byte

def test_identical_inputs_give_the_same_digest_and_content_changes_change_it():
    m, rid, _ = setup()
    m.now = 200.0
    m.intake(act("review_acknowledged", rkey(LS1), 1), fa(done()))
    m.intake(act("review_progress", rkey(LS1), 2, 1, ev(1)), fa(done()))
    base = make(m, rid)
    assert make(m, rid).candidate_digest == base.candidate_digest
    cb = rread(m)
    changed_state = copy.deepcopy(cb)
    changed_state["counters"] = {"duplicate": 1}                      # same basis, different delivered value
    assert changed_state["basis"] == cb["basis"]
    variants = [make(m, rid, cb=changed_state),
                make(m, rid, note={"text": "next: rerun tests", "author": LS1}),
                make(m, rid, evidence=evidence_of(m, rid)[:-1]),
                make(m, rid, task={**TASK, "text": TASK["text"] + "!"}),
                make(m, rid, task={**TASK, "instructions": {**TASK["instructions"], "deep": "deep2"}}),
                make(m, rid, pending=pending_of(m, [{"recordId": "r-1", "kind": "worker_ack", "status": "not_observed"}])),
                make(m, rid, t={**transition(m, rid), "conversationRef": "conv-other"})]
    assert len({v.candidate_digest for v in variants} | {base.candidate_digest}) == len(variants) + 1


def test_import_text_or_provenance_change_changes_the_digest():
    m, rid, _ = setup()
    store = source_store(["alpha", "beta"])
    a = H.verify_imports([imp(0, "alpha")], store, "lead-1", allow, DEST_CONV)
    b = H.verify_imports([imp(1, "beta")], store, "lead-1", allow, DEST_CONV)
    pa, pb = make(m, rid, imports=a), make(m, rid, imports=b)
    assert pa.candidate_digest != pb.candidate_digest
    flipped = [dict(a[0], provenance="assistant-claimed")]
    assert make(m, rid, imports=flipped).candidate_digest != pa.candidate_digest
    rec = pa.manifest["optional"]["imports"][0]
    assert rec["ref"]["conversationId"] == SRC_CONV and rec["destination"]["conversationId"] == DEST_CONV
    assert rec["auth"] == {"sourceRead": "tok:source_read:lead-1", "destinationUse": "tok:destination_use:lead-1"}


def test_manifest_holds_a_candidate_transition_and_no_commit_status():
    m, rid, _ = setup()
    p = make(m, rid)
    assert p.manifest["transition"]["status"] == "candidate"
    assert "receipt" not in canonical(p.manifest) and "recordHash" not in canonical(p.manifest)
    assert p.envelope["version"] == H.ENVELOPE and p.manifest["policy"]["version"] == H.POLICY


# ======================================================================== role proof policy

def test_unproved_mandatory_items_refuse():
    m, rid, _ = setup()
    cb = rread(m)
    for bad_cb, cls, why in ((dict(cb, current="unknown"), "candidate", "no current"),
                             (rread(m, facts=fa(done(**DECIDED))), cls_of(fa(done(**DECIDED))), "decided review"),
                             (rread(m, facts=fa(done(attempt_id="att-9"))), cls_of(fa(done(attempt_id="att-9"))), "moot review")):
        with pytest.raises(H.Refused) as e:
            make(m, rid, cb=bad_cb, cls=cls)
        assert e.value.code == "mandatory_unproved", why
    with pytest.raises(H.Refused) as e:
        make(m, rid, t=transition(m, rid, frm=LS2))                    # predecessor is not the bound session
    assert e.value.code == "mandatory_unproved"
    with pytest.raises(H.Refused):
        make(m, rid, task={**TASK, "instructions": {"system": "s"}})


def test_unknown_diagnostics_are_carried_not_refused():
    m, rid, _ = setup()
    sk = rid
    cb = m.read({"attemptKey": {k: rkey(LS1)[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art"}, fa(done()),
                pending={f"{sk}:ack": "not_observed"})
    p = make(m, rid, cb=cb)
    diag = p.manifest["optional"]["diagnostics"]
    assert diag["ack"]["value"] == "unknown" and diag["asOf"] == "prepare"


# ======================================================================== mandatory visibility of pending uncertain effects

def test_open_intents_and_unconfirmed_appends_are_always_listed():
    m, rid, _ = setup()
    m.mj.append(ROOT, "ck-1", "notify_intent", {"purpose": "remind"})   # an outcome-less intent
    items = pending_of(m, [{"recordId": "r-9", "kind": "review_acknowledged", "status": "not_observed"}])
    assert {(i["kind"], i["status"]) for i in items} == {("open_intent", "unknown"), ("commit_unknown", "not_observed")}
    p = make(m, rid, pending=items)
    assert p.manifest["authority"]["pending"] == items


def test_unlistable_pending_effects_refuse():
    m, rid, _ = setup()
    with pytest.raises(H.Refused) as e:
        H.pending_effects(m.records(), "chain:5", [], [])
    assert e.value.code == "pending_unlistable"
    with pytest.raises(H.Refused):
        H.pending_effects(m.records(), None, [["notify_outcome"]], [])  # stream says outstanding, records disagree
    with pytest.raises(H.Refused):
        make(m, rid, pending=[{"kind": "open_intent"}])


# ======================================================================== accounting and deterministic overflow

def test_required_overflow_is_refused_never_shortened():
    m, rid, _ = setup()
    with pytest.raises(H.Refused) as e:
        make(m, rid, task={**TASK, "text": "x" * (H.REQUIRED_MAX + 1)})
    assert e.value.code == "handoff_overflow"
    with pytest.raises(H.Refused):                                      # instruction texts count too
        make(m, rid, task={**TASK, "instructions": {"system": "s" * 40_000, "fast": "f" * 30_000, "deep": "d"}})
    many = [{"kind": "open_intent", "id": f"i{i}", "status": "unknown"} for i in range(H.REQUIRED_REFS + 1)]
    with pytest.raises(H.Refused):
        make(m, rid, pending=many)


def test_optional_items_are_omitted_by_a_deterministic_prefix_rule():
    m, rid, _ = setup()
    texts = ["y" * 9000 + str(i) for i in range(6)]
    store = source_store(texts)
    verified = H.verify_imports([imp(i, t) for i, t in enumerate(texts)], store, "lead-1", allow, DEST_CONV)
    p1, p2 = make(m, rid, imports=verified), make(m, rid, imports=verified)
    sel = p1.manifest["selection"]
    assert sel["omitted"] > 0 and sel["cursor"] == sel["included"] and p1.candidate_digest == p2.candidate_digest
    assert p1.bytes_total - p1.bytes_required <= H.OPTIONAL_MAX and p1.bytes_total <= H.TOTAL_MAX
    assert len(p1.manifest["optional"]["imports"]) == sel["included"] - 1      # diagnostics come first


def test_note_size_is_capped():
    m, rid, _ = setup()
    with pytest.raises(H.Refused) as e:
        make(m, rid, note={"text": "n" * (H.NOTE_MAX + 1), "author": LS1})
    assert e.value.code == "note_too_large"


# ======================================================================== snapshot discipline, imports

def test_a_package_mixing_reads_is_refused():
    m, rid, _ = setup()
    cb = rread(m)
    with pytest.raises(H.Refused) as e:
        H.build(role="lead", cb=cb, planstore_class="candidate", pins=PINS, basis_check=[cb["basis"], dict(cb["basis"], tx="another-read")], pending=pending_of(m),
                evidence=evidence_of(m, rid), task=TASK, transition=transition(m, rid))
    assert e.value.code == "mixed_snapshot"


@pytest.mark.parametrize("imports,code", [
    ([imp(0, "alpha", h=H.sha("other"))], "import_rewritten"),                  # ref no longer matches the source record
    ([imp(0, "ALPHA", h=H.sha("alpha"))], "import_hash_mismatch"),
    ([imp(0, "alpha", conv=DEST_CONV)], "import_rewritten"),                   # repointed to the destination
    ([imp(0, "alpha"), imp(0, "alpha")], "import_ambiguous"),
])
def test_bad_imports_are_rejected(imports, code):
    with pytest.raises(H.Refused) as e:
        H.verify_imports(imports, source_store(["alpha"]), "lead-1", allow, DEST_CONV)
    assert e.value.code == code


@pytest.mark.parametrize("deny", ["source_read", "destination_use"])
def test_imports_need_both_sides_authorized(deny):
    def stub(principal, kind, target):
        return None if kind == deny else "tok"
    with pytest.raises(H.Refused) as e:
        H.verify_imports([imp(0, "alpha")], source_store(["alpha"]), "lead-1", stub, DEST_CONV)
    assert e.value.code == "import_unauthorized"


def test_imports_and_notes_are_not_acts():
    m, rid, _ = setup()
    m.intake(act("review_acknowledged", rkey(LS1), 1), fa(done()))
    before, n = rread(m)["current"], len(m.records())
    verified = H.verify_imports([imp(0, "I finished checkpoint 9")], source_store(["I finished checkpoint 9"]), "lead-1", allow, DEST_CONV)
    make(m, rid, imports=verified, note={"text": "progress: done with everything", "author": LS1})
    assert rread(m)["current"] == before and len(m.records()) == n


# ======================================================================== freshness and the commit decision (pure)

def test_freshness_compares_semantics_not_bases():
    m, rid, _ = setup()
    pred = m.view().chain(rid).current.link_id
    cb = rread(m)
    pr = TASK["packageRef"]
    s1 = H.semantic_set("lead", cb, pending_of(m), pr, pred, "candidate", PINS)
    other_basis = dict(cb, basis=dict(cb["basis"], tx="t2"), counters={"duplicate": 7})
    assert H.semantic_set("lead", other_basis, pending_of(m), pr, pred, "candidate", PINS) == s1    # basis and diagnostics ignored
    assert H.semantic_set("lead", dict(cb, queue=["conflict"]), pending_of(m), pr, pred, "candidate", PINS) != s1
    assert H.semantic_set("lead", cb, pending_of(m), pr, pred, "decided", PINS) != s1
    assert H.semantic_set("lead", cb, pending_of(m), pr, pred, "candidate", dict(PINS, contentRevision=2)) != s1   # content pin change
    assert H.semantic_set("lead", cb, pending_of(m), pr, pred, "candidate", dict(PINS, dependencies="0" * 64)) != s1
    more = pending_of(m, [{"recordId": "r", "kind": "worker_ack", "status": "unknown"}])
    assert H.semantic_set("lead", cb, more, pr, pred, "candidate", PINS) != s1


def test_decide_commit_order_retry_first_then_freshness_then_cas():
    m, rid, _ = setup()
    t = transition(m, rid)
    pkg = make(m, rid)
    fresh = H.semantic_set("lead", rread(m), pending_of(m), TASK["packageRef"], t["predecessorBindingId"], "candidate", PINS)
    d = H.decide_commit(m.view(), rid, t, pkg.candidate_digest, None, fresh, fresh)
    assert d.outcome == "accepted" and d.append[1]["linkId"] == t["linkId"] and d.append[1]["candidateDigest"] == pkg.candidate_digest
    row = {"root": ROOT, "claim_key": "ck-1", "kind": "review_rebind", "data": canonical(d.append[1])}
    stale = dict(fresh, queue=["x"])
    assert H.decide_commit(m.view(), rid, t, pkg.candidate_digest, row, stale, fresh).outcome == "committed"   # retry before freshness
    assert H.decide_commit(m.view(), rid, t, "0" * 64, row, fresh, fresh).outcome == "conflict"
    assert H.decide_commit(m.view(), rid, t, pkg.candidate_digest, dict(row, claim_key="other"), fresh, fresh).outcome == "conflict"
    assert H.decide_commit(m.view(), rid, t, pkg.candidate_digest, None, stale, fresh).outcome == "stale_candidate"
    assert H.decide_commit(m.view(), rid, dict(t, predecessorBindingId="x"), pkg.candidate_digest, None, fresh, fresh).outcome == "stale_binding"
    assert H.decide_commit(m.view(), rid, dict(t, gate="release", gateRef=LS2), pkg.candidate_digest, None, fresh, fresh).outcome == "refused"


def test_handoff_ids_depend_on_who_and_the_prepare_operation_not_on_content():
    a = H.handoff_ids(ROOT, "ck-1", "r" * 64, "pred", LS2, PREP)
    assert a == H.handoff_ids(ROOT, "ck-1", "r" * 64, "pred", LS2, PREP)                       # exact retry
    assert a != H.handoff_ids(ROOT, "ck-1", "r" * 64, "pred", LS1, PREP)
    assert a != H.handoff_ids(ROOT, "ck-1", "r" * 64, "pred", LS2, str(uuid.uuid4()))          # a genuinely new prepare
    assert len({a["handoffId"], a["recordId"], a["linkId"]}) == 3
    with pytest.raises(H.Refused):
        H.handoff_ids(ROOT, "ck-1", "r" * 64, "pred", LS2, "not-a-uuid")


def test_unconfirmed_ack_does_not_block_a_proven_candidate():
    """msg 1256: an unknown ACK proof makes the presentation status unknown, but the PlanStore class
    and the binding are proven, so the package is issued with the status as a diagnostic."""
    m, rid, _ = setup()
    cb = m.read({"attemptKey": {k: rkey(LS1)[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art"}, fa(done()),
                pending={f"{rid}:ack": "not_observed"})
    assert cb["current"]["review"]["value"] == "unknown"
    p = make(m, rid, cb=cb)
    assert p.manifest["state"]["mandatory"]["planstoreClass"] == "candidate"
    assert p.manifest["optional"]["diagnostics"]["review"]["value"] == "unknown"


@pytest.mark.parametrize("size", [9000, 10500, 15000, 31000])
def test_final_package_respects_both_partitions_at_the_boundary(size):
    m, rid, _ = setup()
    texts = [chr(97 + i) * size for i in range(8)]
    verified = H.verify_imports([imp(i, t) for i, t in enumerate(texts)], source_store(texts), "lead-1", allow, DEST_CONV)
    p = make(m, rid, imports=verified)
    m2 = copy.deepcopy(p.manifest)
    m2["optional"] = {"diagnostics": None, "evidence": [], "imports": [], "note": None}
    req = H.nbytes(TASK["text"]) + sum(H.nbytes(v) for v in TASK["instructions"].values()) + \
        H.nbytes(canonical({"version": H.ENVELOPE, "manifest": m2, "payload": {"imports": [], "note": None}}))
    total = H.nbytes(TASK["text"]) + sum(H.nbytes(v) for v in TASK["instructions"].values()) + H.nbytes(canonical(p.envelope))
    assert total == p.bytes_total and req == p.bytes_required
    assert req <= H.REQUIRED_MAX and total - req <= H.OPTIONAL_MAX and total <= H.TOTAL_MAX


def test_semantic_set_reconstructs_from_the_stored_manifest():
    m, rid, _ = setup()
    t = transition(m, rid)
    p = make(m, rid, t=t)
    cb = rread(m)
    assert H.semantic_from_manifest(p.manifest) == H.semantic_set("lead", cb, pending_of(m), TASK["packageRef"],
                                                                   t["predecessorBindingId"], "candidate", PINS)


def _row(p, task=TASK):
    return {"candidate_digest": p.candidate_digest, "manifest": canonical(p.manifest), "envelope": canonical(p.envelope),
            "task": canonical({"text": task["text"], "instructions": task["instructions"], "packageRef": task["packageRef"]})}


def test_stored_candidate_verification_rejects_any_tampering():
    m, rid, _ = setup()
    p = make(m, rid, note={"text": "n1", "author": LS1})
    manifest, t, task = H.verify_stored(_row(p))
    assert manifest == p.manifest and t["handoffId"] == p.manifest["transition"]["handoffId"] and task["text"] == TASK["text"]
    bad_manifest = copy.deepcopy(p.manifest)
    bad_manifest["transition"]["targetSession"] = LS1
    bad_env = copy.deepcopy(p.envelope)
    bad_env["payload"]["note"] = "n2"
    unbound = copy.deepcopy(p.envelope)
    unbound["payload"]["unboundText"] = "Extra delivered context outside manifest"          # msg 1266 probe
    versioned = dict(copy.deepcopy(p.envelope), version="different-contract")
    extra_top = dict(copy.deepcopy(p.envelope), extra="x")
    for row in (dict(_row(p), manifest=canonical(bad_manifest)),
                dict(_row(p), envelope=canonical(unbound)),
                dict(_row(p), envelope=canonical(versioned)),
                dict(_row(p), envelope=canonical(extra_top)),
                dict(_row(p), envelope=canonical(p.envelope).replace(",", ", ", 1)),             # not canonical bytes
                dict(_row(p), envelope=canonical(bad_env)),
                dict(_row(p), task=canonical({**TASK, "unboundText": "Extra delivered task field"})),     # msg 1273 probe
                dict(_row(p), task=canonical({**TASK, "instructions": {**TASK["instructions"], "extra": "x"}})),
                dict(_row(p), task=canonical(TASK).replace(",", ", ", 1)),                                 # non-canonical bytes
                _row(p, task={**TASK, "text": "other"}),
                _row(p, task={**TASK, "instructions": {**TASK["instructions"], "deep": "x"}})):
        with pytest.raises(H.Refused) as e:
            H.verify_stored(row)
        assert e.value.code == "tampered_candidate"
