"""E2c live (plan 030 rev 7 §12; TEST-ONLY, disposable database): route A over the E2b-a durable
journal against REAL PlanStore claims, transitions and decisions. The project advisory lock is held
through each append; the C-B read is one REPEATABLE READ snapshot. No worker, provider, wake, send,
bridge or ChatAgent. Fixture constants only."""

import json
import threading
import time
import uuid

import psycopg
import pytest

from e1 import acts as A
from e1.acts import E2C_BOUNDS, G, canonical, exec_id, review_id
from e1.acts_durable import ActsJournal, independent_facts, install_acts, read_cb
from e1.durable import CommitUnknown, audit_counters, compact, confirm, install
from e1.evidence import Bounds, JournalRefused
from e2b_support import W, raw, reset_schema
from helpers import key, ok
from test_supervisor_live import make_plan, rev

H = lambda c: c * 64                                     # noqa: E731
WS = f"hkw1:worker-a:{uuid.uuid4()}"
LS1, LS2, LS3 = (f"hkw1:lead-1:{uuid.uuid4()}" for _ in range(3))


@pytest.fixture
def e2c(harness):
    opened: list[ActsJournal] = []

    def make(bounds: Bounds = E2C_BOUNDS) -> str:
        reset_schema(harness.dsn)
        install(harness.dsn, bounds)
        install_acts(harness.dsn)
        return harness.dsn

    def writer(now: float = 100.0, **kw) -> ActsJournal:
        aj = ActsJournal(harness.dsn, W, now=now, **kw)
        opened.append(aj)
        return aj.open()

    from types import SimpleNamespace
    yield SimpleNamespace(make=make, writer=writer, dsn=harness.dsn)
    for aj in opened:
        aj.close()


class Live:
    """One real claim on the plan's target node plus its journal stream and dispatched ExecutionKey."""

    def __init__(self, harness, setup, client, e2c, *, now=100.0, bounds=E2C_BOUNDS):
        self.dsn = e2c.make(bounds)
        self.setup, self.client = setup, client
        self.p = make_plan(harness, setup)
        self.ck = key()
        self.receipt = ok(client.claim(self.p.root, self.ck, "att-1", WS, "supervisor-e2c"))["receipt"]
        assert self.receipt["outcome"] == "claimed" and self.receipt["nodeId"] == self.p.target
        self.aj = e2c.writer(now=now)
        self.aj.append(self.p.root, self.ck, "claim_intent", {"attemptId": "att-1", "executorRef": WS, "actor": "supervisor-e2c"})
        self.aj.append(self.p.root, self.ck, "claimed", {"outcome": "claimed", "nodeId": self.p.target, "attemptId": "att-1",
                                                          "attemptEpoch": self.receipt["attemptEpoch"]})
        self.key = self.ekey()
        self.eid = self.aj.dispatch(self.p.root, self.ck, self.key)

    def package(self, **over) -> str:
        r = self.receipt
        doc = {"kind": "supplied.v1", "suppliedSha256": H("a"), "instructions": {"system": H("b"), "fast": H("c"), "deep": H("d")},
               "contentRevision": r["contentRevision"],
               # PlanStore's content digest is UPPERCASE hex; frozen 030 H is lowercase (reported finding).
               "contentDigest": r["contentDigest"].lower(), "prereqDigest": r["prereqDigest"]}
        doc.update(over)
        return canonical(doc)

    def ekey(self, **over) -> dict:
        k = {"rootId": self.p.root, "nodeId": self.p.target, "attemptId": "att-1", "attemptEpoch": self.receipt["attemptEpoch"],
             "claimKey": self.ck, "runId": "run-1", "packageRef": self.package(), "workerSession": WS}
        k.update(over)
        return k

    def act(self, kind, seq, cp=None, ev=None, key_=None) -> str:
        d = {"kind": kind, "key": key_ or self.key, "actSeq": seq}
        if cp is not None:
            d["checkpointId"], d["evidenceDigest"] = cp, ev
        return json.dumps(d)

    def intake(self, *a, at=None, **kw):
        if at is not None:
            self.aj.now = at
        return self.aj.intake(self.p.root, self.ck, *a, **kw)

    def read(self, req=None, now=None, **kw):
        return read_cb(self.dsn, self.p.root, self.ck, self.p.target, req or {"executionKey": self.key}, now=now if now is not None else self.aj.now,
                       attempt=("att-1", self.receipt["attemptEpoch"]), **kw)

    def current(self, **kw):
        return self.read(**kw)["current"]

    def state_rev(self) -> int:
        return rev(self.setup, self.p.root, self.p.target)

    def finish(self, actor="e1-setup", artifact="sha-art"):
        ok(self.setup.transition(self.p.target, {"to": "done", "attemptId": "att-1", "attemptEpoch": self.receipt["attemptEpoch"],
                                                  "artifactRef": artifact, "operationKey": key(), "expectedStateRevision": self.state_rev(),
                                                  "actor": actor}))

    def records(self) -> list:
        with psycopg.connect(self.dsn) as c:
            return [r[0] for r in c.execute("SELECT kind FROM supervisor_journal.records WHERE root = %s AND claim_key = %s ORDER BY seq",
                                            (self.p.root, self.ck))]


def ev(i: int) -> str:
    return f"{i:064x}"


# ======================================================================== route A against real PlanStore facts

def test_route_a_phases_and_c_b_from_one_snapshot(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c, now=100.0)
    cur = L.current()
    assert cur["status"]["value"] == "dispatch_intended" and cur["deadlinePhase"]["value"]["anchorAt"] == 100.0
    assert L.intake(L.act("worker_ack", 1), at=200.0).outcome == "accepted"
    assert L.intake(L.act("worker_progress", 2, 1, ev(1)), at=300.0).outcome == "accepted"
    r = L.read()
    assert r["identity"]["executionKey"] == L.key and r["basis"]["route"] == "A" and "snapshot" in r["basis"]
    p = r["current"]["deadlinePhase"]
    assert p["proof"] == "complete" and (p["value"]["phase"], p["value"]["anchorAt"]) == ("progress", 300.0)
    assert r["current"]["prerequisites"] == {"value": "unknown", "proof": "unverified"}
    assert audit_counters(L.dsn) == []


def test_restart_keeps_original_anchors_and_pending_review(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c, now=100.0)
    L.intake(L.act("worker_ack", 1), at=200.0)
    L.aj.close()
    again = e2c.writer(now=90_000.0)                               # a restarted writer, much later
    assert again.epoch == L.aj.epoch
    p = L.current(now=90_000.0)["deadlinePhase"]["value"]
    assert (p["phase"], p["anchorAt"], p["overdue"]) == ("first_progress", 200.0, True)
    L.aj = again
    L.finish()
    assert L.aj.request_review(L.p.root, L.ck, {**{k: L.key[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art", "lead": "lead-1",
                                                  "leadSession": None}).outcome == "accepted"
    L.aj.close()
    again2 = e2c.writer(now=95_000.0)
    rr = read_cb(L.dsn, L.p.root, L.ck, L.p.target, {"attemptKey": {k: L.key[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art"},
                 now=95_000.0)
    assert rr["current"]["review"]["value"] == "review_overdue" and rr["current"]["deadlinePhase"]["value"]["anchorAt"] == 90_000.0
    again2.close()


def test_stale_content_refuses_new_acts_and_counters_persist(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    assert L.intake(L.act("worker_ack", 1)).outcome == "accepted"
    ok(setup.revise(L.p.target, "target spec v2", L.receipt["contentRevision"], key(), L.state_rev()))
    d = L.intake(L.act("worker_progress", 2, 1, ev(1)))
    assert (d.outcome, d.reason) == ("stale", "stale_content")
    r = L.read()
    assert r["counters"]["stale"] == 1 and r["current"]["ack"]["proof"] == "complete"    # accepted evidence kept


def test_duplicate_conflict_and_malformed_persist_without_records(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    L.intake(L.act("worker_ack", 1))
    n = len(L.records())
    assert L.intake(L.act("worker_ack", 1)).outcome == "duplicate"
    assert L.intake(L.act("worker_ack", 2)).outcome == "conflict"
    assert L.intake("{not json").outcome == "malformed"
    assert len(L.records()) == n
    r = L.read()
    assert r["counters"]["duplicate"] == 1 and r["counters"]["conflict"] == 1 and r["queue"] == ["conflict"]
    with psycopg.connect(L.dsn) as c:
        assert c.execute("SELECT value FROM supervisor_journal.e2c_counters WHERE root = %s AND claim_key = %s AND "
                         "stream_key = '_stream' AND name = 'malformed'", (L.p.root, L.ck)).fetchone()[0] == 1


# ======================================================================== route A holds PlanStore stable through the append

def test_planstore_writers_wait_for_the_route_a_transaction(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    order: list[str] = []
    started = threading.Event()

    def competing_finish():
        started.set()
        L.finish(actor="competitor")
        order.append("finish_committed")

    t = threading.Thread(target=competing_finish)
    state = {}

    def hold(view, facts):
        state["work"] = facts.node["work_status"]
        t.start()
        started.wait(5)
        time.sleep(1.5)                                             # the finish is blocked on the project lock
        state["finish_done_inside"] = bool(order)
        act = A.parse_act(L.act("worker_ack", 1))
        return A.decide_act(view, act, facts)

    d = L.aj._route_a(L.p.root, L.ck, L.p.target, ("att-1", L.receipt["attemptEpoch"]), hold,
                      record_id=A.act_record_id(A.parse_act(L.act("worker_ack", 1)).act_id))
    order.append("ack_committed")
    t.join(30)
    assert d.outcome == "accepted" and state == {"work": "in_progress", "finish_done_inside": False}
    assert order == ["ack_committed", "finish_committed"]
    assert L.current()["status"]["value"] == "finished"             # matching finish by another actor


def test_other_same_session_callers_cannot_commit_an_in_flight_route_a(harness, setup, client, e2c):
    """msg 1185: while route A is paused after its facts and locks, a malformed-act caller, a transport
    append and a PlanStore writer all wait; none commits or releases the project lock first."""
    L = Live(harness, setup, client, e2c)
    inside, release = threading.Event(), threading.Event()
    order: list[str] = []

    def paused(view, facts):
        inside.set()
        release.wait(30)
        return A.decide_act(view, A.parse_act(L.act("worker_ack", 1)), facts)

    ra = threading.Thread(target=lambda: (L.aj._route_a(L.p.root, L.ck, L.p.target, ("att-1", L.receipt["attemptEpoch"]), paused,
                                                         record_id=A.act_record_id(A.parse_act(L.act("worker_ack", 1)).act_id)),
                                          order.append("route_a")))
    ra.start()
    assert inside.wait(10)
    others = [threading.Thread(target=lambda: (L.intake("{bad"), order.append("malformed"))),
              threading.Thread(target=lambda: (L.aj.transport(L.p.root, L.ck, "launched", "pid-9"), order.append("transport"))),
              threading.Thread(target=lambda: (L.finish(actor="competitor"), order.append("planstore_finish")))]
    for t in others:
        t.start()
    time.sleep(2.0)
    assert order == [] and all(t.is_alive() for t in others)
    with psycopg.connect(L.dsn) as c:                                # nothing committed by the waiting callers
        assert c.execute("SELECT count(*) FROM supervisor_journal.e2c_counters WHERE name = 'malformed'").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM supervisor_journal.records WHERE kind = 'transport_observed'").fetchone()[0] == 0
        assert c.execute("SELECT work_status FROM public.plan_node_state WHERE node_id = %s", (L.p.target,)).fetchone()[0] == "in_progress"
    release.set()
    for t in [ra, *others]:
        t.join(30)
    assert order[0] == "route_a" and sorted(order[1:]) == ["malformed", "planstore_finish", "transport"]
    kinds = L.records()
    assert kinds.index("worker_ack") < kinds.index("transport_observed") and audit_counters(L.dsn) == []


def test_c_b_is_one_snapshot_even_if_planstore_moves_mid_read(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    L.intake(L.act("worker_ack", 1))
    r = L.read(before_journal=lambda: L.finish(actor="mid-read"))
    assert r["current"]["status"]["value"] == "taken_up_self_reported"        # the snapshot's facts, not the later finish
    assert L.current()["status"]["value"] == "finished"


# ======================================================================== stream ends against real transitions

def test_matching_finish_is_finished_and_late_acts_are_stale(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    L.intake(L.act("worker_ack", 1))
    L.finish(actor="someone-else")
    assert L.current()["status"]["value"] == "finished"
    assert L.aj.record_end(L.p.root, L.ck, L.key).outcome == "accepted"
    assert L.intake(L.act("worker_progress", 2, 1, ev(1))).outcome == "stale"
    assert L.current()["ack"]["value"] is not None


def test_release_and_reopen_are_distinct_supersessions(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    L.intake(L.act("worker_ack", 1))
    ok(setup.transition(L.p.target, {"to": "todo", "attemptId": "att-1", "attemptEpoch": L.receipt["attemptEpoch"], "operationKey": key(),
                                     "expectedStateRevision": L.state_rev(), "actor": "e1-setup"}))
    assert L.current()["status"]["value"] == "superseded:released"
    assert L.aj.record_end(L.p.root, L.ck, L.key).outcome == "accepted"

    L2 = Live(harness, setup, client, e2c)
    L2.finish()
    ok(setup.transition(L2.p.target, {"to": "in_progress", "attemptId": "att-2", "operationKey": key(),
                                      "expectedStateRevision": L2.state_rev(), "actor": "e1-setup"}))
    assert L2.current()["status"]["value"] == "superseded:reopened"


def test_cancel_is_its_own_supersession(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    ok(setup.transition(L.p.target, {"to": "cancelled", "operationKey": key(), "expectedStateRevision": L.state_rev(), "actor": "e1-setup"}))
    assert L.current()["status"]["value"] == "superseded:cancelled"


# ======================================================================== route B and confirm_act

def test_route_b_observation_is_audit_only_and_confirm_keeps_the_original_at(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c, now=100.0)
    facts_b = independent_facts(L.dsn, L.p.root, L.p.target)
    d = L.intake(L.act("worker_ack", 1), at=200.0, route_b_facts=facts_b)
    assert d.outcome == "observed" and L.current()["deadlinePhase"]["value"]["phase"] == "ack"
    L.aj.now = 9_000.0
    c = L.aj.confirm_act(L.p.root, L.ck, L.p.target, d.append[1]["obsId"], "recon-live")
    assert c.outcome == "accepted"
    p = L.current()["deadlinePhase"]["value"]
    assert (p["phase"], p["anchorAt"], p["overdue"]) == ("first_progress", 200.0, True)


# ======================================================================== review: binding chain, CAS, ordering, decision

def review_setup(harness, setup, client, e2c, session=None, now=1000.0):
    L = Live(harness, setup, client, e2c, now=now)
    L.finish()
    rk = {**{k: L.key[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art", "lead": "lead-1", "leadSession": session}
    d = L.aj.request_review(L.p.root, L.ck, rk)
    assert d.outcome == "accepted"
    return L, review_id(rk), d.append[1]["linkId"], rk


def rlookup(L, now=None):
    return L.read({"attemptKey": {k: L.key[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art"}, now=now)


def test_review_lookup_after_atomic_bind_and_rebind_with_cas(harness, setup, client, e2c):
    L, rid, root, rk = review_setup(harness, setup, client, e2c)
    assert rlookup(L)["identity"]["reviewKey"]["leadSession"] is None
    assert L.intake(L.act("review_acknowledged", 1, key_={**rk, "leadSession": LS1}), at=1100.0).outcome == "accepted"
    assert rlookup(L)["identity"]["reviewKey"]["leadSession"] == LS1
    anchor = rlookup(L)["current"]["deadlinePhase"]["value"]
    stale_pred = root                                               # what a caller read BEFORE the atomic bind
    n = len(L.records())
    d = L.aj.bind(L.p.root, L.ck, L.p.target, rid, "review_rebind", stale_pred, LS2, gate="operator", gate_ref="recon")
    assert d.outcome == "stale_binding" and len(L.records()) == n  # CAS refused, nothing appended, writer still usable
    L.aj.verify_fence()
    cur = rlookup(L)
    bound = [h for h in cur["historical"]["items"] if h["kind"] == "binding"]
    pred = None
    with psycopg.connect(L.dsn) as c:
        for (data,) in c.execute("SELECT data FROM supervisor_journal.records WHERE root = %s AND claim_key = %s AND kind = "
                                 "'review_acknowledged'", (L.p.root, L.ck)):
            pred = json.loads(data)["bind"]["linkId"]
    assert bound and pred
    assert L.aj.bind(L.p.root, L.ck, L.p.target, rid, "review_rebind", pred, LS2, gate="release", gate_ref=LS1).outcome == "accepted"
    r = L.read({"reviewKey": {**rk, "leadSession": LS2}})
    assert r["identity"]["reviewKey"]["leadSession"] == LS2
    assert r["current"]["deadlinePhase"]["value"]["anchorAt"] == anchor["anchorAt"]
    with pytest.raises(A.Refused) as e:
        L.read({"reviewKey": {**rk, "leadSession": LS1}})
    assert e.value.code == "stale_binding"


def test_stale_cas_after_the_winning_commit(harness, setup, client, e2c):
    """SEQUENTIAL: a caller whose predecessor was read before another rebind committed is refused
    stale_binding by the in-transaction compare-and-set (no concurrency is claimed here)."""
    L, rid, root, rk = review_setup(harness, setup, client, e2c, session=LS1)
    seen_by_caller = root
    assert L.aj.bind(L.p.root, L.ck, L.p.target, rid, "review_rebind", seen_by_caller, LS2, gate="operator", gate_ref="r1").outcome == "accepted"
    d = L.aj.bind(L.p.root, L.ck, L.p.target, rid, "review_rebind", seen_by_caller, LS3, gate="operator", gate_ref="r2")
    assert d.outcome == "stale_binding"
    assert rlookup(L)["identity"]["reviewKey"]["leadSession"] == LS2


def test_competing_rebinds_race_through_the_single_writer_boundary(harness, setup, client, e2c):
    """CONCURRENT: two callers read the same current binding, then submit competing rebinds at the
    same moment from two threads. The writer's serialization boundary orders them; the in-transaction
    CAS accepts exactly one and refuses the other stale_binding. A second session for the same
    writer is refused before any intake (E2b-a fence), never weakened for the test."""
    L, rid, root, rk = review_setup(harness, setup, client, e2c, session=LS1)
    with pytest.raises(JournalRefused) as e:
        ActsJournal(L.dsn, W).open()
    assert e.value.code == "fence_busy"
    barrier = threading.Barrier(2)
    inside: list[str] = []
    results: dict[str, str] = {}

    def submit(name, session):
        barrier.wait(10)

        def hook():
            inside.append(name)
            time.sleep(0.5)                                            # widen the window inside the transaction
        results[name] = L.aj.bind(L.p.root, L.ck, L.p.target, rid, "review_rebind", root, session, gate="operator",
                                  gate_ref=f"recon-{name}", before_decide=hook).outcome

    ts = [threading.Thread(target=submit, args=(n, s)) for n, s in (("a", LS2), ("b", LS3))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert sorted(results.values()) == ["accepted", "stale_binding"] and len(inside) == 2
    winner = LS2 if results["a"] == "accepted" else LS3
    assert rlookup(L)["identity"]["reviewKey"]["leadSession"] == winner
    assert L.records().count("review_rebind") == 1


def test_equal_and_regressing_at_resolve_by_linkage(harness, setup, client, e2c):
    L, rid, root, rk = review_setup(harness, setup, client, e2c, session=LS1, now=500.0)
    L.aj.now = 500.0
    a = L.aj.bind(L.p.root, L.ck, L.p.target, rid, "review_rebind", root, LS2, gate="operator", gate_ref="r1")
    L.aj.now = 10.0
    b = L.aj.bind(L.p.root, L.ck, L.p.target, rid, "review_rebind", a.append[1]["linkId"], LS3, gate="operator", gate_ref="r2")
    assert b.outcome == "accepted"
    r = rlookup(L, now=600.0)
    assert r["identity"]["reviewKey"]["leadSession"] == LS3
    assert (r["current"]["deadlinePhase"]["value"]["phase"], r["current"]["deadlinePhase"]["value"]["anchorAt"]) == ("ack", 500.0)


def test_exact_decision_ends_the_review_with_acceptance_unverified(harness, setup, client, e2c):
    L, rid, root, rk = review_setup(harness, setup, client, e2c, session=LS1)
    ok(setup.decide(L.p.target, {"decision": "accepted", "reviewedContentRevision": L.receipt["contentRevision"], "reviewedArtifactRef": "sha-art",
                                 "reviewedAttemptEpoch": L.receipt["attemptEpoch"], "evidenceRef": "ev-e2c", "operationKey": key(),
                                 "expectedStateRevision": L.state_rev(), "actor": "e1-verifier"}))
    cur = rlookup(L)["current"]
    assert cur["review"]["value"] == "ended" and cur["acceptanceValidity"]["proof"] == "unverified"
    assert L.intake(L.act("review_acknowledged", 1, key_={**rk, "leadSession": LS1})).outcome == "stale"


# ======================================================================== E2b-a properties preserved

def test_lost_commit_reply_then_confirm_and_idempotent_resend(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)

    def lose_reply(stage, aj):
        if stage == "after_commit":
            raise ConnectionError("reply lost")
    L.aj.fault = lose_reply
    with pytest.raises(CommitUnknown):
        L.intake(L.act("worker_ack", 1))
    with pytest.raises(JournalRefused):
        L.intake(L.act("worker_ack", 1))                             # broken until an explicit reacquire()
    assert confirm(L.dsn, W, L.aj.unknown).status == "committed"
    L.aj.fault = None
    L.aj.reacquire()
    assert L.intake(L.act("worker_ack", 1)).outcome == "duplicate"   # the held act, resent: no second record
    assert L.records().count("worker_ack") == 1 and audit_counters(L.dsn) == []


def test_damaged_progress_record_hides_progress_only_and_is_retained(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    L.intake(L.act("worker_ack", 1))
    L.intake(L.act("worker_progress", 2, 1, ev(1)))
    raw(L.dsn, ("UPDATE supervisor_journal.records SET data = replace(data, %s, %s) WHERE root = %s AND claim_key = %s AND kind = "
                "'worker_progress'", (ev(1), ev(2), L.p.root, L.ck)), replica=True)
    cur = L.current()
    assert cur["ack"]["proof"] == "complete" and cur["progress"] == {"value": "unknown", "proof": "corrupt"}
    d = L.intake(L.act("worker_progress", 3, 2, ev(3)))
    assert d.outcome == "novelty_unknown"                            # fails closed: counter + one queue entry, no record
    assert "novelty_unknown" in L.read()["queue"]
    with pytest.raises(JournalRefused) as e:
        L.aj.transport(L.p.root, L.ck, "launched", "pid-1")          # E2b-a: a corrupt stream refuses every append
    assert e.value.code == "corrupt"
    assert compact(L.dsn, 10**9)["compacted"] == 0                   # and is never compacted


def test_counter_guards_and_saturation(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    L.intake(L.act("worker_ack", 1))
    L.intake(L.act("worker_ack", 1))                                 # duplicate -> counter row
    L.intake(L.act("worker_ack", 2))                                 # conflict -> counter + queue row
    raw(L.dsn, ("UPDATE supervisor_journal.e2c_counters SET value = %s WHERE stream_key = %s AND name = 'duplicate'",
                (A.COUNTER_MAX - 1, L.eid)))
    before = L.current()
    L.intake(L.act("worker_ack", 1))
    L.intake(L.act("worker_ack", 1))
    r = L.read()
    assert r["counters"]["duplicate"] == A.COUNTER_MAX and "duplicate" in r["counters"]["saturated"]
    assert r["current"] == before
    for stmt in ("UPDATE supervisor_journal.e2c_counters SET value = 0", "DELETE FROM supervisor_journal.e2c_counters",
                 "TRUNCATE supervisor_journal.e2c_queue", "UPDATE supervisor_journal.e2c_queue SET reason = 'x'"):
        with pytest.raises(psycopg.errors.RaiseException):
            raw(L.dsn, stmt)


def test_g_cap_with_a_full_supplied_package(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    assert len(L.key["packageRef"]) > 500                          # a representative full supplied.v1 package
    L.intake(L.act("worker_ack", 1))
    for i in range(1, G + 1):
        assert L.intake(L.act("worker_progress", 1 + i, i, ev(i)), at=1000.0 + i).outcome == "accepted"
    before = L.current()
    assert L.intake(L.act("worker_progress", 500, 500, ev(500)), at=9000.0).outcome == "progress_overflow"
    after = L.current()
    assert after["progress"] == before["progress"] and after["progress"]["value"]["capReached"] is True
    assert after["deadlinePhase"]["value"]["anchorAt"] == 1000.0 + G and after["progress"]["proof"] == "complete"
    assert audit_counters(L.dsn) == []


def test_transport_facts_change_nothing(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    before = L.current()
    for fact in ("bridge_ack", "launched", "completed"):
        L.aj.transport(L.p.root, L.ck, fact, f"{fact}-ref")
    after = L.current()
    assert after["deadlinePhase"] == before["deadlinePhase"] and after["ack"] == before["ack"] and after["status"] == before["status"]


def test_dispatch_is_correlated_with_the_authoritative_receipt(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    for bad in (L.ekey(runId="run-2", packageRef=L.package(contentDigest=H("0"))),          # digest is not the receipt's
                L.ekey(runId="run-3", packageRef=L.package(prereqDigest=H("1"))),           # pin is not the receipt's
                L.ekey(runId="run-4", attemptId="att-other")):
        with pytest.raises(A.Refused) as e:
            L.aj.dispatch(L.p.root, L.ck, bad)
        assert e.value.code in ("receipt_mismatch", "not_dispatchable")
    with pytest.raises(A.Malformed):
        L.aj.dispatch(L.p.root, "another-claim", L.ekey(runId="run-5"))
    assert L.intake(L.act("worker_ack", 1, key_=L.ekey(claimKey="another-claim"))).outcome == "malformed"


def test_independent_facts_can_never_make_an_effective_act(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    facts_b = independent_facts(L.dsn, L.p.root, L.p.target)
    forged = A.PlanFacts("A", facts_b.node, revision=facts_b.revision, basis=facts_b.basis)
    with pytest.raises(ValueError):
        L.intake(L.act("worker_ack", 1), route_b_facts=forged)
    assert "worker_ack" not in L.records()


def test_journal_outage_returns_the_plan_leaf_with_evidence_unknown(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    L.intake(L.act("worker_ack", 1))
    raw(L.dsn, "DROP SCHEMA supervisor_journal CASCADE")
    r = L.read()
    assert r["evidence"] == "unavailable" and r["current"] == "unknown" and r["identity"] is None
    assert r["leaf"]["work_status"] == "in_progress" and r["leaf"]["attempt_id"] == "att-1"


def test_c_b_refuses_a_selector_that_disagrees_with_the_resolved_identity(harness, setup, client, e2c):
    """msg 1188: facts of ANOTHER node in the same plan never answer for this key, in either form."""
    L = Live(harness, setup, client, e2c)
    L.intake(L.act("worker_ack", 1))
    ak = {k: L.key[k] for k in A.ATTEMPT_FIELDS}
    for req in ({"executionKey": L.key}, {"attemptKey": ak}, {"attemptKey": ak, "runId": "run-1"}):
        with pytest.raises(A.Refused) as e:
            read_cb(L.dsn, L.p.root, L.ck, L.p.pred, req, now=100.0)
        assert (e.value.code, e.value.detail) == ("selector_mismatch", ["nodeId"])
    L.finish()
    rk = {**ak, "artifactRef": "sha-art", "lead": "lead-1", "leadSession": None}
    L.aj.request_review(L.p.root, L.ck, rk)
    with pytest.raises(A.Refused) as e:
        read_cb(L.dsn, L.p.root, L.ck, L.p.pred, {"attemptKey": ak, "artifactRef": "sha-art"}, now=100.0)
    assert e.value.code == "selector_mismatch"
    assert L.read()["identity"]["executionKey"] == L.key          # the matching selector still answers


def test_run_id_is_unique_across_streams(harness, setup, client, e2c):
    """030 §8 runId uniqueness across streams WHILE THE REGISTRY ROW IS RETAINED (e2c_runs primary key,
    inserted in the dispatch's route A transaction; a refusal appends nothing). The row is deleted
    with its stream on compaction/eviction, so lifetime uniqueness across retention is NOT proved."""
    L = Live(harness, setup, client, e2c)
    p2, ck2 = make_plan(harness, setup), key()
    r2 = ok(client.claim(p2.root, ck2, "att-1", WS, "supervisor-e2c"))["receipt"]
    L.aj.append(p2.root, ck2, "claim_intent", {"attemptId": "att-1", "executorRef": WS, "actor": "supervisor-e2c"})
    pkg = canonical({"kind": "supplied.v1", "suppliedSha256": H("a"), "instructions": {"system": H("b"), "fast": H("c"), "deep": H("d")},
                     "contentRevision": r2["contentRevision"], "contentDigest": r2["contentDigest"].lower(), "prereqDigest": r2["prereqDigest"]})
    k2 = {"rootId": p2.root, "nodeId": p2.target, "attemptId": "att-1", "attemptEpoch": r2["attemptEpoch"], "claimKey": ck2,
          "runId": "run-1", "packageRef": pkg, "workerSession": WS}
    before = len(L.records())
    with pytest.raises(A.Refused) as e:
        L.aj.dispatch(p2.root, ck2, k2)                             # "run-1" is L's run
    assert e.value.code == "run_id_reused"
    with psycopg.connect(L.dsn) as c:
        assert c.execute("SELECT count(*) FROM supervisor_journal.records WHERE root = %s AND kind = 'dispatch_intent'",
                         (p2.root,)).fetchone()[0] == 0
    assert len(L.records()) == before and audit_counters(L.dsn) == []
    assert L.aj.dispatch(p2.root, ck2, {**k2, "runId": "run-2"})     # a fresh runId is accepted; the writer is still usable
    with pytest.raises(psycopg.errors.RaiseException):
        raw(L.dsn, "UPDATE supervisor_journal.e2c_runs SET run_id = 'x'")


def test_unrelated_stream_compaction_changes_no_field(harness, setup, client, e2c):
    L = Live(harness, setup, client, e2c)
    L.intake(L.act("worker_ack", 1))
    before = L.current()
    L.aj.append(L.p.root, "other-claim", "claim_intent", {"attemptId": "x"})
    L.aj.append(L.p.root, "other-claim", "operator_resolution", {"decision": "abandoned_no_effects", "reconciliationRef": "r"})
    L.aj.resolve(L.p.root, "other-claim")
    assert compact(L.dsn, 10**9)["compacted"] == 1
    assert L.current() == before
