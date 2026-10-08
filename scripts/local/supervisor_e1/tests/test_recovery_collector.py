"""Read-only journal collector (e1/recovery_collector.py; HK-ISSUE-015; root msgs 2348/2349/2350/2351). DISPOSABLE harness
journal only: no coordinator store, no historical data. Every collected observation is fed to the accepted projection."""

import json
import uuid

import psycopg
import pytest

from e1 import recovery_collector as RC
from e1 import recovery_manifest as RM
from e1.durable import BUDGET_RECORD_MAX, compact
from e1.evidence import Bounds
from e2b_support import W, jdb, journal_digest, raw  # noqa: F401 (fixture)

CK = "ck-1"
TERMINAL = {"decision": "abandoned_no_effects", "reconciliationRef": "recon-1"}
PRE = [("claim_intent", {"attemptId": "a1", "note": "free text never collected"}), ("claimed", {"outcome": "claimed"}),
       ("package_ref", {"packageSha256": "p" * 64, "jcs": "{}"}), ("dispatch_intent", {"exec": "e" * 64, "key": {}})]


def feed(dj, root, records):
    for kind, data in records:
        dj.append(root, CK, kind, data)


def legacy_resolve(dsn, root):
    """The PRE-HK-017 resolve() arithmetic for a stream whose post-resolution intent is unanswered."""
    with psycopg.connect(dsn) as c:
        s = c.execute("SELECT reserved FROM supervisor_journal.streams WHERE root = %s", (root,)).fetchone()
        release = s[0] * BUDGET_RECORD_MAX
        c.execute("UPDATE supervisor_journal.streams SET state = 'resolved', resolved_at = 1.0, count = count - reserved, "
                  "bytes = bytes - %s, reserved = 0, fault_reserved = 0, outstanding = '[]'::jsonb WHERE root = %s", (release, root))
        c.execute("UPDATE supervisor_journal.global_usage SET count = count - %s, bytes = bytes - %s WHERE id = 1", (s[0], release))
        c.execute("UPDATE supervisor_journal.writer_usage SET unresolved = unresolved - 1 WHERE writer_id = %s", (W,))
        c.commit()


def by_root(obs):
    return {s["root"]: s for s in obs["streams"]}


def project_statuses(obs):
    p = json.loads(RM.project(obs)[0])
    return {s["root"]: (s["status"], s["reason"], [(i["id"], i["status"]) for i in s["intents"]]) for s in p["streams"]}


def test_collects_every_stream_kind_and_the_projection_accepts_it(jdb):
    dsn = jdb.make(Bounds(compact_after=1.0))
    dj = jdb.writer()
    feed(dj, "d-compacted", PRE + [("operator_resolution", TERMINAL)])
    dj.resolve("d-compacted", CK)
    compact(dsn, 10.0)                                                   # ONLY d-compacted exists yet: it is summarized
    feed(dj, "a-active", PRE)
    feed(dj, "b-resolved", PRE + [("operator_resolution", TERMINAL)])
    dj.resolve("b-resolved", CK)
    feed(dj, "c-legacy", PRE + [("operator_resolution", TERMINAL), ("notify_intent", {"n": 1})])
    legacy_resolve(dsn, "c-legacy")
    dj.close()
    before = journal_digest(dsn)
    obs = RC.collect_observation(dsn, W)
    assert journal_digest(dsn) == before                                 # read-only
    st = project_statuses(obs)
    assert st["a-active"] == ("listed", None, [("claim_intent@1", "answered"), ("dispatch_intent@4", "open_unknown")])
    assert st["b-resolved"][2][-1] == ("dispatch_intent@4", "closed:abandoned_no_effects")
    assert st["c-legacy"][2][-1] == ("notify_intent@6", "post_terminal_unknown")
    assert st["d-compacted"][:2] == ("compacted_no_enumeration", "records_compacted")
    assert by_root(obs)["d-compacted"]["summary"]["resolution"] == TERMINAL
    assert obs["writer"] == {"id": W, "epoch": 1} and obs["bounds"]["perStream"] == Bounds().per_stream
    text = json.dumps(obs)
    for forbidden in ("free text never collected", "packageSha256", '"exec"', "at_json", "intent_digest", "record_id", "postgres"):
        assert forbidden not in text                                    # whitelist only; no DSN/user


def test_reader_and_bind_problems_keep_the_stream_but_unlisted(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    feed(dj, "chain", PRE)
    feed(dj, "ok", PRE)
    dj.close()
    raw(dsn, "UPDATE supervisor_journal.records SET data = '{\"n\":9}' WHERE root = 'chain' AND seq = 1", replica=True)
    obs = RC.collect_observation(dsn, W)
    s = by_root(obs)
    assert s["chain"]["corrupt"].startswith("chain:") and s["chain"]["records"] == []
    assert s["ok"]["corrupt"] is None and [r["seq"] for r in s["ok"]["records"]] == [1, 2, 3, 4]
    assert project_statuses(obs)["chain"][0] == "unlistable"


def test_record_hashes_are_bound_to_the_reader_and_head():
    rec = lambda i: type("R", (), {"seq": i})()                          # noqa: E731
    recs, head = [rec(1), rec(2)], "b" * 64
    ok = [{"seq": 1, "h": "a" * 64}, {"seq": 2, "h": head}]
    assert RC._bind(recs, ok, head) is None
    assert RC._bind(recs, ok[:1], head) == "hash_bind:count"
    assert RC._bind(recs, [ok[0], {"seq": 3, "h": head}], head) == "hash_bind:seq"
    assert RC._bind(recs, [{"seq": 1, "h": None}, ok[1]], head) == "hash_bind:format"
    assert RC._bind(recs, ok, "c" * 64) == "hash_bind:head"


def test_an_unsanitizable_reference_marks_the_stream_not_rewritten(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    feed(dj, "bad-ref", PRE + [("operator_resolution", {"decision": "abandoned_no_effects", "reconciliationRef": "has a space"})])
    dj.close()
    s = by_root(RC.collect_observation(dsn, W))["bad-ref"]
    assert (s["corrupt"], s["records"]) == ("sanitize:reconciliation_ref", [])
    assert project_statuses({**RC.collect_observation(dsn, W)})["bad-ref"][:2] == ("unlistable", "sanitize:reconciliation_ref")


@pytest.mark.parametrize("tamper, why", [
    ("DELETE FROM supervisor_journal.summaries WHERE root = 'comp'", "summary_missing"),
    ("UPDATE supervisor_journal.summaries SET summary = repeat('x', 8000) WHERE root = 'comp'", "summary_size"),
])
def test_a_compacted_stream_with_a_summary_problem_gets_the_placeholder(jdb, tamper, why):
    dsn = jdb.make(Bounds(compact_after=1.0))
    dj = jdb.writer()
    feed(dj, "comp", PRE + [("operator_resolution", TERMINAL)])
    dj.resolve("comp", CK)
    dj.close()
    compact(dsn, 10.0)
    raw(dsn, tamper, replica=True)
    s = by_root(RC.collect_observation(dsn, W))["comp"]
    assert (s["corrupt"], s["summary"], s["records"]) == (why, {"kindsTail": [], "resolution": None}, [])


def test_header_problems_refuse_the_whole_collection(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    feed(dj, "r1", PRE)
    dj.close()
    raw(dsn, "UPDATE supervisor_journal.streams SET outstanding = to_jsonb(repeat('x', 70000)) WHERE root = 'r1'", replica=True)
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, W)
    assert e.value.code == "read_failed" and "outstanding" in str(e.value)


def test_an_over_cap_identity_is_never_invented(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    feed(dj, "r" * 1100, PRE[:1])                                        # over the 1024-octet DB cap, under the record cap
    dj.close()
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, W)
    assert e.value.code == "read_failed" and "root" in str(e.value)


@pytest.mark.parametrize("value", [5000])          # 0 is impossible in the DB (CHECK per_stream > 0)
def test_an_out_of_range_per_stream_refuses_before_any_stream_read(jdb, value):
    dsn = jdb.make()
    raw(dsn, f"UPDATE supervisor_journal.global_usage SET per_stream = {value}", replica=True)
    seen = []
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, W, _before_query=lambda cur: seen.append(1))
    assert e.value.code == "read_failed" and len(seen) == 2              # BEGIN and Q1 only


def test_a_missing_global_row_and_an_unknown_writer_refuse_typed(jdb):
    dsn = jdb.make()
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, "nobody#X")
    assert e.value.code == "writer_unknown"
    raw(dsn, "DELETE FROM supervisor_journal.global_usage", replica=True)
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, W)
    assert e.value.code == "read_failed"


def test_sixty_five_streams_refuse(jdb):
    dsn = jdb.make(Bounds(unresolved_streams=128, global_records=4000))
    dj = jdb.writer()
    for i in range(65):
        dj.append(f"s{i:03}", CK, "claim_intent", {"n": i})
    dj.close()
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, W)
    assert e.value.code == "input_overflow"


def test_the_size_counter_is_exact_and_overflow_stops_further_reads(jdb, monkeypatch):
    dsn = jdb.make()
    dj = jdb.writer()
    for r in ("a", "b", "c"):
        feed(dj, r, PRE)
    dj.close()
    obs = RC.collect_observation(dsn, W)
    exact = len(RC._canon(obs))
    monkeypatch.setattr(RC, "OBSERVATION_MAX", exact)                    # the full observation fits exactly
    assert RC.collect_observation(dsn, W) == obs
    monkeypatch.setattr(RC, "OBSERVATION_MAX", exact - 1)
    queries = []
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, W, _before_query=lambda cur: queries.append(1))
    assert e.value.code == "input_overflow"
    small = len(RC._canon(dict(obs, streams=obs["streams"][:1])))
    monkeypatch.setattr(RC, "OBSERVATION_MAX", small)                    # only the first stream fits
    queries.clear()
    with pytest.raises(RC.CollectorRefused):
        RC.collect_observation(dsn, W, _before_query=lambda cur: queries.append(1))
    assert len(queries) == 4 + 2 + 2                                      # BEGIN, Q1, Q2, Q3; stream a (2); stream b (2); no c


def test_no_query_after_the_soft_deadline(jdb):
    dsn = jdb.make()
    queries = []
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, W, deadline_s=0.0, _before_query=lambda cur: queries.append(1))
    assert e.value.code == "collector_timeout" and queries == []


def test_the_watchdog_requests_cancellation_of_a_running_statement(jdb):
    dsn = jdb.make()
    jdb.writer().close()

    def stall(cur):
        cur.execute("SELECT pg_sleep(20)")                                 # cancelled by the watchdog, not by statement_timeout
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, W, deadline_s=1.0, _before_query=stall)
    assert e.value.code == "collector_timeout"


def test_the_connection_is_read_only_and_errors_never_echo_the_dsn(jdb):
    dsn = jdb.make()
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with psycopg.connect(dsn, autocommit=True, options=RC.CONNECT_OPTIONS) as c:
            c.execute("INSERT INTO supervisor_journal.writer_usage (writer_id, epoch, unresolved) VALUES ('x', 1, 0)")
    bad = dsn.replace("dbname=", "dbname=nonexistent_") if "dbname=" in dsn else dsn + "_nonexistent"
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(bad, W)
    assert e.value.code == "connect_failed" and "password" not in str(e.value).lower() and bad not in str(e.value)


# --- root review 2366: deadline validation, setup cleanup, header numerics/bounds before stream reads ----------------

@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -1, True, "1", None])
def test_an_invalid_deadline_refuses_before_connecting(monkeypatch, deadline):
    def never(*a, **kw):
        raise AssertionError("connect must not be attempted")
    monkeypatch.setattr(RC.psycopg, "connect", never)
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation("dbname=x", W, deadline_s=deadline)
    assert e.value.code == "read_failed" and "deadline" in str(e.value)


class FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, *a):
        self.log.append(("execute", sql))

    def close(self):
        self.log.append("cursor.close")


class FakeConn:
    def __init__(self, log, cursor_fails=False):
        self.log, self.cursor_fails = log, cursor_fails

    def cursor(self):
        if self.cursor_fails:
            raise RuntimeError("cursor boom")
        return FakeCursor(self.log)

    def cancel(self):
        self.log.append("cancel")

    def close(self):
        self.log.append("conn.close")


def test_a_failing_cursor_creation_still_closes_the_connection(monkeypatch):
    log = []
    monkeypatch.setattr(RC.psycopg, "connect", lambda *a, **kw: FakeConn(log, cursor_fails=True))
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation("dbname=x", W)
    assert (e.value.code, str(e.value)) == ("read_failed", "read_failed: setup: RuntimeError") and log == ["conn.close"]


def test_a_failing_timer_start_still_closes_cursor_and_connection(monkeypatch):
    log = []
    monkeypatch.setattr(RC.psycopg, "connect", lambda *a, **kw: FakeConn(log))

    class BadTimer:
        def __init__(self, *a, **kw):
            self.daemon = False

        def start(self):
            raise RuntimeError("no threads")

        def cancel(self):
            log.append("timer.cancel")
    monkeypatch.setattr(RC.threading, "Timer", BadTimer)
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation("dbname=x", W)
    assert e.value.code == "read_failed" and "RuntimeError" in str(e.value)
    assert log == ["timer.cancel", ("execute", "ROLLBACK"), "cursor.close", "conn.close"]


def test_an_infinite_resolved_at_refuses_before_any_stream_read(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    feed(dj, "r1", PRE + [("operator_resolution", TERMINAL)])
    dj.resolve("r1", CK)
    dj.close()
    raw(dsn, "UPDATE supervisor_journal.streams SET resolved_at = 'Infinity' WHERE root = 'r1'", replica=True)
    queries = []
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, W, _before_query=lambda cur: queries.append(1))
    assert e.value.code == "read_failed" and "resolved_at" in str(e.value) and len(queries) == 4   # BEGIN, Q1, Q2, Q3


def test_outstanding_beyond_per_stream_refuses_before_any_stream_read(jdb):
    dsn = jdb.make()
    dj = jdb.writer()
    feed(dj, "r1", PRE)
    dj.close()
    raw(dsn, "UPDATE supervisor_journal.global_usage SET per_stream = 2",
        "UPDATE supervisor_journal.streams SET outstanding = '[[\"claimed\"],[\"claimed\"],[\"claimed\"]]'::jsonb WHERE root = 'r1'",
        replica=True)
    queries = []
    with pytest.raises(RC.CollectorRefused) as e:
        RC.collect_observation(dsn, W, _before_query=lambda cur: queries.append(1))
    assert e.value.code == "read_failed" and "outstanding" in str(e.value) and len(queries) == 4
