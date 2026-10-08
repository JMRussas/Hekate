"""HK-ISSUE-017 (root msgs 2252/2262/2263/2267): a terminal operator resolution closes only the intents recorded BEFORE it.

- resolve() (model and durable) refuses `resolution_not_final`, writing nothing, while an intent recorded after the
  stream's terminal resolution is unanswered; the no-terminal refusal and the confirmed-finish branch are unchanged;
- one FIFO pairing over the whole prefix (evidence.unanswered_intents) serves resolve() and handoff.pending_effects;
- legacy rows already resolved that way by the pre-fix resolve() are RETAINED with their records by time and pressure
  compaction (never summarized or evicted); with only such data left, admission still fails closed with global_cap.
No vocabulary, schema, policy or caller change; nothing recovers records already compacted before this fix."""

import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from e1 import handoff as H
from e1.durable import BUDGET_RECORD_MAX, audit_counters, compact
from e1.evidence import INTENTS, Bounds, JournalRefused, ModelJournal, final_resolution, unanswered_intents
from e2b_support import W, jdb, journal_digest  # noqa: F401 (fixture)

CK = "ck-1"
TERMINAL = {"decision": "abandoned_no_effects", "reconciliationRef": "recon-1"}
PRE = [("claim_intent", {"attemptId": "a1"}), ("claimed", {"outcome": "claimed"}),
       ("package_ref", {"packageSha256": "p" * 64, "jcs": "{}"}), ("dispatch_intent", {"exec": "e" * 64, "key": {}})]


def feed(append, root, records):
    for kind, data in records:
        append(root, CK, kind, data)


def model(*after, resolution=TERMINAL, root="root-1"):
    m = ModelJournal("w#1", now=1.0)
    feed(m.append, root, PRE + ([("operator_resolution", resolution)] if resolution else []) + list(after))
    return m, m.streams[(root, CK)]


def state(s):
    return (s.resolved_at, [list(o) for o in s.outstanding], s.reserved, s.fault_reserved, len(s.records))


# --- the shared pairing -------------------------------------------------------------------------------------------

def old_pairing(records):
    """The pairing loop exactly as handoff.pending_effects had it before the shared helper (9778b7e)."""
    open_ = []
    for r in records:
        if r.kind in INTENTS:
            open_.append((r, list(INTENTS[r.kind])))
        for o in open_:
            if r.kind in o[1]:
                o[1].remove(r.kind)
                break
    return [(r.seq, rest) for r, rest in open_ if rest]


@pytest.mark.parametrize("after", [
    [],
    [("notify_intent", {"n": 1})],
    [("notify_intent", {"n": 1}), ("notify_outcome", {"n": 1})],
    [("dispatch_intent", {"exec": "f" * 64, "key": {}}), ("dispatch_outcome", {"exec": "e" * 64, "outcome": "x"})],
    [("operator_resolution", {"decision": "release", "reconciliationRef": "r"}), ("notify_intent", {"n": 2})],
])
def test_the_shared_pairing_equals_the_previous_pending_effects_loop(after):
    _, s = model(*after)
    assert [(r.seq, rest) for r, rest in unanswered_intents(s.records)] == old_pairing(s.records)


# --- resolvers: model ----------------------------------------------------------------------------------------------

def test_a_post_resolution_unanswered_intent_refuses_and_changes_nothing():
    m, s = model(("notify_intent", {"n": 1}))
    before = state(s)
    with pytest.raises(JournalRefused) as e:
        m.resolve("root-1", CK)
    assert e.value.code == "resolution_not_final" and "1: notify_intent@6" in str(e.value)
    assert state(s) == before and s.resolved_at is None and s.outstanding


def test_recording_its_outcome_or_a_new_final_resolution_lets_resolve_succeed():
    m, s = model(("notify_intent", {"n": 1}), ("notify_outcome", {"n": 1}))
    m.resolve("root-1", CK)
    assert s.resolved_at is not None
    m2, s2 = model(("notify_intent", {"n": 1}), ("operator_resolution", dict(TERMINAL, reconciliationRef="recon-2")))
    m2.resolve("root-1", CK)
    items = H.pending_effects(s2.records, None, s2.outstanding)
    assert [(i["id"].split("@")[0], i["status"]) for i in items] == [("dispatch_intent", "closed:abandoned_no_effects"),
                                                                      ("notify_intent", "closed:abandoned_no_effects")]


def test_pre_resolution_intents_still_resolve_and_the_old_refusals_are_unchanged():
    m, s = model()
    m.resolve("root-1", CK)                                              # abandoned_no_effects closes the earlier intent
    assert s.resolved_at is not None
    m2, s2 = model(resolution=None)
    with pytest.raises(JournalRefused) as e:
        m2.resolve("root-1", CK)
    assert (e.value.code, str(e.value)) == ("unresolved_outstanding",
                                            "unresolved_outstanding: needs a confirmed finish + decision, or a terminal operator resolution")


def test_the_confirmed_finish_branch_is_unchanged():
    m = ModelJournal("w#1", now=1.0)
    feed(m.append, "root-1", [("claim_intent", {"attemptId": "a1"}), ("claimed", {"outcome": "claimed"}),
                              ("finish_intent", {"to": "done"}), ("finish_outcome", {"outcome": "applied"})])
    m.resolve("root-1", CK, planstore_decided=True)
    assert m.streams[("root-1", CK)].resolved_at is not None


def test_fifo_trap_a_later_outcome_answers_the_earlier_intent_so_the_later_one_still_blocks():
    m, s = model(("dispatch_intent", {"exec": "f" * 64, "key": {}}), ("dispatch_outcome", {"exec": "e" * 64, "outcome": "x"}))
    f = final_resolution(s.records)
    assert f.problem == "resolution_not_final" and f.after_ids == ("dispatch_intent@6",) and f.after_count == 1
    with pytest.raises(JournalRefused) as e:
        m.resolve("root-1", CK)
    assert e.value.code == "resolution_not_final"


def test_after_ids_are_bounded_and_the_count_is_exact():
    m, s = model(*[("notify_intent", {"n": i}) for i in range(11)])
    f = final_resolution(s.records)
    assert f.after_count == 11 and len(f.after_ids) == 8


def legacy_resolve_model(m, s):
    """What the PRE-FIX ModelJournal.resolve did to a stream with a post-resolution intent (it is now refused)."""
    s.resolved_at = m.now
    s.reserved, s.fault_reserved, s.outstanding = 0, 0, []


def test_model_time_compaction_retains_a_legacy_unsafe_stream_and_compacts_a_safe_one():
    m, unsafe = model(("notify_intent", {"n": 1}))
    legacy_resolve_model(m, unsafe)
    feed(m.append, "root-2", PRE + [("operator_resolution", TERMINAL)])
    m.resolve("root-2", CK)
    m.now = 1.0 + m.bounds.compact_after
    n = len(unsafe.records)
    m.compact()
    assert unsafe.summary is None and len(unsafe.records) == n
    assert m.streams[("root-2", CK)].summary is not None and m.streams[("root-2", CK)].records == []


def test_model_pressure_eviction_never_compacts_a_legacy_unsafe_stream_and_fails_closed():
    m = ModelJournal("w#1", Bounds(per_stream=16, unresolved_streams=64, global_records=48, global_bytes=1 << 20), now=1.0)
    feed(m.append, "unsafe", PRE + [("operator_resolution", TERMINAL), ("notify_intent", {"n": 1})])
    unsafe = m.streams[("unsafe", CK)]
    legacy_resolve_model(m, unsafe)
    feed(m.append, "safe", PRE + [("operator_resolution", TERMINAL)])
    m.resolve("safe", CK)
    kept = ([(r.seq, r.kind) for r in unsafe.records], unsafe.count(), unsafe.nbytes(), unsafe.summary)
    code = None
    for i in range(64):                                                  # new live work until admission fails
        try:
            m.append(f"live-{i}", CK, "claim_intent", {"attemptId": f"a{i}"})
        except JournalRefused as e:
            code = e.code
            break
    assert code == "global_cap"                                           # fail closed: no eligible room left
    safe = m.streams.get(("safe", CK))
    assert safe is None or safe.summary is not None                       # the safe resolved stream was evicted for room
    assert ([(r.seq, r.kind) for r in unsafe.records], unsafe.count(), unsafe.nbytes(), unsafe.summary) == kept
    assert m.streams[("unsafe", CK)] is unsafe                            # the unsafe stream kept every record


# --- resolvers and compaction: durable (owned disposable journal DB) ---------------------------------------------------

def durable_stream(dj, root, *after, resolution=TERMINAL):
    feed(dj.append, root, PRE + ([("operator_resolution", resolution)] if resolution else []) + list(after))


def test_durable_resolve_refuses_without_writing_then_succeeds_after_a_new_final_resolution(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    root = str(uuid.uuid4())
    durable_stream(dj, root, ("notify_intent", {"n": 1}))
    before = journal_digest(dsn)
    with pytest.raises(JournalRefused) as e:
        dj.resolve(root, CK)
    assert e.value.code == "resolution_not_final" and journal_digest(dsn) == before
    dj.append(root, CK, "operator_resolution", dict(TERMINAL, reconciliationRef="recon-2"))
    dj.resolve(root, CK)
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        assert c.execute("SELECT state FROM supervisor_journal.streams WHERE root = %s", (root,)).fetchone()["state"] == "resolved"
    assert audit_counters(dsn) == []


def legacy_resolve_sql(dsn, root, now):
    """One transaction mirroring the PRE-FIX DurableJournal.resolve counter arithmetic exactly (it is now refused)."""
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        s = c.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s FOR UPDATE", (root, CK)).fetchone()
        release = s["reserved"] * BUDGET_RECORD_MAX
        c.execute("UPDATE supervisor_journal.streams SET state = 'resolved', resolved_at = %s, count = count - reserved, "
                  "bytes = bytes - %s, reserved = 0, fault_reserved = 0, outstanding = '[]'::jsonb "
                  "WHERE root = %s AND claim_key = %s", (now, release, root, CK))
        c.execute("UPDATE supervisor_journal.global_usage SET count = count - %s, bytes = bytes - %s WHERE id = 1", (s["reserved"], release))
        c.execute("UPDATE supervisor_journal.writer_usage SET unresolved = unresolved - 1 WHERE writer_id = %s", (s["writer_id"],))
        c.commit()


def stream_row(dsn, root):
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        s = c.execute("SELECT state, count, bytes FROM supervisor_journal.streams WHERE root = %s", (root,)).fetchone()
        n = c.execute("SELECT count(*) AS n FROM supervisor_journal.records WHERE root = %s", (root,)).fetchone()["n"]
    return (s["state"], s["count"], s["bytes"], n) if s else None


def test_durable_time_compaction_retains_a_legacy_unsafe_row_and_compacts_a_safe_one(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    unsafe, safe = str(uuid.uuid4()), str(uuid.uuid4())
    durable_stream(dj, unsafe, ("notify_intent", {"n": 1}))
    legacy_resolve_sql(dsn, unsafe, 1.0)
    durable_stream(dj, safe)
    dj.resolve(safe, CK)
    assert audit_counters(dsn) == []
    before = stream_row(dsn, unsafe)
    out = compact(dsn, 1.0 + Bounds().compact_after)
    assert out["compacted"] == 1 and out["retained_unsafe"] == [(unsafe, CK, "resolution_not_final")]
    assert stream_row(dsn, unsafe) == before and stream_row(dsn, safe)[0] == "compacted" and audit_counters(dsn) == []


def test_durable_pressure_eviction_never_compacts_a_legacy_unsafe_row_and_fails_closed(jdb):
    dsn = jdb.make(Bounds(per_stream=16, unresolved_streams=64, global_records=48, global_bytes=1 << 20))
    dj = jdb.writer()
    unsafe, safe = str(uuid.uuid4()), str(uuid.uuid4())
    durable_stream(dj, unsafe, ("notify_intent", {"n": 1}))
    legacy_resolve_sql(dsn, unsafe, 1.0)
    durable_stream(dj, safe)
    dj.resolve(safe, CK)
    kept = stream_row(dsn, unsafe)
    code = None
    for i in range(64):                                                  # new live work until admission fails
        try:
            dj.append(str(uuid.uuid4()), CK, "claim_intent", {"attemptId": f"a{i}"})
        except JournalRefused as e:
            code = e.code
            break
    assert code == "global_cap"                                           # fail closed: no room left but unsafe data
    assert stream_row(dsn, safe)[0] in ("compacted", None)                # the safe resolved stream was evicted for room
    assert stream_row(dsn, unsafe) == kept and audit_counters(dsn) == []  # the unsafe row kept every record
