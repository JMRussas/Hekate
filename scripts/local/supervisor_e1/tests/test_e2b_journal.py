"""E2b-a live (plan 028 §9 checks 1, 2, 4, 6, 7, 8): the durable journal adapter on the FIXTURE-ONLY
supervisor_journal tables of a NEW disposable database. Test-only; no production schema, service,
worker, provider or wake mechanism."""

import json
import threading
import time
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from e1.durable import (BUDGET_RECORD_MAX, CommitUnknown, DurableJournal, audit_counters, compact, durable_meta_bytes,
                        confirm, operator_takeover, read_stream_records, record_hash)
from e1.evidence import RECORD_MAX_BYTES, Bounds, JournalRefused, encoded_size
from e1.recovery import ReadBounds, read_connection, read_stream_coherent, classify_stream
from e1.supervisor import FakeWorker, Supervisor, echo
from e2b_support import W, Child, backend_gone, jdb, journal_digest, open_when_free, raw  # noqa: F401 (fixture)
from helpers import key
from test_e2a_live import TimelineClient
from test_supervisor_live import make_plan

ROOT = str(uuid.uuid4())
RES = {"decision": "abandoned_no_effects", "reason": "fixture", "reconciliationRef": "recon-1"}


def records(dsn, root=ROOT, ck="k1"):
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return c.execute("SELECT seq, kind, data FROM supervisor_journal.records WHERE root = %s AND claim_key = %s ORDER BY seq",
                         (root, ck)).fetchall()


def resolved_stream(dj, ck, root=ROOT):
    dj.append(root, ck, "claim_intent", {"n": 1})
    dj.append(root, ck, "claimed", {"outcome": "claimed"})
    dj.append(root, ck, "operator_resolution", RES)
    dj.resolve(root, ck)


# --------------------------------------------------------------------------- check 1: atomicity

def test_c1_connection_closed_before_commit_leaves_no_record_and_unchanged_counters(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    before = journal_digest(dsn)
    dj.fault = lambda stage, d: d.conn.close() if stage == "before_commit" else None
    rid = str(uuid.uuid4())
    with pytest.raises(CommitUnknown):                                   # the client cannot tell: unknown
        dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"}, record_id=rid)
    assert confirm(dsn, W, dj.unknown).status == "not_observed"           # not visible; NOT proof of absence
    assert journal_digest(dsn) == before and audit_counters(dsn) == []
    with pytest.raises(JournalRefused) as e:                              # no silent resume
        dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    assert e.value.code == "fence_lost"
    dj.fault = None
    dj.reacquire()
    u = dj.unknown
    rec = dj.append(u["root"], u["claim_key"], u["kind"], u["data"], record_id=u["record_id"], expected_seq=u["expected_seq"])
    assert rec.seq == 2 and [r["kind"] for r in records(dsn)] == ["claim_intent", "claimed"] and audit_counters(dsn) == []


def test_c1_a_child_killed_inside_the_append_transaction_leaves_no_record(jdb):
    dsn = jdb.make()
    ch = Child()
    try:
        pid = ch.send(op="open", dsn=dsn, writer=W)["opened"]
        assert ch.send(op="append", root=ROOT, key="k1", kind="claim_intent", data={"n": 1}) == {"ok": 1}
        before = journal_digest(dsn)
        assert ch.send(op="hold", root=ROOT, key="k1", kind="claimed", data={"outcome": "claimed"}) == {"holding": pid}
        with psycopg.connect(dsn) as c:
            assert c.execute("SELECT state FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()[0] == "idle in transaction"
        assert journal_digest(dsn) == before                              # the INSERT is invisible before COMMIT
    finally:
        ch.kill()
    assert backend_gone(dsn, pid)
    assert journal_digest(dsn) == before and audit_counters(dsn) == []
    assert [r["kind"] for r in records(dsn)] == ["claim_intent"]
    open_when_free(dsn).close()                                           # the dead session's lock is gone


# --------------------------------------------------------------------------- check 2: lost COMMIT reply

def test_c2_lost_commit_reply_means_no_effect_then_lookup_and_idempotent_resend(harness, setup, jdb):
    dsn = jdb.make()
    p = make_plan(harness, setup)
    ck = key()
    armed = {"on": True}

    def lose_reply(stage, d):
        if stage == "after_commit" and armed.pop("on", False):
            raise ConnectionResetError("reply lost after a real COMMIT")
    dj = jdb.writer(fault=lose_reply)
    timeline: list[str] = []
    r = Supervisor(TimelineClient(harness.base_url, timeline), FakeWorker(lambda pkg, run: echo(pkg, run)),
                   journal=dj.hook(p.root, ck)).run(p.root, ck, "att", None)
    assert r.reason == "journal_refused:claim_intent" and r.error == "CommitUnknown"
    assert "EFFECT:claim" not in timeline                                # the writer did NOT start the effect
    u = dj.unknown
    c = confirm(dsn, W, u)                                                # authoritative lookup, new connection
    assert c.status == "committed" and c.record.kind == "claim_intent" and c.record.seq == 1
    found = c.record
    before = journal_digest(dsn)
    with pytest.raises(JournalRefused) as e:
        dj.append(p.root, ck, "claim_intent", u["data"])
    assert e.value.code == "fence_lost"                                   # reconnect cannot silently resume
    dj.reacquire()
    again = dj.append(p.root, ck, "claim_intent", u["data"], record_id=u["record_id"], expected_seq=u["expected_seq"])
    assert (again.seq, again.kind) == (1, "claim_intent") and journal_digest(dsn) == before    # identical resend: no-op
    for data, exp in ((dict(u["data"], attemptId="other"), 1), (u["data"], 2)):
        with pytest.raises(JournalRefused) as e:
            dj.append(p.root, ck, "claim_intent", data, record_id=u["record_id"], expected_seq=exp)
        assert e.value.code == "record_id_conflict"                       # different payload or expected seq
    with pytest.raises(JournalRefused) as e:
        dj.append(p.root, "other-key", "claim_intent", u["data"], record_id=u["record_id"], expected_seq=1)
    assert e.value.code == "record_id_conflict"                           # different stream
    assert journal_digest(dsn) == before and audit_counters(dsn) == []    # conflicts never change counters


def lost_reply_writer(jdb):
    armed = {"on": True}

    def lose_reply(stage, d):
        if stage == "after_commit" and armed.pop("on", False):
            raise ConnectionResetError("reply lost after a real COMMIT")
    return jdb.writer(fault=lose_reply)


def test_c2_a_lookup_never_confirms_a_different_identity_under_the_id(jdb):
    dsn = jdb.make()
    dj = lost_reply_writer(jdb)
    with pytest.raises(CommitUnknown):
        dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    u = dj.unknown
    for wrong in (dict(u, data={"n": 2}), dict(u, expected_seq=2), dict(u, kind="launch_intent"), dict(u, claim_key="k2")):
        with pytest.raises(JournalRefused) as e:
            confirm(dsn, W, wrong)
        assert e.value.code == "identity_mismatch"
    with pytest.raises(JournalRefused) as e:
        confirm(dsn, "supervisor-e1#B", u)                                # another writer's identity
    assert e.value.code == "identity_mismatch"
    assert confirm(dsn, W, u).record.seq == 1


def test_c2_a_corrupt_stream_never_confirms_or_answers_a_duplicate(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    dj.fault = lambda stage, d: (_ for _ in ()).throw(ConnectionResetError()) if stage == "after_commit" else None
    with pytest.raises(CommitUnknown):
        dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    u = dj.unknown
    raw(dsn, "UPDATE supervisor_journal.records SET data = '{\"n\":9}' WHERE claim_key = 'k1' AND seq = 1", replica=True)
    with pytest.raises(JournalRefused) as e:
        confirm(dsn, W, u)                                                # the record exists, but the stream is corrupt
    assert e.value.code == "corrupt"
    dj.fault = None
    dj.reacquire()
    with pytest.raises(JournalRefused) as e:
        dj.append(u["root"], u["claim_key"], u["kind"], u["data"], record_id=u["record_id"], expected_seq=u["expected_seq"])
    assert e.value.code == "corrupt"                                      # no idempotent "success" from a corrupt stream


def test_c2_a_duplicate_intent_authorizes_nothing_without_the_fence(jdb):
    dsn = jdb.make()
    dj = lost_reply_writer(jdb)
    with pytest.raises(CommitUnknown):
        dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    u = dj.unknown
    dj.reacquire()
    operator_takeover(dsn, W, "recon", 1.0)
    with pytest.raises(JournalRefused) as e:
        dj.append(ROOT, "k1", "claim_intent", {"n": 1}, record_id=u["record_id"], expected_seq=1)
    assert e.value.code == "fenced:epoch"


def test_c2_identity_ignores_the_clock(jdb):
    dsn = jdb.make()
    dj = jdb.writer(now=5.0)
    rid = str(uuid.uuid4())
    first = dj.append(ROOT, "k1", "claim_intent", {"n": 1}, record_id=rid, expected_seq=1)
    dj.now = 99.0
    assert dj.append(ROOT, "k1", "claim_intent", {"n": 1}, record_id=rid, expected_seq=1) == first


# --------------------------------------------------------------------------- check 4: append fencing

def test_c4_a_second_live_instance_cannot_take_the_writer_lock(jdb):
    jdb.make()
    jdb.writer()
    with pytest.raises(JournalRefused) as e:
        DurableJournal(jdb.dsn, W).open()
    assert e.value.code == "fence_busy"
    jdb.writer("supervisor-e1#B")                                         # a different writer is independent


def test_c4_after_an_operator_takeover_the_old_writers_appends_fail(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    with pytest.raises(JournalRefused):
        operator_takeover(dsn, W, "  ", 1.0)                              # needs a reconciliationRef
    assert operator_takeover(dsn, W, "recon-takeover", 1.0) == 2
    before = journal_digest(dsn)
    with pytest.raises(JournalRefused) as e:
        dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    assert e.value.code == "fenced:epoch" and journal_digest(dsn) == before
    with pytest.raises(JournalRefused) as e:
        dj.verify_fence()
    assert e.value.code == "fenced:epoch"
    with pytest.raises(JournalRefused) as e:                              # the old instance cannot re-open itself
        dj.reacquire()
    assert e.value.code == "fenced:epoch"
    new = open_when_free(dsn)                                             # a NEW instance, after the old session ended
    assert new.epoch == 2
    assert new.append(ROOT, "k1", "claimed", {"outcome": "claimed"}).seq == 2
    new.close()
    with pytest.raises(psycopg.errors.RaiseException):
        raw(dsn, "UPDATE supervisor_journal.takeovers SET reconciliation_ref = 'x'")


def test_c4_an_append_requires_the_lock_on_its_own_live_session(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    dj.conn.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", (0x5E2B, W))
    dj.conn.commit()
    other = DurableJournal(dsn, W).open()                                 # someone else now holds it (same epoch)
    before = journal_digest(dsn)
    with pytest.raises(JournalRefused) as e:
        dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    assert e.value.code == "fenced:lock" and journal_digest(dsn) == before
    other.close()


# --------------------------------------------------------------------------- check 6: global caps across writers

def test_c6_two_writers_near_the_global_cap_admit_exactly_the_admissible_set(jdb):
    dsn = jdb.make(Bounds(global_records=7))                              # one claim_intent reserves 4 (intent + claimed + 2 faults)
    a, b = jdb.writer("writer-A"), jdb.writer("writer-B")
    holding, release = threading.Event(), threading.Event()

    def hold(stage, d):
        if stage == "before_commit":
            holding.set()
            assert release.wait(30)
    a.fault = hold
    out: dict[str, object] = {}

    def run(name, dj, ck):
        try:
            out[name] = dj.append(ROOT, ck, "claim_intent", {"w": name}).seq
        except JournalRefused as e:
            out[name] = e.code
    ta = threading.Thread(target=run, args=("A", a, "ka"))
    ta.start()
    assert holding.wait(30)                                               # A holds the singleton lock, uncommitted
    tb = threading.Thread(target=run, args=("B", b, "kb"))
    tb.start()
    deadline = time.monotonic() + 10
    with psycopg.connect(dsn) as c:
        while c.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (b.backend_pid(),)).fetchone()[0] != "Lock":
            assert time.monotonic() < deadline, "B never waited on the global lock"
            time.sleep(0.05)
    release.set()
    ta.join(30)
    tb.join(30)
    assert out == {"A": 1, "B": "global_cap"}                             # serialized: exactly one admitted, no deadlock
    assert audit_counters(dsn) == []
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM supervisor_journal.streams").fetchone()[0] == 1


def test_c6_parallel_writers_never_exceed_the_global_cap(jdb):
    dsn = jdb.make(Bounds(global_records=4 * 5 + 3))
    writers = [jdb.writer(f"writer-{i}") for i in range(4)]
    results: list[object] = []
    lock = threading.Lock()
    start = threading.Barrier(len(writers))

    def run(dj, i):
        start.wait()
        for n in range(3):
            try:
                dj.append(ROOT, f"k-{i}-{n}", "claim_intent", {"n": n})
                r = "ok"
            except JournalRefused as e:
                r = e.code
            with lock:
                results.append(r)
    ts = [threading.Thread(target=run, args=(dj, i)) for i, dj in enumerate(writers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert results.count("ok") == 5 and results.count("global_cap") == 7 and audit_counters(dsn) == []


def test_c6_a_refused_admission_changes_nothing_and_eviction_takes_only_resolved_data(jdb):
    dsn = jdb.make(Bounds(global_records=8))
    dj = jdb.writer()
    resolved_stream(dj, "old")                                            # resolved: 3 records, reservations released
    dj.append(ROOT, "live", "claim_intent", {"n": 1})                     # 4 more (unresolved)
    before = journal_digest(dsn)
    with pytest.raises(JournalRefused) as e:                              # needs 4; compaction of "old" frees only 2
        dj.append(ROOT, "third", "claim_intent", {"n": 1})
    assert e.value.code == "global_cap" and journal_digest(dsn) == before
    dj.append(ROOT, "live", "claimed", {"outcome": "claimed"})            # a reserved outcome is never refused
    assert audit_counters(dsn) == []


# --------------------------------------------------------------------------- check 7: compaction guard and retention

@pytest.mark.parametrize("sql", [
    "DELETE FROM supervisor_journal.records",
    "UPDATE supervisor_journal.records SET data = data",
    "TRUNCATE supervisor_journal.records",
    "DELETE FROM supervisor_journal.streams",
    "TRUNCATE supervisor_journal.streams CASCADE",
    "TRUNCATE supervisor_journal.global_usage CASCADE",
    "DELETE FROM supervisor_journal.takeovers",
])
def test_c7_raw_writes_outside_compaction_are_refused(jdb, sql):
    dsn = jdb.make()
    dj = jdb.writer()
    resolved_stream(dj, "r1")
    operator_takeover(dsn, W, "recon", 1.0)
    before = journal_digest(dsn)
    with pytest.raises(psycopg.errors.RaiseException):
        raw(dsn, sql)
    assert journal_digest(dsn) == before


def test_c7_the_compaction_flag_cannot_delete_unresolved_or_young_streams(jdb):
    dsn = jdb.make(Bounds(compact_after=100.0, summary_retention=1000.0))
    dj = jdb.writer(now=10.0)
    dj.append(ROOT, "active", "claim_intent", {"n": 1})
    resolved_stream(dj, "young")
    before = journal_digest(dsn)
    for ck in ("active", "young"):
        with pytest.raises(psycopg.errors.RaiseException):
            raw(dsn, "SELECT set_config('supervisor_journal.compaction', 'on', true), set_config('supervisor_journal.now', '50', true)",
                ("DELETE FROM supervisor_journal.records WHERE claim_key = %s", (ck,)))
    with pytest.raises(psycopg.errors.RaiseException):
        raw(dsn, "SELECT set_config('supervisor_journal.compaction', 'evict', true)",
            "DELETE FROM supervisor_journal.records WHERE claim_key = 'active'")
    assert journal_digest(dsn) == before
    assert compact(dsn, 50.0) == {"compacted": 0, "dropped": 0, "retained_corrupt": []} and journal_digest(dsn) == before


def test_c7_compaction_keeps_the_chain_head_then_retention_drops_the_summary(jdb):
    dsn = jdb.make(Bounds(compact_after=100.0, summary_retention=1000.0))
    dj = jdb.writer(now=10.0)
    resolved_stream(dj, "r1")
    dj.append(ROOT, "active", "claim_intent", {"n": 1})
    with psycopg.connect(dsn) as c:
        head = c.execute("SELECT head_hash FROM supervisor_journal.streams WHERE claim_key = 'r1'").fetchone()[0]
    assert compact(dsn, 110.0) == {"compacted": 1, "dropped": 0, "retained_corrupt": []}
    with psycopg.connect(dsn) as c:
        s = c.execute("SELECT summary, head_hash FROM supervisor_journal.summaries WHERE claim_key = 'r1'").fetchone()
        assert s[1] == head and json.loads(s[0])["resolution"]["reconciliationRef"] == "recon-1"
        assert c.execute("SELECT count(*) FROM supervisor_journal.records WHERE claim_key = 'r1'").fetchone()[0] == 0
    assert [r["kind"] for r in records(dsn, ck="active")] == ["claim_intent"]          # unresolved: untouched
    assert audit_counters(dsn) == []
    assert compact(dsn, 1009.0) == {"compacted": 0, "dropped": 0, "retained_corrupt": []}
    assert compact(dsn, 1010.0) == {"compacted": 0, "dropped": 1, "retained_corrupt": []}
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM supervisor_journal.streams WHERE claim_key = 'r1'").fetchone()[0] == 0
    assert audit_counters(dsn) == []


def test_c7_resolution_needs_terminal_evidence(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    before = journal_digest(dsn)
    with pytest.raises(JournalRefused) as e:
        dj.resolve(ROOT, "k1")
    assert e.value.code == "unresolved_outstanding" and journal_digest(dsn) == before
    dj.append(ROOT, "k1", "operator_resolution", {"decision": "retry_permitted_once", "reason": "r", "reconciliationRef": "x"})
    with pytest.raises(JournalRefused):
        dj.resolve(ROOT, "k1")                                            # permission is not a resolution
    dj.append(ROOT, "k1", "operator_resolution", RES)
    dj.resolve(ROOT, "k1")
    with pytest.raises(JournalRefused) as e:
        dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    assert e.value.code == "resolved" and audit_counters(dsn) == []


def test_c7_byte_scope_is_logical_record_plus_durable_metadata(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    rec = dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    with psycopg.connect(dsn) as c:
        row = c.execute("SELECT record_id::text, writer_epoch, intent_digest, prev_hash, record_hash, budget_bytes, at_json "
                        "FROM supervisor_journal.records").fetchone()
        stream_bytes = c.execute("SELECT bytes FROM supervisor_journal.streams").fetchone()[0]
    assert row[5] == rec.size + durable_meta_bytes(row[0], row[1], row[2], row[3], row[4])
    assert stream_bytes == row[5] + 3 * BUDGET_RECORD_MAX                 # 1 terminal + 2 fault slots at the worst case
    assert BUDGET_RECORD_MAX > RECORD_MAX_BYTES


# --------------------------------------------------------------------------- check 8: corruption detection

def scan_label(dsn, ck):
    with read_connection(dsn) as c:
        return classify_stream(read_stream_coherent(c, ROOT, ck, ReadBounds()), W).label


def test_c8_a_tampered_record_marks_the_stream_corrupt(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    raw(dsn, "UPDATE supervisor_journal.records SET data = '{\"outcome\":\"forged\"}' WHERE claim_key = 'k1' AND seq = 2",
        replica=True)                                                     # a credentialed session disabling triggers
    with pytest.raises(JournalRefused) as e:
        dj.append(ROOT, "k1", "launch_intent", {"runId": "r"})
    assert e.value.code == "corrupt"
    assert scan_label(dsn, "k1") == "proof_missing:chain:2"
    with pytest.raises(JournalRefused):
        dj.resolve(ROOT, "k1")


def test_c8_a_seq_gap_marks_the_stream_corrupt(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    for kind, data in (("claim_intent", {"n": 1}), ("claimed", {"outcome": "claimed"}), ("launch_intent", {"runId": "r"})):
        dj.append(ROOT, "k1", kind, data)
    raw(dsn, "DELETE FROM supervisor_journal.records WHERE claim_key = 'k1' AND seq = 2", replica=True)
    assert scan_label(dsn, "k1") == "proof_missing:seq_gap:2"
    with pytest.raises(JournalRefused) as e:
        dj.append(ROOT, "k1", "launched", {"runId": "r"})
    assert e.value.code == "corrupt"


def test_c8_compaction_flag_tampering_is_detected_and_the_corrupt_stream_is_retained(jdb):
    dsn = jdb.make(Bounds(global_records=8, compact_after=1.0))
    dj = jdb.writer()
    resolved_stream(dj, "r1")
    raw(dsn, "SELECT set_config('supervisor_journal.compaction', 'evict', true)",
        "DELETE FROM supervisor_journal.records WHERE claim_key = 'r1' AND seq = 3")       # partial delete via the flag
    with read_connection(dsn) as c:
        sf = read_stream_coherent(c, ROOT, "r1", ReadBounds())
    assert sf.corrupt in ("seq_count", "head")
    out = compact(dsn, 100.0)
    assert (out["compacted"], out["dropped"]) == (0, 0) and out["retained_corrupt"][0][:2] == (ROOT, "r1")   # never compacted
    dj.append(ROOT, "a", "claim_intent", {"n": 1})                       # 4 of 8 used (r1 counts 3)
    with pytest.raises(JournalRefused) as e:
        dj.append(ROOT, "b", "claim_intent", {"n": 1})                   # pressure: r1 is NOT evicted
    assert e.value.code == "global_cap"
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM supervisor_journal.records WHERE claim_key = 'r1'").fetchone()[0] == 2


def test_c8_known_limit_a_credentialed_writer_recomputing_the_chain_is_not_detected(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    with psycopg.connect(dsn) as c:
        rows = c.execute("SELECT record_id::text AS record_id, root, claim_key, seq, kind, writer_id, writer_epoch, at_json, data, "
                         "intent_digest, prev_hash FROM supervisor_journal.records WHERE claim_key = 'k1' ORDER BY seq").fetchall()
    cols = ["record_id", "root", "claim_key", "seq", "kind", "writer_id", "writer_epoch", "at_json", "data", "intent_digest", "prev_hash"]
    prev = "0" * 64
    for r in rows:
        row = dict(zip(cols, r))
        if row["seq"] == 2:
            row["data"] = '{"outcome":"forged"}'
        size = encoded_size(row["kind"], row["writer_id"], row["root"], row["claim_key"], row["seq"], json.loads(row["at_json"]), row["data"])
        row["budget_bytes"] = size + durable_meta_bytes(row["record_id"], row["writer_epoch"], row["intent_digest"], prev, prev)
        row["prev_hash"] = prev
        h = record_hash(prev, row)
        raw(dsn, ("UPDATE supervisor_journal.records SET data = %s, budget_bytes = %s, prev_hash = %s, record_hash = %s "
                  "WHERE record_id = %s", (row["data"], row["budget_bytes"], prev, h, row["record_id"])), replica=True)
        prev = h
    raw(dsn, ("UPDATE supervisor_journal.streams SET head_hash = %s WHERE claim_key = 'k1'", (prev,)))
    with read_connection(dsn) as c:
        sf = read_stream_coherent(c, ROOT, "k1", ReadBounds())
    # KNOWN LIMIT (028 §7): integrity checks detect accidents and well-behaved-path bugs, not a hostile
    # credentialed writer. Tamper resistance would need an external anchor (out of scope).
    assert sf.corrupt is None and sf.records[1].payload == {"outcome": "forged"}


def compacted_stream(dj, dsn, ck):
    resolved_stream(dj, ck)
    assert compact(dsn, 10.0)["compacted"] == 1


@pytest.mark.parametrize("tamper,why", [
    ("UPDATE supervisor_journal.summaries SET summary = '{\"x\":1}' WHERE claim_key = 'c1'", "summary_identity"),
    ("UPDATE supervisor_journal.summaries SET head_hash = repeat('f', 64) WHERE claim_key = 'c1'", "summary_head"),
    ("UPDATE supervisor_journal.streams SET bytes = bytes + 1 WHERE claim_key = 'c1'", "summary_counters"),
    ("DELETE FROM supervisor_journal.summaries WHERE claim_key = 'c1'", "summary_missing"),
    ("UPDATE supervisor_journal.summaries SET summary = repeat('x', 8000) WHERE claim_key = 'c1'", "summary_size"),
])
def test_c7_a_corrupt_summary_is_retained_by_age_retention(jdb, tamper, why):
    dsn = jdb.make(Bounds(compact_after=1.0, summary_retention=50.0))
    dj = jdb.writer()
    compacted_stream(dj, dsn, "c1")
    raw(dsn, tamper, replica=True)
    out = compact(dsn, 100.0)
    assert out["dropped"] == 0 and out["retained_corrupt"] == [(ROOT, "c1", why)]
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM supervisor_journal.streams WHERE claim_key = 'c1'").fetchone()[0] == 1


def test_c7_a_corrupt_summary_is_never_evicted_under_pressure(jdb):
    dsn = jdb.make(Bounds(global_records=5, compact_after=1.0))
    dj = jdb.writer()
    compacted_stream(dj, dsn, "c1")                                      # 1 record of 5
    raw(dsn, "UPDATE supervisor_journal.summaries SET head_hash = repeat('f', 64) WHERE claim_key = 'c1'", replica=True)
    dj.append(ROOT, "a", "claim_intent", {"n": 1})                       # 1 + 4 = 5
    before = journal_digest(dsn)
    with pytest.raises(JournalRefused) as e:
        dj.append(ROOT, "b", "claim_intent", {"n": 1})                   # would need c1 dropped: refused instead
    assert e.value.code == "global_cap" and journal_digest(dsn) == before


def test_c7_a_valid_summary_is_evicted_under_pressure(jdb):
    dsn = jdb.make(Bounds(global_records=5, compact_after=1.0))
    dj = jdb.writer()
    compacted_stream(dj, dsn, "c1")
    dj.append(ROOT, "a", "claim_intent", {"n": 1})
    dj.append(ROOT, "a", "claimed", {"outcome": "claimed"})
    dj.append(ROOT, "a", "operator_resolution", RES)
    dj.resolve(ROOT, "a")                                                # c1 (1) + a (3) = 4
    dj.append(ROOT, "b", "claim_intent", {"n": 1})                       # needs 4: drop c1, compact a
    with psycopg.connect(dsn) as c:
        assert c.execute("SELECT count(*) FROM supervisor_journal.streams WHERE claim_key = 'c1'").fetchone()[0] == 0
    assert audit_counters(dsn) == []


def test_c2_a_compacted_or_dropped_stream_never_confirms_absence(jdb):
    """msg 954: a missing record in a compacted or dropped stream is UNKNOWN, never "not committed"."""
    dsn = jdb.make(Bounds(compact_after=1.0, summary_retention=50.0))
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    dj.fault = lambda stage, d: (_ for _ in ()).throw(ConnectionResetError()) if stage == "after_commit" else None
    with pytest.raises(CommitUnknown):
        dj.append(ROOT, "k1", "operator_resolution", RES)
    u = dj.unknown
    dj.fault = None
    assert confirm(dsn, W, u).status == "committed"
    dj.reacquire()
    dj.resolve(ROOT, "k1")
    assert compact(dsn, 10.0)["compacted"] == 1
    c = confirm(dsn, W, u)
    assert c.status == "compacted" and c.record is None                   # records gone: outcome unknown
    assert compact(dsn, 100.0)["dropped"] == 1
    assert confirm(dsn, W, u).status == "compacted"                       # stream dropped: still unknown, not absent
    ghost = dict(u, record_id=str(uuid.uuid4()))
    assert confirm(dsn, W, ghost).status == "compacted"                   # an id that never existed: also unknown here


def test_c2_a_forgotten_first_claim_intent_is_operator_only_after_retention(jdb):
    """msg 996: a pending FIRST record (expected_seq 1) whose stream later resolved, compacted and was
    dropped must never read as "not committed": resending it would recreate the stream and could
    re-authorize the effect after retention."""
    dsn = jdb.make(Bounds(compact_after=1.0, summary_retention=50.0))
    dj = lost_reply_writer(jdb)
    with pytest.raises(CommitUnknown):
        dj.append(ROOT, "first", "claim_intent", {"n": 1})
    u = dj.unknown
    assert u["expected_seq"] == 1 and confirm(dsn, W, u).status == "committed"
    dj.reacquire()
    dj.append(ROOT, "first", "claimed", {"outcome": "claimed"})
    dj.append(ROOT, "first", "operator_resolution", RES)
    dj.resolve(ROOT, "first")
    assert compact(dsn, 10.0)["compacted"] == 1
    assert confirm(dsn, W, u).status == "compacted"
    assert compact(dsn, 100.0)["dropped"] == 1
    c = confirm(dsn, W, u)
    assert c.status == "compacted" and c.record is None                   # unknown, operator-only; never not_observed
    never = dict(u, record_id=str(uuid.uuid4()), claim_key="never-existed")
    assert confirm(dsn, W, never).status == "compacted"                   # no retained proof distinguishes the two
    assert audit_counters(dsn) == []


def test_c2_not_observed_carries_no_record_and_the_held_resend_commits_once(jdb):
    """not_observed is reported only for an existing, valid stream and carries no record. Resending
    ONLY the held append is the caller's contract; the adapter enforces just the expected seq."""
    dsn = jdb.make()
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    dj.fault = lambda stage, d: d.conn.close() if stage == "before_commit" else None
    with pytest.raises(CommitUnknown):
        dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    u = dj.unknown
    dj.fault = None
    c = confirm(dsn, W, u)
    assert c.status == "not_observed" and c.record is None
    dj.reacquire()
    dj.append(u["root"], u["claim_key"], u["kind"], u["data"], record_id=u["record_id"], expected_seq=u["expected_seq"])
    assert confirm(dsn, W, u).status == "committed"
    dj.append(u["root"], u["claim_key"], u["kind"], u["data"], record_id=u["record_id"], expected_seq=u["expected_seq"])
    assert [r["kind"] for r in records(dsn)] == ["claim_intent", "claimed"] and audit_counters(dsn) == []


def test_c2_a_fresh_id_at_the_held_seq_is_not_blocked_by_the_adapter(jdb):
    """Documents the boundary (msgs 996/1007): at the HELD expected seq, the adapter does not tell a
    fresh id from the held resend; "resend only the held append" is the caller's contract. Breaking
    it commits an unrelated record and makes the held append unresendable (seq_mismatch)."""
    dsn = jdb.make()
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    dj.fault = lambda stage, d: d.conn.close() if stage == "before_commit" else None
    with pytest.raises(CommitUnknown):
        dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    u = dj.unknown
    dj.fault = None
    assert u["expected_seq"] == 2 and confirm(dsn, W, u).status == "not_observed"
    dj.reacquire()
    fresh = dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"}, record_id=str(uuid.uuid4()), expected_seq=u["expected_seq"])
    assert fresh.seq == u["expected_seq"]                                 # NOT enforced by the adapter
    with pytest.raises(JournalRefused) as e:
        dj.append(u["root"], u["claim_key"], u["kind"], u["data"], record_id=u["record_id"], expected_seq=u["expected_seq"])
    assert e.value.code == "seq_mismatch"                                 # the held append can no longer be resent
    assert confirm(dsn, W, u).status == "not_observed" and audit_counters(dsn) == []
