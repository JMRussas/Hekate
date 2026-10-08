"""The guarded operator takeover (HK-ISSUE-015 M1 primitive; root msgs 2204/2207/2209) on the FIXTURE-ONLY journal
tables of the disposable database. It proves epoch/ref idempotency only: no manifest, no reconciliation, no caller."""

import threading
import uuid

import psycopg
import pytest

from e1.durable import TakeoverResult, audit_counters, operator_takeover, operator_takeover_guarded
from e1.evidence import JournalRefused
from e2b_support import W, jdb, journal_digest, open_when_free  # noqa: F401 (fixture)

ROOT = str(uuid.uuid4())


def epoch(dsn, writer=W) -> int:
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT epoch FROM supervisor_journal.writer_usage WHERE writer_id = %s", (writer,)).fetchone()[0]


def takeovers(dsn, ref=None) -> list[tuple]:
    with psycopg.connect(dsn) as c:
        q = "SELECT writer_id, from_epoch, to_epoch, reconciliation_ref FROM supervisor_journal.takeovers"
        rows = c.execute(q + (" WHERE reconciliation_ref = %s" if ref else "") + " ORDER BY id", (ref,) if ref else ()).fetchall()
    return [tuple(r) for r in rows]


def refused(code, *a, **kw):
    with pytest.raises(JournalRefused) as e:
        operator_takeover_guarded(*a, **kw)
    assert e.value.code == code
    return e.value


def setup_writer(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    dj.append(ROOT, "k1", "claim_intent", {"n": 1})
    return dsn, dj


def test_a_first_call_bumps_once_and_an_identical_call_replays_without_writing(jdb):
    dsn, dj = setup_writer(jdb)
    assert operator_takeover_guarded(dsn, W, "recon-A", 1, 2.0) == TakeoverResult(2, False)
    assert epoch(dsn) == 2 and takeovers(dsn) == [(W, 1, 2, "recon-A")]
    before = journal_digest(dsn)
    assert operator_takeover_guarded(dsn, W, "recon-A", 1, 3.0) == TakeoverResult(2, True)
    assert journal_digest(dsn) == before and audit_counters(dsn) == []


def test_the_binding_is_exact_and_every_refusal_writes_nothing(jdb):
    dsn, dj = setup_writer(jdb)
    before = journal_digest(dsn)
    refused("takeover", dsn, W, "  ", 1, 1.0)
    refused("clock", dsn, W, "r", 1, float("nan"))
    for bad in (True, 0, -1, 1.0, "1"):
        refused("takeover_expected_epoch", dsn, W, "r", bad, 1.0)
    refused("takeover_unknown_writer", dsn, "nobody#X", "r", 1, 1.0)
    refused("epoch_moved_foreign", dsn, W, "r", 7, 1.0)                     # CAS: the epoch is 1, not 7
    assert journal_digest(dsn) == before
    operator_takeover_guarded(dsn, W, "recon-A", 1, 2.0)
    after = journal_digest(dsn)
    refused("takeover_ref_bound_elsewhere", dsn, W, "recon-A", 2, 3.0)      # same ref, another expected epoch
    assert journal_digest(dsn) == after


def test_a_foreign_takeover_is_never_absorbed(jdb):
    dsn, dj = setup_writer(jdb)
    operator_takeover(dsn, W, "legacy-1", 1.0)                              # the unguarded path moves 1 -> 2
    before = journal_digest(dsn)
    refused("epoch_moved_foreign", dsn, W, "recon-A", 1, 2.0)               # proof saw epoch 1
    assert journal_digest(dsn) == before and takeovers(dsn, "recon-A") == []
    assert operator_takeover_guarded(dsn, W, "recon-B", 2, 3.0) == TakeoverResult(3, False)
    operator_takeover(dsn, W, "legacy-2", 4.0)                              # a foreign bump AFTER our takeover
    before = journal_digest(dsn)
    refused("epoch_moved_after_takeover", dsn, W, "recon-B", 2, 5.0)
    assert journal_digest(dsn) == before


def test_legacy_repeated_refs_are_ambiguous_and_never_rewritten(jdb):
    dsn, dj = setup_writer(jdb)
    operator_takeover(dsn, W, "dup", 1.0)
    operator_takeover(dsn, W, "dup", 2.0)                                   # the legacy path allows a repeated ref
    rows = takeovers(dsn, "dup")
    refused("takeover_ref_ambiguous", dsn, W, "dup", 1, 3.0)
    assert takeovers(dsn, "dup") == rows and epoch(dsn) == 3


def test_a_caller_interrupted_after_a_real_commit_replays_on_reinvocation(jdb):
    # The hook raises AFTER the bump really committed: the caller never sees its result (a simulated lost
    # response at the caller, not a database or network fault). Re-invoking replays; no second bump.
    dsn, dj = setup_writer(jdb)

    def lose(stage):
        assert stage == "after_commit"
        raise ConnectionError("caller interrupted after commit (simulated)")
    with pytest.raises(ConnectionError):
        operator_takeover_guarded(dsn, W, "recon-A", 1, 2.0, fault=lose)
    assert epoch(dsn) == 2 and len(takeovers(dsn, "recon-A")) == 1          # the effect is durable
    assert operator_takeover_guarded(dsn, W, "recon-A", 1, 3.0) == TakeoverResult(2, True)
    assert len(takeovers(dsn, "recon-A")) == 1 and audit_counters(dsn) == []


def race(dsn, calls):
    """Run the calls concurrently from separate connections, released together."""
    gate, out = threading.Barrier(len(calls)), [None] * len(calls)

    def run(i, ref, exp):
        gate.wait()
        try:
            out[i] = operator_takeover_guarded(dsn, W, ref, exp, 2.0)
        except JournalRefused as e:
            out[i] = e.code
    ts = [threading.Thread(target=run, args=(i, *c)) for i, c in enumerate(calls)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    return out


def test_concurrent_identical_calls_bump_exactly_once(jdb):
    dsn, dj = setup_writer(jdb)
    out = race(dsn, [("recon-A", 1)] * 4)
    assert sorted(o.replayed for o in out) == [False, True, True, True] and {o.epoch for o in out} == {2}
    assert epoch(dsn) == 2 and len(takeovers(dsn, "recon-A")) == 1 and audit_counters(dsn) == []


def test_competing_refs_from_the_same_epoch_have_exactly_one_winner(jdb):
    dsn, dj = setup_writer(jdb)
    out = race(dsn, [("recon-A", 1), ("recon-B", 1)])
    wins = [o for o in out if isinstance(o, TakeoverResult)]
    assert len(wins) == 1 and wins[0] == TakeoverResult(2, False) and out.count("epoch_moved_foreign") == 1
    assert epoch(dsn) == 2 and len(takeovers(dsn)) == 1


def test_after_a_guarded_takeover_the_old_writer_is_fenced_and_a_new_session_can_append(jdb):
    dsn, dj = setup_writer(jdb)
    operator_takeover_guarded(dsn, W, "recon-A", 1, 2.0)
    before = journal_digest(dsn)
    with pytest.raises(JournalRefused) as e:
        dj.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    assert e.value.code == "fenced:epoch" and journal_digest(dsn) == before
    dj.close()
    new = open_when_free(dsn)
    try:
        assert new.epoch == 2
        new.append(ROOT, "k1", "claimed", {"outcome": "claimed"})
    finally:
        new.close()
    assert audit_counters(dsn) == []
