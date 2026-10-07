"""E2b-a live (plan 028 §9 checks 3, 5, 9, 10, 11 and the §2 rule): crash points with a killed child
process, append vs effect fencing against the real API, the SHARED COHERENT-READ ADAPTER EXPERIMENT
(not a production proof path), the bounded read-only recovery scan and scan(now). Disposable
database only; no worker, provider, wake or send."""

import json
import uuid
from pathlib import Path

import psycopg
import pytest

from e1.coherent import prove_finish, read_coherent
from e1.durable import DurableJournal, audit_counters, operator_takeover
from e1.evidence import Facts, ModelJournal, classify
from e1.recovery import OperatorQueue, ReadBounds, classify_stream, read_connection, read_stream_coherent, scan
from e1.supervisor import FakeWorker, Outcome, Supervisor, echo
from e1.wire import SupervisorClient
from e2b_support import W, Child, backend_gone, jdb, journal_digest, open_when_free  # noqa: F401 (fixture)
from helpers import key, ok
from test_e2a_live import journal_for
from test_supervisor_faults import FaultyClient
from test_supervisor_live import db_snapshot, make_plan, node_state, rev

REPO = Path(__file__).resolve().parents[4]


def coherent_label(dsn, root, ck, rb=ReadBounds()):
    with read_connection(dsn) as c:
        sf = read_stream_coherent(c, root, ck, rb)
    return classify_stream(sf, W), sf


# --------------------------------------------------------------------------- check 3: intent before effect, killed child

CLAIM = {"root": "r", "claimKey": "k", "attemptId": "att", "executorRef": None, "actor": "supervisor-e1"}
STEPS = [
    ("append", "claim_intent", CLAIM), ("effect", "claim", None),
    ("append", "claimed", {"outcome": "claimed", "nodeId": None, "attemptId": "att", "attemptEpoch": 1}),
    ("append", "launch_intent", {"launchKey": "launch:k:1", "runId": "run-1"}), ("effect", "dispatch", None),
    ("append", "launched", {"runId": "run-1", "modelOnly": True}),
    ("append", "exited", {"runId": "run-1", "modelOnly": True, "exitCode": 0}),
    ("append", "result_captured", {"runId": "run-1", "artifactRef": "sha-1", "candidate": True}),
    ("append", "finish_intent", {"held": {"operationKey": "supervisor:k:finish"}}), ("effect", "transition", None),
    ("append", "finish_outcome", {"outcome": "applied"}),
]
LABEL_AFTER = {"claim_intent": "C1:not_observed", "claimed": "C2", "launch_intent": "C3", "launched": "C4", "exited": "C5",
               "result_captured": "C6", "finish_intent": "C7:proof_missing"}
CRASHES = [(i, "hold") for i, s in enumerate(STEPS) if s[0] == "append" and i > 0] + \
          [(i, "after_effect") for i, s in enumerate(STEPS) if s[0] == "effect"]


@pytest.mark.parametrize("crash_at,mode", CRASHES)
def test_c3_a_killed_child_leaves_committed_intents_and_no_outcome(jdb, crash_at, mode):
    dsn = jdb.make()
    root, ck = str(uuid.uuid4()), "k"
    committed: list[str] = []
    ch = Child()
    try:
        pid = ch.send(op="open", dsn=dsn, writer=W)["opened"]
        for i, (op, name, data) in enumerate(STEPS):
            if i == crash_at and mode == "hold":
                assert ch.send(op="hold", root=root, key=ck, kind=name, data=data) == {"holding": pid}
                break
            if op == "append":
                assert ch.send(op="append", root=root, key=ck, kind=name, data=data) == {"ok": len(committed) + 1}
                committed.append(name)
            else:
                # An effect marker is emitted only after its intent was CONFIRMED; the intent is visible
                # to an independent connection at that moment.
                assert committed[-1].endswith("_intent")
                assert ch.send(op="effect", name=name) == {"effect": name}
                with psycopg.connect(dsn) as c:
                    assert c.execute("SELECT kind FROM supervisor_journal.records WHERE root = %s ORDER BY seq DESC LIMIT 1",
                                     (root,)).fetchone()[0] == committed[-1]
                if i == crash_at:
                    assert ch.send(op="idle") == {"idle": pid}
                    break
    finally:
        ch.kill()
    assert backend_gone(dsn, pid)
    with psycopg.connect(dsn) as c:
        kinds = [r[0] for r in c.execute("SELECT kind FROM supervisor_journal.records WHERE root = %s ORDER BY seq", (root,))]
    assert kinds == committed and audit_counters(dsn) == []               # the held outcome is absent
    # The E2a classifier gives the same answer from the durable prefix as from the model prefix.
    label, sf = coherent_label(dsn, root, ck)
    mj = ModelJournal(W)
    for k, (op, name, data) in enumerate(s for s in STEPS if s[0] == "append"):
        if k == len(committed):
            break
        mj.append(root, ck, name, data)
    assert label.label == classify(mj.prefix(root, ck, len(committed)), Facts(reader=W, claim_found=False)).label
    assert label.label == LABEL_AFTER[committed[-1]] and label.automatic == ()
    open_when_free(dsn).close()                                           # a restarted writer can take over its fence


# --------------------------------------------------------------------------- check 5: effect fencing is NOT claimed

class TakeoverBeforeEffect(SupervisorClient):
    """The operator takes over AFTER the finish intent was confirmed and BEFORE the old process sends
    its effect. `then` optionally lets another actor change the node first."""

    def __init__(self, base, dsn, then=None):
        super().__init__(base)
        self.dsn, self.then = dsn, then

    def transition(self, node, payload):
        if payload["to"] == "done":
            operator_takeover(self.dsn, W, "recon-takeover", 1.0)
            if self.then:
                self.then(node, payload)
        return super().transition(node, payload)


def run_with_takeover(harness, setup, jdb, then=None):
    dsn = jdb.make()
    p = make_plan(harness, setup)
    ck = key()
    old = jdb.writer()
    r = Supervisor(TakeoverBeforeEffect(harness.base_url, dsn, then), FakeWorker(lambda pkg, run: echo(pkg, run, artifact_ref="sha-t")),
                   journal=old.hook(p.root, ck)).run(p.root, ck, "att", "e1:t")
    return dsn, p, ck, old, r


def test_c5_an_old_writers_effect_after_takeover_is_decided_by_planstore_and_reconciled(harness, setup, jdb):
    dsn, p, ck, old, r = run_with_takeover(harness, setup, jdb)
    assert r.outcome is Outcome.NEEDS_OPERATOR and r.journal_errors == ["finish_outcome:JournalRefused"]
    assert node_state(setup, p.root, p.target)["work"] == "done"         # the late effect still landed (PlanStore CAS)
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT kind FROM supervisor_journal.records WHERE root = %s ORDER BY seq DESC LIMIT 1",
                         (p.root,)).fetchone()[0] == "finish_intent"     # the outcome record was refused
    with pytest.raises(Exception):
        DurableJournal(dsn, W).open()                                     # old session still live: takeover cannot prove quiescence
    old_pid = old.backend_pid()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SELECT pg_terminate_backend(%s)", (old_pid,))         # an explicit operator act
    assert backend_gone(dsn, old_pid)
    new = open_when_free(dsn)
    assert new.epoch == 2
    c7, _ = coherent_label(dsn, p.root, ck)
    assert c7.label == "C7:proved" and c7.automatic == ()                 # unknown to the journal; reconciled by proof
    new.close()


def test_c5_a_late_effect_after_an_intervening_change_is_refused_by_planstore(harness, setup, jdb):
    def other_actor_finishes(node, held):
        ok(setup.transition(node, {"to": "done", "attemptId": held["attemptId"], "attemptEpoch": held["attemptEpoch"],
                                   "artifactRef": "sha-other", "operationKey": key(),
                                   "expectedStateRevision": held["expectedStateRevision"], "actor": "e1-other",
                                   "executorRef": held.get("executorRef")}))
    dsn, p, ck, old, r = run_with_takeover(harness, setup, jdb, then=other_actor_finishes)
    assert r.reason.startswith("finish_rejected:409") and r.journal_errors == ["finish_outcome:JournalRefused"]
    assert node_state(setup, p.root, p.target)["artifactRef"] == "sha-other"
    old.close()
    c7, _ = coherent_label(dsn, p.root, ck)
    assert c7.label == "C7:intervening" and c7.automatic == ()


# --------------------------------------------------------------------------- check 9: shared coherent-read adapter experiment

def durable_run(harness, setup, jdb, client=None, artifact="sha-c7"):
    p = make_plan(harness, setup)
    ck = key()
    dj = jdb.writer()
    s = Supervisor(client or FaultyClient(harness.base_url, lose_finish_reply=True),
                   FakeWorker(lambda pkg, run: echo(pkg, run, artifact_ref=artifact)), journal=dj.hook(p.root, ck))
    r = s.run(p.root, ck, "att", "e1:c7")
    dj.close()
    return p, ck, s, r


def both(harness, jdb, p, ck, r, rb=ReadBounds()):
    """The durable adapter's proof vs the E2a fixture proof on the same database state."""
    label, sf = coherent_label(jdb.dsn, p.root, ck, rb)
    e2a = prove_finish(read_coherent(harness.psql, p.target), r.held_finish, r.package)
    return label.label, e2a


def test_c9_proved_and_still_proved_after_an_unchanged_replay(harness, setup, jdb):
    jdb.make()
    p, ck, s, r = durable_run(harness, setup, jdb)
    assert r.reason == "uncertain:finish_reply"                           # the finish landed; its reply was lost
    assert both(harness, jdb, p, ck, r) == ("C7:proved", "proved")
    s._client.f["lose_finish_reply"] = False                              # this time the reply arrives
    resp, why = s.replay_held_finish(r)
    assert why == "replayed" and ok(resp)["outcome"] == "unchanged"
    assert both(harness, jdb, p, ck, r) == ("C7:proved", "proved")


def test_c9_superseded_by_a_decision_and_by_a_structural_bump(harness, setup, jdb):
    jdb.make()
    p, ck, s, r = durable_run(harness, setup, jdb)
    ok(setup.decide(p.target, {"decision": "accepted", "reviewedContentRevision": 1, "reviewedArtifactRef": "sha-c7",
                               "reviewedAttemptEpoch": 1, "evidenceRef": "rev", "operationKey": key(),
                               "expectedStateRevision": rev(setup, p.root, p.target), "actor": "e1-verifier"}))
    assert both(harness, jdb, p, ck, r) == ("C7:superseded", "superseded")
    p2, ck2, s2, r2 = durable_run(harness, setup, jdb)
    sib = str(uuid.uuid4())
    ok(setup.add_child(p2.root, sib, "sibling", 5, key(), rev(setup, p2.root, p2.root)))
    ok(setup.add_dependency(p2.target, sib, key(), rev(setup, p2.root, p2.target), gate="completed"))
    assert both(harness, jdb, p2, ck2, r2) == ("C7:superseded", "superseded")


def test_c9_unconfirmed_then_intervening(harness, setup, jdb):
    jdb.make()
    p, ck, s, r = durable_run(harness, setup, jdb, client=FaultyClient(harness.base_url, fail_finish_before_send=True), artifact="sha-u")
    assert both(harness, jdb, p, ck, r) == ("C7:unconfirmed", "unconfirmed")
    held = r.held_finish
    ok(setup.transition(p.target, {"to": "done", "attemptId": held["attemptId"], "attemptEpoch": held["attemptEpoch"],
                                   "artifactRef": "sha-u", "operationKey": key(), "expectedStateRevision": held["expectedStateRevision"],
                                   "actor": "e1-other", "executorRef": held.get("executorRef")}))
    assert both(harness, jdb, p, ck, r) == ("C7:intervening", "intervening")


@pytest.mark.parametrize("rb", [ReadBounds(events_max=1), ReadBounds(events_bytes=64), ReadBounds(events_page=1, events_max=1)])
def test_c9_an_incomplete_bounded_read_is_proof_missing(harness, setup, jdb, rb):
    jdb.make()
    p, ck, s, r = durable_run(harness, setup, jdb)
    label, e2a = both(harness, jdb, p, ck, r, rb)
    assert (label, e2a) == ("C7:proof_missing", "proved")                # E2a's unbounded read proves it; the bounded one never guesses
    assert both(harness, jdb, p, ck, r, ReadBounds(events_page=1)) == ("C7:proved", "proved")   # paging alone is fine


# --------------------------------------------------------------------------- check 10: bounded recovery scan

@pytest.mark.parametrize("n,label", [(1, "C1:committed_replay"), (2, "C2"), (3, "C3"), (6, "C6"), (7, "C7:proved"), (8, "C8")])
def test_c10_seeded_crash_prefixes_reproduce_the_e2a_classifications_without_writes(harness, setup, jdb, n, label):
    dsn = jdb.make()
    p = make_plan(harness, setup)
    ck = key()
    mj = ModelJournal(W)
    Supervisor(SupervisorClient(harness.base_url), FakeWorker(lambda pkg, run: echo(pkg, run)),
               journal=journal_for(mj, p.root, ck)).run(p.root, ck, "att", None)
    dj = jdb.writer()
    for rec in mj.prefix(p.root, ck, n):                                  # the durable stream holds exactly the prefix
        dj.append(p.root, ck, rec.kind, rec.payload)
    dj.close()
    before = (db_snapshot(harness, p.root), journal_digest(dsn))
    res = scan(dsn, W, 0.0, queue=OperatorQueue(16, 1 << 16))
    assert res.results[(p.root, ck)].label == label and res.results[(p.root, ck)].automatic == ()
    assert (db_snapshot(harness, p.root), journal_digest(dsn)) == before  # no PlanStore write, nothing persisted


def test_c10_paging_reports_foreign_streams_and_reads_page_by_page(harness, jdb):
    dsn = jdb.make()
    a, b = jdb.writer(), jdb.writer("supervisor-e1#B")
    for i in range(5):
        a.append(str(uuid.uuid4()), f"k{i}", "claim_intent", {"n": i})
    b.append(str(uuid.uuid4()), "kb", "claim_intent", {"n": 0})
    before = journal_digest(dsn)
    res = scan(dsn, W, 0.0, rb=ReadBounds(streams_page=2), queue=OperatorQueue(16, 1 << 16))
    assert res.pages == 4 and len(res.results) == 6 and res.next_after is None   # 3 full pages + the empty one that ends the pass
    assert sorted(c.label for c in res.results.values()) == ["C1:not_observed"] * 5 + ["foreign"]
    assert journal_digest(dsn) == before


def test_c10_one_scan_call_is_bounded_and_continues_with_a_cursor(harness, jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    for i in range(5):
        dj.append(str(uuid.uuid4()), f"k{i}", "claim_intent", {"n": i})
    q = OperatorQueue(16, 1 << 16)
    seen, after, calls = set(), ("", ""), 0
    while True:
        res = scan(dsn, W, 0.0, rb=ReadBounds(streams_page=2, max_streams=2), queue=q, after=after)
        calls += 1
        assert len(res.results) <= 2                                      # output bounded per call
        seen |= set(res.results)
        if res.next_after is None:
            break
        after = res.next_after
    assert calls == 3 and len(seen) == 5


# --------------------------------------------------------------------------- check 11: scan(now)

def reviewed_stream(harness, setup, jdb, lead="lead-1", at=100.0, extra_reviews=()):
    p = make_plan(harness, setup)
    ck = key()
    dj = jdb.writer(now=at)
    r = Supervisor(SupervisorClient(harness.base_url), FakeWorker(lambda pkg, run: echo(pkg, run, artifact_ref="sha-r")),
                   journal=dj.hook(p.root, ck)).run(p.root, ck, "att", None)
    review = [p.root, p.target, r.held_finish["attemptId"], r.held_finish["attemptEpoch"], "sha-r"]
    for other in extra_reviews:
        dj.append(p.root, ck, "review_requested", {"review": [p.root, p.target] + list(other), "lead": lead})
    dj.append(p.root, ck, "review_requested", {"review": review, "lead": lead})
    dj.close()
    return p, ck, tuple(review)


def test_c11_scan_returns_intended_notifications_only_and_writes_nothing(harness, setup, jdb):
    dsn = jdb.make()
    p, ck, review = reviewed_stream(harness, setup, jdb)
    q = OperatorQueue(16, 1 << 16)
    before = (db_snapshot(harness, p.root), journal_digest(dsn))
    early = scan(dsn, W, 105.0, queue=q, ack_window=10.0)
    assert early.notifications == [] and early.results[(p.root, ck)].label == "C10"
    due = scan(dsn, W, 110.0, queue=q, ack_window=10.0)
    assert due.notifications == [("notify_same_lead", review, "lead-1")]   # anchored at the record's own `at` (100)
    assert (db_snapshot(harness, p.root), journal_digest(dsn)) == before  # nothing sent, recorded or persisted
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM supervisor_journal.records WHERE kind LIKE 'notify%%'").fetchone()[0] == 0
    assert json.loads(q.entries[(p.root, ck)])["label"] == "C10"


def test_c11_a_decision_the_snapshot_cannot_verify_goes_to_the_operator_and_the_entry_is_refreshed(harness, setup, jdb):
    dsn = jdb.make()
    p, ck, review = reviewed_stream(harness, setup, jdb)
    q = OperatorQueue(16, 1 << 16)
    scan(dsn, W, 105.0, queue=q, ack_window=10.0)
    assert json.loads(q.entries[(p.root, ck)]) == {"label": "C10", "allowed": ["escalate_same_lead"]}
    ok(setup.decide(p.target, {"decision": "accepted", "reviewedContentRevision": 1, "reviewedArtifactRef": "sha-r",
                               "reviewedAttemptEpoch": 1, "evidenceRef": "rev", "operationKey": key(),
                               "expectedStateRevision": rev(setup, p.root, p.target), "actor": "e1-verifier"}))
    res = scan(dsn, W, 200.0, queue=q, ack_window=10.0)
    c = res.results[(p.root, ck)]
    assert c.label == "review:decided_unverified" and c.allowed == ("operator_confirm_decision",)   # pins need the whole graph
    assert ("operator_classification", review, "") in res.notifications
    assert json.loads(q.entries[(p.root, ck)]) == {"label": "review:decided_unverified", "allowed": ["operator_confirm_decision"]}


def test_c11_reopen_makes_the_review_moot_and_cleans_the_queue(harness, setup, jdb):
    dsn = jdb.make()
    p, ck, review = reviewed_stream(harness, setup, jdb)
    q = OperatorQueue(16, 1 << 16)
    scan(dsn, W, 105.0, queue=q, ack_window=10.0)
    assert (p.root, ck) in q.entries
    ok(setup.transition(p.target, {"to": "in_progress", "attemptId": "att-2", "operationKey": key(),
                                   "expectedStateRevision": rev(setup, p.root, p.target), "actor": "e1-setup"}))
    res = scan(dsn, W, 200.0, queue=q, ack_window=10.0)
    assert res.results[(p.root, ck)].label == "review:moot" and ("moot", review, "") in res.notifications
    assert (p.root, ck) not in q.entries and res.queue["queued"] == 0


def test_c11_only_the_exact_current_review_key_is_live(harness, setup, jdb):
    dsn = jdb.make()
    old = ("att", 1, "sha-old")                                           # same attempt, a different artifact
    p, ck, review = reviewed_stream(harness, setup, jdb, extra_reviews=[old])
    res = scan(dsn, W, 110.0, queue=OperatorQueue(16, 1 << 16), ack_window=10.0)
    assert ("moot", (p.root, p.target) + old, "") in res.notifications
    assert ("notify_same_lead", review, "lead-1") in res.notifications
    assert not any(n[1] == (p.root, p.target) + old and n[0] == "notify_same_lead" for n in res.notifications)


def test_c11_queue_overflow_is_explicit_bounded_and_never_drops_evidence(harness, jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    keys = [(str(uuid.uuid4()), f"k{i}") for i in range(4)]
    dj.append(keys[0][0], keys[0][1], "claim_intent", {"n": 0})
    q = OperatorQueue(max_entries=1, max_bytes=1 << 16, overflow_keys_max=2)
    first = scan(dsn, W, 0.0, queue=q)
    assert first.queue == {"queued": 1, "overflow_count": 0, "overflow_keys": [], "alert": False, "removed_inactive": 0}
    for root, ck in keys[1:]:
        dj.append(root, ck, "claim_intent", {"n": 1})
    dj.append(keys[0][0], keys[0][1], "claimed", {"outcome": "claimed"})  # the queued stream advances C1 -> C2
    before = journal_digest(dsn)
    second = scan(dsn, W, 0.0, queue=q)
    assert second.queue["queued"] == 1 and second.queue["overflow_count"] == 3 and second.queue["alert"] is True
    assert len(second.queue["overflow_keys"]) == 2                        # retained keys are bounded; the count is exact
    assert json.loads(q.entries[keys[0]]) == {"label": "C2", "allowed": ["release"]}   # refreshed in place, never stale
    assert journal_digest(dsn) == before                                  # overflowed streams' evidence untouched
    assert {k: c.label for k, c in second.results.items() if k != keys[0]} == {k: "C1:not_observed" for k in keys[1:]}
    small = OperatorQueue(max_entries=16, max_bytes=10)
    assert scan(dsn, W, 0.0, queue=small).queue["overflow_count"] == 4    # the byte bound Y holds too


# --------------------------------------------------------------------------- §2: task derivations never read the journal

def test_task_state_derivations_do_not_depend_on_the_journal(harness, setup, jdb):
    """Deterministic, scoped proof: the public plan-contract reads used here (plan view, node events,
    claim lookup) return identical results with the journal schema present, filled with adversarial
    rows, and renamed away. Scope: these endpoints on this build; plus no Api source mentions it."""
    dsn = jdb.make()
    p = make_plan(harness, setup)
    ck = key()
    Supervisor(SupervisorClient(harness.base_url), FakeWorker(lambda pkg, run: echo(pkg, run)),
               journal=jdb.writer().hook(p.root, ck)).run(p.root, ck, "att", None)
    reads = lambda: (ok(setup.plan(p.root)), ok(setup.events(p.target)), SupervisorClient(harness.base_url).get_claim(p.root, ck).raw)
    with_journal = reads()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET session_replication_role = replica")
        c.execute("UPDATE supervisor_journal.records SET data = '{\"outcome\":\"rejected:forged\"}'")
    assert reads() == with_journal
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("ALTER SCHEMA supervisor_journal RENAME TO sj_hidden")
    try:
        assert reads() == with_journal
    finally:
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("ALTER SCHEMA sj_hidden RENAME TO supervisor_journal")
    api_sources = [f for f in (REPO / "context-store").rglob("*.cs") if "supervisor_journal" in f.read_text(encoding="utf-8", errors="ignore")]
    assert api_sources == []


# --------------------------------------------------------------------------- msg 954 regressions (bounds, queue cleanup)

@pytest.mark.parametrize("field", ["streams_page", "max_streams", "events_max", "events_bytes", "events_page"])
@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "2", None])
def test_read_bounds_reject_non_positive_or_non_int_values(field, bad):
    with pytest.raises(ValueError):
        ReadBounds(**{field: bad})


@pytest.mark.parametrize("kw", [dict(max_entries=0, max_bytes=10), dict(max_entries=1, max_bytes=0),
                                dict(max_entries=True, max_bytes=10), dict(max_entries=1, max_bytes=10, overflow_keys_max=-1),
                                dict(max_entries=1.0, max_bytes=10)])
def test_operator_queue_rejects_bad_bounds(kw):
    with pytest.raises(ValueError):
        OperatorQueue(**kw)


@pytest.mark.parametrize("kw", [dict(now=-1.0), dict(now=float("nan")), dict(now=True), dict(ack_window=0),
                                dict(progress_window=float("inf")), dict(after=("a",)), dict(after=["", ""])])
def test_scan_rejects_bad_inputs_before_touching_the_database(kw):
    args = dict(now=0.0)
    args.update(kw)
    now = args.pop("now")
    with pytest.raises(ValueError):
        scan("host=127.0.0.1 port=1 dbname=never", W, now, queue=OperatorQueue(1, 1 << 16), **args)   # no DB is reachable


def test_queue_cleanup_and_count_work_across_bounded_scan_calls(harness, jdb):
    """msg 954 §3: with more active streams than one call handles, a queued stream that is resolved
    leaves the queue on the NEXT call wherever the cursor is, and `queued` is counted after cleanup."""
    dsn = jdb.make()
    dj = jdb.writer()
    keys = sorted((str(uuid.uuid4()), "k") for _ in range(5))
    for root, ck in keys:
        dj.append(root, ck, "claim_intent", {"n": 1})
    q = OperatorQueue(16, 1 << 20)
    rb = ReadBounds(streams_page=2, max_streams=2)
    after = ("", "")
    while True:
        res = scan(dsn, W, 0.0, rb=rb, queue=q, after=after)
        if res.next_after is None:
            break
        after = res.next_after
    assert set(q.entries) == set(keys)
    first = keys[0]
    dj.append(first[0], first[1], "claimed", {"outcome": "claimed"})
    dj.append(first[0], first[1], "operator_resolution", {"decision": "abandoned_no_effects", "reason": "r",
                                                           "reconciliationRef": "recon"})
    dj.resolve(first[0], first[1])
    res = scan(dsn, W, 0.0, rb=rb, queue=q, after=keys[2])               # a call that never visits keys[0]
    assert first not in res.results and first not in q.entries
    assert res.queue["removed_inactive"] == 1 and res.queue["queued"] == len(keys) - 1


def test_a_resolution_after_the_reads_is_never_left_queued_by_that_scan(harness, jdb):
    """msg 996: a stream read (and classified) by this call but resolved before the queue update
    must not be (re)inserted: refresh first, reconcile after, count last."""
    dsn = jdb.make()
    dj = jdb.writer()
    root, ck = str(uuid.uuid4()), "race"
    dj.append(root, ck, "claim_intent", {"n": 1})
    q = OperatorQueue(4, 1 << 20)
    calls = []

    def resolve_now():
        calls.append(True)
        dj.append(root, ck, "claimed", {"outcome": "claimed"})
        dj.append(root, ck, "operator_resolution", {"decision": "abandoned_no_effects", "reason": "r", "reconciliationRef": "recon"})
        dj.resolve(root, ck)
    res = scan(dsn, W, 0.0, rb=ReadBounds(max_streams=1), queue=q, _before_queue=resolve_now)
    assert calls == [True] and res.results[(root, ck)].label == "C1:not_observed"      # the stale read exists
    assert (root, ck) not in q.entries and res.queue["queued"] == 0 and res.queue["removed_inactive"] == 1
