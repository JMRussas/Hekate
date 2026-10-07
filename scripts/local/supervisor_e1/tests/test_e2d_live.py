"""E2d live (plan 032 rev 6 §6 cases 2 and 4; TEST-ONLY, disposable database): review-lead rollover as
prepare / commit / activate over the E2c route A adapter, against REAL claims, finishes and review
facts. No conversation is launched, woken or invoked; activation is a client obligation."""

import json
import uuid

import psycopg
import pytest

from e1 import acts as A
from e1 import handoff as H
from e1.durable import CommitUnknown, audit_counters, confirm
from e1.evidence import JournalRefused
from e1.handoff_durable import commit, install_handoff, prepare, redeliver, verify_receipt
from e2b_support import W, raw
from test_e2c_live import LS1, LS2, LS3, Live, e2c, ev  # noqa: F401 (fixture)

TASK_INSTR = {"system": "sys", "fast": "fast", "deep": "deep"}


class Review:
    """A finished target with a requested review bound to LS1, plus the handoff schema."""

    def __init__(self, harness, setup, client, e2c, now=1000.0):
        self.L = L = Live(harness, setup, client, e2c, now=now)
        install_handoff(L.dsn)
        L.finish()
        self.rk = {**{k: L.key[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art", "lead": "lead-1", "leadSession": LS1}
        assert L.aj.request_review(L.p.root, L.ck, self.rk).outcome == "accepted"
        self.rid = A.review_id(self.rk)
        self.task = {"text": f"Review {L.p.target} artifact sha-art", "instructions": TASK_INSTR, "packageRef": L.key["packageRef"]}

    def act(self, kind, seq, session, cp=None, evd=None):
        return self.L.act(kind, seq, cp, evd, key_={**self.rk, "leadSession": session})

    def prep(self, target=LS2, prepare_id=None, **kw):
        return prepare(self.L.aj, self.L.p.root, self.L.ck, self.rk, prepare_id=prepare_id or str(uuid.uuid4()), target=target,
                       gate=kw.pop("gate", "operator"), gate_ref=kw.pop("gate_ref", "recon-e2d"), task=self.task,
                       conversation_ref="conv-successor", now=self.L.aj.now, **kw)

    def commit(self, p, **kw):
        return commit(self.L.aj, p, now=self.L.aj.now, **kw)

    def lookup(self):
        return self.L.read({"attemptKey": {k: self.rk[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art"})

    def rebinds(self):
        return self.L.records().count("review_rebind")


@pytest.fixture
def R(harness, setup, client, e2c):
    return lambda **kw: Review(harness, setup, client, e2c, **kw)


def test_prepare_grants_no_authority_and_the_candidate_is_immutable(R):
    r = R()
    p = r.prep()
    assert p.package.manifest["transition"]["status"] == "candidate"
    assert r.L.intake(r.act("review_acknowledged", 1, LS2)).outcome == "foreign_session"      # target is not the binding yet
    assert r.lookup()["identity"]["reviewKey"]["leadSession"] == LS1 and r.rebinds() == 0
    for stmt in ("UPDATE supervisor_journal.e2d_candidates SET candidate_digest = repeat('0', 64)",
                 "DELETE FROM supervisor_journal.e2d_candidates", "TRUNCATE supervisor_journal.e2d_candidates"):
        with pytest.raises(psycopg.errors.RaiseException):
            raw(r.L.dsn, stmt)


def test_prepare_lists_pending_effects_from_the_same_snapshot(R):
    r = R()
    p = r.prep()
    pending = p.package.manifest["authority"]["pending"]
    assert any(i["kind"] == "open_intent" and i["id"].startswith("dispatch_intent@") for i in pending)   # no dispatch_outcome yet
    assert p.package.manifest["state"]["basis"]["snapshot"]


def test_commit_receipt_activation_anchors_and_novelty(R):
    r = R()
    r.L.aj.now = 1100.0
    assert r.L.intake(r.act("review_acknowledged", 1, LS1)).outcome == "accepted"
    r.L.aj.now = 1200.0
    assert r.L.intake(r.act("review_progress", 2, LS1, 1, ev(1))).outcome == "accepted"
    anchor = r.lookup()["current"]["deadlinePhase"]["value"]
    p = r.prep()
    d, rec = r.commit(p)
    assert d.outcome == "accepted" and rec["recordId"] == p.transition["recordId"] and rec["bindingLinkId"] == p.transition["linkId"]
    assert verify_receipt(r.L.dsn, r.L.p.root, r.L.ck, r.rid, rec) == "current"
    assert r.lookup()["identity"]["reviewKey"]["leadSession"] == LS2
    assert r.lookup()["current"]["deadlinePhase"]["value"]["anchorAt"] == anchor["anchorAt"]          # never re-anchored
    assert r.L.intake(r.act("review_progress", 3, LS1, 2, ev(2))).outcome == "foreign_session"      # old lead refused
    assert r.L.intake(r.act("review_progress", 3, LS2, 2, ev(1))).outcome == "not_novel"            # novelty continues
    r.L.aj.now = 1300.0
    assert r.L.intake(r.act("review_progress", 3, LS2, 2, ev(2))).outcome == "accepted"
    assert audit_counters(r.L.dsn) == []


def test_semantic_change_between_prepare_and_commit_is_stale_but_diagnostics_are_not(R):
    r = R()
    p = r.prep()
    r.L.intake(r.act("review_acknowledged", 1, LS1))                   # diagnostic change: ACK appears
    d, rec = r.commit(p)
    assert d.outcome == "accepted"

    r2 = R()
    p2 = r2.prep()
    r2.L.intake(r2.act("review_acknowledged", 1, LS1))
    r2.L.intake(r2.act("review_acknowledged", 2, LS1))                 # conflict -> a new queue reason (semantic)
    n = r2.rebinds()
    d2, rec2 = r2.commit(p2)
    assert d2.outcome == "stale_candidate" and "queue" in d2.reason and rec2 is None and r2.rebinds() == n


def test_stale_prepare_then_reprepare_then_commit(R):
    """msg 1256: a genuinely new prepare (new prepareId) is possible after stale_candidate."""
    r = R()
    p = r.prep()
    r.L.intake(r.act("review_acknowledged", 1, LS1))
    r.L.intake(r.act("review_acknowledged", 2, LS1))                   # stale the candidate
    assert r.commit(p)[0].outcome == "stale_candidate"
    p2 = r.prep()                                                      # fresh prepareId, same predecessor and target
    assert p2.transition["handoffId"] != p.transition["handoffId"]
    assert r.commit(p2)[0].outcome == "accepted"


def test_exact_retry_returns_the_committed_record_and_conflicts_are_refused(R):
    r = R()
    pid = str(uuid.uuid4())
    p = r.prep(prepare_id=pid)
    d1, rec1 = r.commit(p)
    d2, rec2 = r.commit(p)                                             # retry check runs BEFORE the CAS
    assert (d1.outcome, d2.outcome) == ("accepted", "committed") and rec1 == rec2 and r.rebinds() == 1
    with psycopg.connect(r.L.dsn) as c:
        assert c.execute("SELECT candidate_digest FROM supervisor_journal.e2d_candidates WHERE handoff_id = %s",
                         (p.transition["handoffId"],)).fetchone()[0] == p.package.candidate_digest


def test_same_prepare_id_with_other_content_is_a_conflict_before_commit(R):
    r = R()
    pid = str(uuid.uuid4())
    r.prep(prepare_id=pid)
    with pytest.raises(H.Refused) as e:
        r.prep(prepare_id=pid, note={"text": "changed", "author": LS1})
    assert e.value.code == "conflict"


def test_a_bare_repeated_review_rebind_is_still_stale_binding(R):
    r = R()
    root_link = json.loads(_record(r, "review_requested")["data"])["linkId"]
    assert r.L.aj.bind(r.L.p.root, r.L.ck, r.L.p.target, r.rid, "review_rebind", root_link, LS2, gate="operator",
                       gate_ref="r1").outcome == "accepted"
    assert r.L.aj.bind(r.L.p.root, r.L.ck, r.L.p.target, r.rid, "review_rebind", root_link, LS2, gate="operator",
                       gate_ref="r1").outcome == "stale_binding"


def _record(r, kind):
    with psycopg.connect(r.L.dsn, row_factory=psycopg.rows.dict_row) as c:
        return c.execute("SELECT record_id::text AS record_id, data FROM supervisor_journal.records WHERE root = %s AND "
                         "claim_key = %s AND kind = %s ORDER BY seq DESC LIMIT 1", (r.L.p.root, r.L.ck, kind)).fetchone()


def test_lost_commit_reply_is_confirmed_on_the_exact_identity(R):
    r = R()
    p = r.prep()

    def lose(stage, aj):
        if stage == "after_commit":
            raise ConnectionError("reply lost")
    r.L.aj.fault = lose
    with pytest.raises(CommitUnknown) as e:
        r.commit(p)
    assert e.value.record_id == p.transition["recordId"]
    assert confirm(r.L.dsn, W, r.L.aj.unknown).status == "committed"
    r.L.aj.fault = None
    r.L.aj.reacquire()
    d, rec = r.commit(p)                                               # the same identity: the committed record
    assert d.outcome == "committed" and rec["recordId"] == p.transition["recordId"] and r.rebinds() == 1


def test_superseded_receipt_and_delivery_failure(R):
    r = R()
    p = r.prep()
    d, rec = r.commit(p)
    digest, envelope, task = redeliver(r.L.dsn, p.transition["handoffId"])   # "delivery failed": the SAME candidate again
    assert digest == p.package.candidate_digest and envelope == p.package.envelope and task["text"] == r.task["text"]
    assert task["instructions"] == r.task["instructions"]                      # exact Task bytes are retained
    p3 = r.prep(target=LS3)                                            # a later handoff LS2 -> LS3
    assert r.commit(p3)[0].outcome == "accepted"
    assert verify_receipt(r.L.dsn, r.L.p.root, r.L.ck, r.rid, rec) == "superseded"
    assert r.L.intake(r.act("review_acknowledged", 1, LS2)).outcome == "foreign_session"
    assert verify_receipt(r.L.dsn, r.L.p.root, r.L.ck, r.rid, dict(rec, recordHash="0" * 64)) == "mismatch"


def test_post_commit_act_before_any_receipt_fetch_is_accepted_no_fence(R):
    """Documented, not hidden (032 §3a): activation is a client obligation; binding validation alone
    accepts the successor once the commit is durable."""
    r = R()
    d, _ = r.commit(r.prep())
    assert d.outcome == "accepted"
    assert r.L.intake(r.act("review_acknowledged", 1, LS2)).outcome == "accepted"


def test_crash_after_prepare_leaves_an_orphan_without_authority(R, e2c):
    r = R()
    p = r.prep()
    r.L.aj.close()
    r.L.aj = e2c.writer(now=r.L.aj.now)                                # restarted supervisor; the Python Prepared is "lost"
    assert r.lookup()["identity"]["reviewKey"]["leadSession"] == LS1 and r.rebinds() == 0       # the orphan changed nothing
    d = commit(r.L.aj, p.transition["handoffId"], now=r.L.aj.now)[0]   # by id only, from the stored candidate
    assert d.outcome == "accepted", d.reason                           # still fresh: may be committed later
    # (A refused probe act from the target would add a foreign_session queue reason, a semantic
    # change that correctly makes the candidate stale; test_prepare_grants_no_authority covers it.)


def test_operator_gated_lead_transition_without_quiescence_proof(R):
    r = R()
    r.L.intake(r.act("review_acknowledged", 1, LS1))
    d, _ = r.commit(r.prep(gate="operator", gate_ref="lead-unresponsive"))
    assert d.outcome == "accepted"
    assert r.L.intake(r.act("review_progress", 2, LS1, 1, ev(1))).outcome == "foreign_session"   # old lead's later acts


def test_release_gate_must_be_the_old_session(R):
    r = R()
    p = r.prep(gate="release", gate_ref=LS3)
    assert r.commit(p)[0].outcome == "refused" and r.rebinds() == 0
    assert r.commit(r.prep(gate="release", gate_ref=LS1))[0].outcome == "accepted"


def test_a_note_never_counts_as_progress(R):
    r = R()
    r.L.intake(r.act("review_acknowledged", 1, LS1))
    before = r.lookup()["current"]["progress"]
    r.commit(r.prep(note={"text": "I reviewed everything, checkpoint 5 done", "author": LS1}))
    assert r.lookup()["current"]["progress"] == before


def test_content_revised_before_prepare_refuses_the_prepare(R, setup):
    r = R()
    node = next(n for n in r.L.setup.plan(r.L.p.root).body["nodes"] if n["id"] == r.L.p.target)
    from helpers import key as k, ok
    ok(setup.revise(r.L.p.target, "revised before prepare", node["contentRevision"], k(), node["stateRevision"]))
    with pytest.raises(H.Refused) as e:
        r.prep()
    assert e.value.code == "stale_content"


def test_json_escaped_task_bytes_are_stored_and_redelivered_exactly(R):
    r = R()
    r.task = dict(r.task, text=chr(10) * 50000)                      # 50 KB delivered, ~100 KB as JSON
    p = r.prep()
    assert p.package.bytes_required <= H.REQUIRED_MAX
    assert redeliver(r.L.dsn, p.transition["handoffId"])[2]["text"] == r.task["text"]


def test_a_corrupt_stream_never_confirms_a_retry(R):
    r = R()
    p = r.prep()
    assert r.commit(p)[0].outcome == "accepted"
    raw(r.L.dsn, ("UPDATE supervisor_journal.records SET record_hash = repeat('0', 64) WHERE record_id = %s",
                  (p.transition["recordId"],)), replica=True)
    d, rec = r.commit(p)
    assert d.outcome == "corrupt" and rec is None


def test_a_tampered_prepared_object_is_rejected(R):
    """msg 1260: commit uses the IMMUTABLE stored candidate; a caller object that differs is refused."""
    r = R()
    p = r.prep()
    p.transition["targetSession"] = LS3                                # nested dicts are mutable
    d, rec = r.commit(p)
    assert d.outcome == "tampered_candidate" and rec is None and r.rebinds() == 0
    p.transition["targetSession"] = LS2
    p.package.manifest["authority"]["pending"] = []
    assert r.commit(p)[0].outcome == "tampered_candidate"


def test_stored_candidate_tampering_is_detected(R):
    r = R()
    p = r.prep()
    raw(r.L.dsn, ("UPDATE supervisor_journal.e2d_candidates SET task = replace(task, 'Review', 'Rewrite') WHERE handoff_id = %s",
                  (p.transition["handoffId"],)), replica=True)         # bypassing the guard, as a credentialed user could
    assert commit(r.L.aj, p.transition["handoffId"], now=r.L.aj.now)[0].outcome == "tampered_candidate"
    with pytest.raises(H.Refused):
        redeliver(r.L.dsn, p.transition["handoffId"])


def test_real_content_change_between_prepare_and_commit_is_stale(R, setup):
    """msg 1260: freshness compares the CURRENT PlanStore pins read under the lock, not the task twice."""
    r = R()
    p = r.prep()
    node = next(n for n in r.L.setup.plan(r.L.p.root).body["nodes"] if n["id"] == r.L.p.target)
    from helpers import key as k, ok
    ok(setup.revise(r.L.p.target, "target spec v2", node["contentRevision"], k(), node["stateRevision"]))
    d, rec = r.commit(p)
    assert d.outcome == "stale_candidate" and "pins" in d.reason and rec is None and r.rebinds() == 0
