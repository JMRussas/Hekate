"""E2b-a durable journal adapter (plan 028 §9; TEST-ONLY, disposable database).

Implements the E2a hook contract (`journal(kind, data) -> Record`, with `degraded`) over the
FIXTURE-ONLY `supervisor_journal` tables (journal_schema.sql). Vocabulary, payload validation,
record encoding, reservation rules, resolution predicates and the classifier are REUSED from
e1/evidence.py; this module adds only what durability needs:

- one record per transaction, locks taken in the fixed order global_usage -> writer_usage ->
  streams (FOR UPDATE), so parallel writers serialize on the singleton and cannot both pass a
  nearly full global cap;
- an immutable client record id with an idempotent INSERT: the identity is (writer, stream, kind,
  exact original payload, expected seq); an identical resend is a no-op, a different one is
  refused and changes nothing;
- a lost COMMIT reply is UNKNOWN (CommitUnknown): the adapter becomes unusable until an explicit
  reacquire(), the caller performs no effect and looks the record id up on a new connection;
- APPEND fencing only: every append re-checks the writer epoch AND that this same live session
  holds the writer's advisory lock. Effects outside the transaction are NOT fenced (028 §5);
- re-validation of the whole stream (seq contiguity, hash chain, payload, sizes) on every write:
  a corrupt stream refuses new records and is never compacted or evicted.

Bytes are LOGICAL, not on-disk: each record is charged its exact E2a encoded size plus the exact
size of its durable metadata (id, epoch, version, intent digest, previous and own hash). Reserved
slots are charged at the worst case of both. Nothing here is a production mechanism.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from e1.evidence import (AT_REPR_MAX, FAULT_RESERVE, INTENTS, KINDS, MAX_AT, MAX_SEQ, RECORD_MAX_BYTES, RESERVE_KINDS,
                         TERMINALS, Bounds, JournalRefused, ModelJournal, Record, Stream, _check_payload, _fallback_text,
                         encoded_size, is_confirmed_finish, is_terminal_resolution, worst_reserved_record_size)

SCHEMA_SQL = Path(__file__).with_name("journal_schema.sql")
VERSION = 1
GENESIS = "0" * 64
LOCK_NS = 0x5E2B                 # advisory lock namespace (pg_locks.classid); objid = hashtext(writer)
CONNECT_OPTIONS = "-c lock_timeout=5000 -c statement_timeout=30000"


def durable_meta_bytes(record_id: str, epoch: int, intent: str, prev: str, own: str) -> int:
    """Exact logical size of the durable metadata a record adds to its E2a encoding."""
    return len(json.dumps({"epoch": epoch, "hash": own, "id": record_id, "intent": intent, "prev": prev, "v": VERSION},
                          separators=(",", ":")).encode("utf-8"))


DURABLE_META_MAX = durable_meta_bytes(str(uuid.UUID(int=0)), MAX_SEQ, GENESIS, GENESIS, GENESIS)
BUDGET_RECORD_MAX = RECORD_MAX_BYTES + DURABLE_META_MAX     # what one reserved slot is charged


class CommitUnknown(Exception):
    """The COMMIT result was not observed. The record may or may not exist: look it up."""

    def __init__(self, record_id: str, cause: str):
        super().__init__(f"commit outcome unknown for {record_id}: {cause}")
        self.record_id = record_id


def _valid_clock(v: object) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= MAX_AT
            and len(json.dumps(v)) <= AT_REPR_MAX)


def intent_digest(writer: str, root: str, claim_key: str, kind: str, data: Any, expected_seq: int) -> str:
    """Idempotency identity: writer, stream, kind, the EXACT original payload and the expected seq.
    Independent of the clock and of any retry metadata."""
    try:
        payload = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        payload = "repr:" + repr(data)
    ident = json.dumps([VERSION, writer, root, claim_key, kind, payload, expected_seq], separators=(",", ":"))
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()


def record_hash(prev: str, row: dict[str, Any]) -> str:
    canonical = json.dumps([VERSION, str(row["record_id"]), row["root"], row["claim_key"], row["seq"], row["kind"],
                            row["writer_id"], row["writer_epoch"], row["at_json"], row["data"], row["intent_digest"],
                            row["budget_bytes"]], separators=(",", ":"))
    return hashlib.sha256((prev + canonical).encode("utf-8")).hexdigest()


def to_record(row: dict[str, Any]) -> Record:
    return Record(row["seq"], row["kind"], row["writer_id"], row["root"], row["claim_key"], json.loads(row["at_json"]), row["data"])


def summary_budget(summary: str, head: str) -> int:
    return len(summary.encode("utf-8")) + len(head)


# --- bounded, validated stream read (shared by appends, compaction and recovery) ------------

_STREAM_ROWS_SQL = """
SELECT t.* FROM (
    SELECT p.*, count(*) OVER () AS page_rows,
           sum(octet_length(p.data) + octet_length(p.at_json) + octet_length(p.kind) + octet_length(p.writer_id)
               + octet_length(p.intent_digest) + octet_length(p.prev_hash) + octet_length(p.record_hash))
               OVER (ORDER BY p.seq) AS cum_bytes
    FROM (SELECT record_id::text AS record_id, seq, kind, writer_id, writer_epoch, at_json, data, version, intent_digest,
                 prev_hash, record_hash, budget_bytes
            FROM supervisor_journal.records WHERE root = %(root)s AND claim_key = %(key)s
           ORDER BY seq LIMIT %(limit)s) p
) t WHERE t.cum_bytes <= %(max_bytes)s ORDER BY t.seq
"""


def read_stream_records(cur: psycopg.Cursor, root: str, claim_key: str, stream: dict[str, Any] | None,
                        per_stream: int) -> tuple[list[Record], str | None]:
    """Read and validate a stream's records, bounded AT THE DATABASE (rows and bytes). Returns the
    validated records and None, or the valid prefix and a corruption/incompleteness reason."""
    limit = per_stream + 1
    max_bytes = per_stream * BUDGET_RECORD_MAX
    cur.execute(_STREAM_ROWS_SQL, {"root": root, "key": claim_key, "limit": limit, "max_bytes": max_bytes})
    rows = cur.fetchall()
    if rows and (len(rows) < rows[0]["page_rows"] or rows[0]["page_rows"] > per_stream):
        return [], "incomplete:bounds"
    prev, out = GENESIS, []
    for i, r in enumerate(rows, 1):
        r = dict(r, root=root, claim_key=claim_key)
        if r["seq"] != i:
            return out, f"seq_gap:{i}"
        if r["version"] != VERSION or r["kind"] not in KINDS:
            return out, f"format:{i}"
        if stream is not None and r["writer_id"] != stream["writer_id"]:
            return out, f"writer:{i}"
        if r["prev_hash"] != prev or record_hash(prev, r) != r["record_hash"]:
            return out, f"chain:{i}"
        try:
            at = json.loads(r["at_json"])
            _check_payload(json.loads(r["data"]))
        except (ValueError, JournalRefused):
            return out, f"payload:{i}"
        if not _valid_clock(at):
            return out, f"clock:{i}"
        size = encoded_size(r["kind"], r["writer_id"], root, claim_key, r["seq"], at, r["data"])
        meta = durable_meta_bytes(r["record_id"], r["writer_epoch"], r["intent_digest"], r["prev_hash"], r["record_hash"])
        if size > RECORD_MAX_BYTES or r["budget_bytes"] != size + meta:
            return out, f"size:{i}"
        prev = r["record_hash"]
        out.append(to_record(r))
    if stream is not None and stream["state"] != "compacted":
        if len(rows) != stream["next_seq"] - 1:
            return out, "seq_count"
        if stream["head_hash"] != prev:
            return out, "head"
    return out, None


SUMMARY_MAX_BYTES = 3 * RECORD_MAX_BYTES + 1024     # identities + last kinds + two bounded payloads


def summary_problem(cur: psycopg.Cursor, st: dict[str, Any]) -> str | None:
    """Validate a COMPACTED stream before it may be dropped (by age or pressure): the summary exists,
    is bounded (checked at the database), parses, names the same stream/writer/resolution time, keeps
    the stream's chain head, and the stream's counters equal what the summary is charged. Anything
    else is corruption: the stream is retained, never dropped."""
    row = cur.execute("SELECT writer_id, resolved_at, head_hash, octet_length(summary) AS n, "
                      "CASE WHEN octet_length(summary) <= %s THEN summary END AS summary "
                      "FROM supervisor_journal.summaries WHERE root = %s AND claim_key = %s",
                      (SUMMARY_MAX_BYTES, st["root"], st["claim_key"])).fetchone()
    if row is None:
        return "summary_missing"
    if row["summary"] is None:
        return "summary_size"
    try:
        doc = json.loads(row["summary"])
    except ValueError:
        return "summary_format"
    if not isinstance(doc, dict) or (doc.get("root"), doc.get("claimKey"), doc.get("writer")) != (st["root"], st["claim_key"], st["writer_id"]):
        return "summary_identity"
    if row["writer_id"] != st["writer_id"] or row["resolved_at"] != st["resolved_at"] or doc.get("resolvedAt") != st["resolved_at"]:
        return "summary_identity"
    if row["head_hash"] != st["head_hash"]:
        return "summary_head"
    if (st["count"], st["bytes"]) != (1, summary_budget(row["summary"], row["head_hash"])):
        return "summary_counters"
    return None


def install(dsn: str, bounds: Bounds) -> None:
    """Apply the fixture-only schema and the singleton (disposable database only)."""
    with psycopg.connect(dsn, autocommit=False) as conn:
        conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO supervisor_journal.global_usage (id, count, bytes, per_stream, unresolved_streams, global_records, "
                     "global_bytes, compact_after, summary_retention, notify_max) VALUES (1, 0, 0, %s, %s, %s, %s, %s, %s, %s)",
                     (bounds.per_stream, bounds.unresolved_streams, bounds.global_records, bounds.global_bytes,
                      bounds.compact_after, bounds.summary_retention, bounds.notify_max))
        conn.commit()


def bounds_of(g: dict[str, Any]) -> Bounds:
    return Bounds(per_stream=g["per_stream"], unresolved_streams=g["unresolved_streams"], global_records=g["global_records"],
                  global_bytes=g["global_bytes"], compact_after=g["compact_after"], summary_retention=g["summary_retention"],
                  notify_max=g["notify_max"])


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, autocommit=False, row_factory=dict_row, options=CONNECT_OPTIONS)


@dataclass(frozen=True)
class Confirmation:
    """Outcome of confirm(). ONLY "committed" (with its record) authorizes the effect that followed
    the append. Every other status authorizes nothing new:
    - "not_observed": the stream EXISTS, validates, and shows no record with this id. NOT proof of
      absence (a COMMIT may still become visible). By the caller's contract only the SAME held
      idempotent append (same id, payload and expected seq) may be resent, or the caller stops for
      an operator; the adapter does not enforce that rule beyond the expected-seq check;
    - "compacted": the stream is compacted, dropped, or not visible at all. A missing stream can be
      one whose first record committed and was later resolved, compacted and dropped, so it is
      NEVER read as "not committed": the outcome is unknown and only an operator reconciles it."""
    status: str
    record: Record | None = None


def confirm(dsn: str, writer: str, pending: dict[str, Any]) -> Confirmation:
    """Authoritative lookup after CommitUnknown, on a NEW connection and in ONE snapshot. "committed"
    only if the stream validates end to end AND the stored record has the pending append's full
    identity (writer, stream, kind, original payload, expected seq). Corruption or a different
    identity under the id raises: neither can authorize anything."""
    want = intent_digest(writer, pending["root"], pending["claim_key"], pending["kind"], pending["data"], pending["expected_seq"])
    with connect(dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        g = conn.execute("SELECT per_stream FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
        row = conn.execute("SELECT record_id::text AS record_id, root, claim_key, seq, kind, writer_id, at_json, data, intent_digest "
                           "FROM supervisor_journal.records WHERE record_id = %s", (pending["record_id"],)).fetchone()
        s = conn.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s",
                         (pending["root"], pending["claim_key"])).fetchone()
        bad = None
        if s is not None and s["state"] != "compacted":
            with conn.cursor() as cur:
                _, bad = read_stream_records(cur, pending["root"], pending["claim_key"], s, g["per_stream"])
        conn.rollback()
    if bad:
        raise JournalRefused("corrupt", bad)
    if row is None:
        if s is None or s["state"] == "compacted":
            return Confirmation("compacted")              # records gone or never visible: unknown, operator-only
        return Confirmation("not_observed")
    if (row["intent_digest"] != want or row["seq"] != pending["expected_seq"] or row["writer_id"] != writer
            or (row["root"], row["claim_key"], row["kind"]) != (pending["root"], pending["claim_key"], pending["kind"])):
        raise JournalRefused("identity_mismatch", "a different record holds this id")
    return Confirmation("committed", to_record(row))


def operator_takeover(dsn: str, writer: str, reconciliation_ref: str, now: float) -> int:
    """OPERATOR act: bump the writer's epoch (fences its FUTURE journal appends only; it cannot prove
    the old process is quiescent or stop effects it still sends). Returns the new epoch."""
    if not isinstance(reconciliation_ref, str) or not reconciliation_ref.strip():
        raise JournalRefused("takeover", "a reconciliationRef is required")
    if not _valid_clock(now):
        raise JournalRefused("clock")
    with connect(dsn) as conn:
        conn.execute("SELECT 1 FROM supervisor_journal.global_usage WHERE id = 1 FOR UPDATE")
        row = conn.execute("SELECT epoch FROM supervisor_journal.writer_usage WHERE writer_id = %s FOR UPDATE", (writer,)).fetchone()
        if row is None:
            raise JournalRefused("takeover", "unknown writer")
        new = row["epoch"] + 1
        conn.execute("UPDATE supervisor_journal.writer_usage SET epoch = %s WHERE writer_id = %s", (new, writer))
        conn.execute("INSERT INTO supervisor_journal.takeovers (writer_id, from_epoch, to_epoch, reconciliation_ref, at_json) "
                     "VALUES (%s, %s, %s, %s, %s)", (writer, row["epoch"], new, reconciliation_ref, json.dumps(now)))
        conn.commit()
    return new


def audit_counters(dsn: str) -> list[str]:
    """Recompute every counter from the rows (test assertion helper). Empty when consistent."""
    problems: list[str] = []
    with connect(dsn) as conn:
        g = conn.execute("SELECT * FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
        streams = conn.execute("SELECT s.*, (SELECT count(*) FROM supervisor_journal.records r WHERE r.root = s.root AND "
                               "r.claim_key = s.claim_key) AS n, (SELECT coalesce(sum(budget_bytes), 0) FROM "
                               "supervisor_journal.records r WHERE r.root = s.root AND r.claim_key = s.claim_key) AS b, "
                               "(SELECT summary_budget FROM (SELECT octet_length(summary) + length(head_hash) AS summary_budget "
                               "FROM supervisor_journal.summaries m WHERE m.root = s.root AND m.claim_key = s.claim_key) x) AS sb "
                               "FROM supervisor_journal.streams s").fetchall()
        writers = conn.execute("SELECT w.writer_id, w.unresolved, (SELECT count(*) FROM supervisor_journal.streams s WHERE "
                               "s.writer_id = w.writer_id AND s.state = 'active') AS active FROM supervisor_journal.writer_usage w").fetchall()
        conn.rollback()
    tc = tb = 0
    for s in streams:
        if s["state"] == "compacted":
            want_c, want_b = 1, s["sb"]
        else:
            want_c, want_b = s["n"] + s["reserved"], s["b"] + s["reserved"] * BUDGET_RECORD_MAX
        if (s["count"], s["bytes"]) != (want_c, want_b):
            problems.append(f"stream {s['claim_key']}: {(s['count'], s['bytes'])} != {(want_c, want_b)}")
        tc += s["count"]
        tb += s["bytes"]
    if (g["count"], g["bytes"]) != (tc, tb):
        problems.append(f"global: {(g['count'], g['bytes'])} != {(tc, tb)}")
    for w in writers:
        if w["unresolved"] != w["active"]:
            problems.append(f"writer {w['writer_id']}: unresolved {w['unresolved']} != active {w['active']}")
    return problems


class DurableJournal:
    """One writer instance: one session that holds the writer's advisory lock and runs its appends."""

    def __init__(self, dsn: str, writer: str, *, now: float = 0.0,
                 fault: Callable[[str, "DurableJournal"], None] | None = None):
        self.dsn, self.writer = dsn, writer
        self.now = now
        self.fault = fault             # TEST hook: fault(stage, self) at "before_commit" / "after_commit"
        self.conn: psycopg.Connection | None = None
        self.epoch: int | None = None
        self.broken = False
        self._expected: dict[tuple[str, str], int] = {}
        # The last append whose COMMIT outcome is unknown: what the caller needs for the authoritative
        # lookup and an idempotent resend (same id, same payload, same expected seq).
        self.unknown: dict[str, Any] | None = None

    # --- clock (validated on assignment, as in E2a) ----------------------------------
    @property
    def now(self) -> float:
        return self._now

    @now.setter
    def now(self, value: float) -> None:
        if not _valid_clock(value):
            raise JournalRefused("clock", "clock must be finite, non-negative and <= MAX_AT")
        self._now = value

    # --- fence --------------------------------------------------------------------------
    def open(self) -> "DurableJournal":
        """Acquire the writer's session advisory lock (refused if another live session holds it)."""
        conn = connect(self.dsn)
        try:
            got = conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS ok", (LOCK_NS, self.writer)).fetchone()["ok"]
            if not got:
                raise JournalRefused("fence_busy", f"writer {self.writer} is held by another live session")
            conn.execute("INSERT INTO supervisor_journal.writer_usage (writer_id, epoch, unresolved) VALUES (%s, 1, 0) "
                         "ON CONFLICT (writer_id) DO NOTHING", (self.writer,))
            epoch = conn.execute("SELECT epoch FROM supervisor_journal.writer_usage WHERE writer_id = %s", (self.writer,)).fetchone()["epoch"]
            conn.commit()
        except BaseException:
            conn.close()
            raise
        if self.epoch is not None and epoch != self.epoch:
            conn.close()
            self.broken = True
            raise JournalRefused("fenced:epoch", f"epoch moved {self.epoch} -> {epoch}; an operator took over")
        self.conn, self.epoch, self.broken = conn, epoch, False
        return self

    def reacquire(self) -> "DurableJournal":
        """EXPLICIT re-open after a lost connection or CommitUnknown. Refused if the epoch moved."""
        self.close()
        return self.open()

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None

    def backend_pid(self) -> int:
        return self.conn.info.backend_pid

    def _check_fence(self, cur: psycopg.Cursor) -> dict[str, Any]:
        w = cur.execute("SELECT epoch, unresolved FROM supervisor_journal.writer_usage WHERE writer_id = %s FOR UPDATE",
                        (self.writer,)).fetchone()
        if w is None or w["epoch"] != self.epoch:
            raise JournalRefused("fenced:epoch", "writer epoch changed (operator takeover)")
        held = cur.execute("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted AND "
                           "pid = pg_backend_pid() AND classid = %s::oid AND objid = hashtext(%s)::oid AND objsubid = 2) AS ok",
                           (LOCK_NS, self.writer)).fetchone()["ok"]
        if not held:
            raise JournalRefused("fenced:lock", "this session does not hold the writer lock")
        return w

    def verify_fence(self) -> None:
        """Re-check epoch + same-session lock (immediately before an effect). Narrows, never closes, the window."""
        self._usable()
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1 FROM supervisor_journal.global_usage WHERE id = 1 FOR SHARE")
                self._check_fence(cur)
            self.conn.rollback()
        except psycopg.Error as e:
            self._break()
            raise JournalRefused("fenced:connection", type(e).__name__) from e
        except JournalRefused:
            self.conn.rollback()
            raise

    def _usable(self) -> None:
        if self.broken or self.conn is None or self.conn.closed:
            raise JournalRefused("fence_lost", "connection lost: an explicit reacquire() is required")

    def _break(self) -> None:
        self.broken = True
        self.close()

    # --- appends ---------------------------------------------------------------------------
    def hook(self, root: str, claim_key: str) -> Callable[[str, dict[str, Any]], Record]:
        """The E2a Supervisor journal hook for one stream."""
        def journal(kind: str, data: dict[str, Any]) -> Record:
            return self.append(root, claim_key, kind, data)
        return journal

    def append(self, root: str, claim_key: str, kind: str, data: dict[str, Any], *, record_id: str | None = None,
               expected_seq: int | None = None) -> Record:
        self._usable()
        if kind not in KINDS:
            raise JournalRefused("unknown_kind", kind)
        rid = str(uuid.UUID(str(record_id))) if record_id is not None else str(uuid.uuid4())
        skey = (root, claim_key)
        exp = expected_seq if expected_seq is not None else self._expected.get(skey)
        try:
            with self.conn.cursor() as cur:
                rec, created = self._append_tx(cur, root, claim_key, kind, data, rid, exp)
                if not created:
                    self.conn.rollback()
                    self._expected[skey] = rec.seq + 1
                    if kind in INTENTS:
                        self.verify_fence()                  # a duplicate intent authorizes nothing without the fence
                    return rec
                if self.fault:
                    self.fault("before_commit", self)
        except JournalRefused:
            self._safe_rollback()
            raise
        except psycopg.Error as e:
            # The server never received our COMMIT: not committed. The session is gone or unusable.
            self._break()
            raise JournalRefused("connection_lost", type(e).__name__) from e
        pending = {"record_id": rid, "root": root, "claim_key": claim_key, "kind": kind, "data": data, "expected_seq": rec.seq}
        try:
            self.conn.commit()
        except psycopg.Error as e:
            self._break()
            self.unknown = pending
            raise CommitUnknown(rid, type(e).__name__) from e
        if self.fault:
            try:
                self.fault("after_commit", self)
            except Exception as e:  # noqa: BLE001 -- the REPLY was lost after a real COMMIT
                self._break()
                self.unknown = pending
                raise CommitUnknown(rid, f"reply lost: {type(e).__name__}") from e
        self._expected[skey] = rec.seq + 1
        if kind in INTENTS:
            self.verify_fence()        # the effect may start only while the fence still holds
        return rec

    def _safe_rollback(self) -> None:
        try:
            if self.conn is not None and not self.conn.closed:
                self.conn.rollback()
        except psycopg.Error:
            self._break()

    def _append_tx(self, cur, root, claim_key, kind, data, rid, exp) -> tuple[Record, bool]:
        g = cur.execute("SELECT * FROM supervisor_journal.global_usage WHERE id = 1 FOR UPDATE").fetchone()
        w = self._check_fence(cur)
        s = cur.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s FOR UPDATE",
                        (root, claim_key)).fetchone()
        if exp is None:
            exp = s["next_seq"] if s else 1
        b = bounds_of(g)
        if s is not None and s["state"] != "compacted":
            # Validate BEFORE anything else, including the idempotent-duplicate answer.
            _, bad = read_stream_records(cur, root, claim_key, s, b.per_stream)
            if bad:
                raise JournalRefused("corrupt", bad)
        digest = intent_digest(self.writer, root, claim_key, kind, data, exp)
        existing = cur.execute("SELECT record_id::text AS record_id, root, claim_key, seq, kind, writer_id, at_json, data, "
                               "intent_digest FROM supervisor_journal.records WHERE record_id = %s", (rid,)).fetchone()
        if existing is not None:
            if (existing["intent_digest"] == digest and existing["seq"] == exp and existing["writer_id"] == self.writer
                    and (existing["root"], existing["claim_key"], existing["kind"]) == (root, claim_key, kind)):
                return to_record(existing), False           # identical resend: no-op
            raise JournalRefused("record_id_conflict", "a different record already uses this id")
        new_stream = s is None
        if new_stream:
            if kind != "claim_intent":
                raise JournalRefused("no_stream", f"{kind} before claim_intent")
            if w["unresolved"] >= b.unresolved_streams:
                raise JournalRefused("unresolved_cap")
            s = {"writer_id": self.writer, "state": "active", "count": 0, "bytes": 0, "reserved": 0, "fault_reserved": 0,
                 "outstanding": [], "next_seq": 1, "head_hash": GENESIS}
        else:
            if s["writer_id"] != self.writer:
                raise JournalRefused("foreign_writer", f"stream owned by {s['writer_id']}")
            if s["state"] != "active":
                raise JournalRefused("resolved", "stream is resolved")
        if s["next_seq"] != exp:
            raise JournalRefused("seq_mismatch", f"expected seq {exp}, stream is at {s['next_seq']}")
        seq = s["next_seq"]
        at_json = json.dumps(self.now)
        outstanding = [list(o) for o in s["outstanding"]]
        count, nbytes, reserved, fault_reserved = s["count"], s["bytes"], s["reserved"], s["fault_reserved"]

        consumes = None
        if kind in TERMINALS:
            consumes = next((o for o in outstanding if kind in o), None)
            if consumes is None:
                raise JournalRefused("no_open_intent", f"{kind} without a matching intent")
        elif kind in RESERVE_KINDS and fault_reserved > 0:
            consumes = "fault"

        def encode() -> str:
            _check_payload(data)
            text = json.dumps(data, sort_keys=True, separators=(",", ":"))
            if encoded_size(kind, self.writer, root, claim_key, seq, self.now, text) > RECORD_MAX_BYTES:
                raise JournalRefused("record_cap", kind)
            return text

        prev = s["head_hash"]

        def budget(text: str) -> int:
            # The metadata size depends on the hashes only through their fixed width.
            return (encoded_size(kind, self.writer, root, claim_key, seq, self.now, text)
                    + durable_meta_bytes(rid, self.epoch, digest, prev, GENESIS))

        evict: list = []
        if consumes is not None:
            try:
                text = encode()
            except JournalRefused as e:
                text = _fallback_text(e.code)        # never refused: fits the reserved worst case
            if consumes == "fault":
                fault_reserved -= 1
            else:
                consumes.remove(kind)
                outstanding = [o for o in outstanding if o]
            reserved -= 1
            add_count, add_bytes = 0, budget(text) - BUDGET_RECORD_MAX     # fills an already-charged slot
        else:
            text = encode()
            if kind in INTENTS:
                if worst_reserved_record_size(self.writer, root, claim_key) > RECORD_MAX_BYTES:
                    raise JournalRefused("metadata_headroom", "writer/root/key leave no room for a reserved outcome")
                terminals = list(INTENTS[kind])
                extra_fault = FAULT_RESERVE - fault_reserved
                slots = len(terminals) + extra_fault
                if (seq - 1) + reserved + 1 + slots > MAX_SEQ:
                    raise JournalRefused("seq_exhausted", kind)
                if count + 1 + slots > b.per_stream:
                    raise JournalRefused("stream_cap", f"{kind} needs {1 + slots} slots incl. reserved capacity")
                add_count, add_bytes = 1 + slots, budget(text) + slots * BUDGET_RECORD_MAX
                evict = self._plan_room(cur, g, add_count, add_bytes)
                reserved += slots
                fault_reserved += extra_fault
                outstanding.append(terminals)
            else:
                if (seq - 1) + reserved + 1 > MAX_SEQ:
                    raise JournalRefused("seq_exhausted", kind)
                if count + 1 > b.per_stream:
                    raise JournalRefused("stream_cap", kind)
                add_count, add_bytes = 1, budget(text)
                evict = self._plan_room(cur, g, add_count, add_bytes)

        row = {"record_id": rid, "root": root, "claim_key": claim_key, "seq": seq, "kind": kind, "writer_id": self.writer,
               "writer_epoch": self.epoch, "at_json": at_json, "data": text, "intent_digest": digest}
        row["budget_bytes"] = budget(text)
        own = record_hash(prev, row)

        if evict:
            self._apply_room(cur, evict)
        if new_stream:
            cur.execute("INSERT INTO supervisor_journal.streams (root, claim_key, writer_id, state, count, bytes, reserved, "
                        "fault_reserved, outstanding, next_seq, head_hash) VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, %s)",
                        (root, claim_key, self.writer, count + add_count, nbytes + add_bytes, reserved, fault_reserved,
                         Jsonb(outstanding), seq + 1, own))
            cur.execute("UPDATE supervisor_journal.writer_usage SET unresolved = unresolved + 1 WHERE writer_id = %s", (self.writer,))
        else:
            cur.execute("UPDATE supervisor_journal.streams SET count = %s, bytes = %s, reserved = %s, fault_reserved = %s, "
                        "outstanding = %s, next_seq = %s, head_hash = %s WHERE root = %s AND claim_key = %s",
                        (count + add_count, nbytes + add_bytes, reserved, fault_reserved, Jsonb(outstanding), seq + 1, own,
                         root, claim_key))
        cur.execute("INSERT INTO supervisor_journal.records (record_id, root, claim_key, seq, kind, writer_id, writer_epoch, at_json, "
                    "data, version, intent_digest, prev_hash, record_hash, budget_bytes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s)",
                    (rid, root, claim_key, seq, kind, self.writer, self.epoch, at_json, text, VERSION, digest, prev, own,
                     row["budget_bytes"]))
        cur.execute("UPDATE supervisor_journal.global_usage SET count = count + %s, bytes = bytes + %s WHERE id = 1",
                    (add_count, add_bytes))
        return Record(seq, kind, self.writer, root, claim_key, self.now, text), True

    # --- global room: PROSPECTIVE plan of resolved data only; applied only when admission succeeds ----
    def _plan_room(self, cur, g: dict[str, Any], need_count: int, need_bytes: int) -> list:
        c, b = g["count"], g["bytes"]

        def fits() -> bool:
            return c + need_count <= g["global_records"] and b + need_bytes <= g["global_bytes"]
        plan: list = []
        if fits():
            return plan
        for st in cur.execute("SELECT * FROM supervisor_journal.streams WHERE state = 'compacted' "
                              "ORDER BY resolved_at, root, claim_key LIMIT %s FOR UPDATE", (g["global_records"],)).fetchall():
            if summary_problem(cur, st):
                continue                                 # a corrupt summary is retained, never evicted
            c -= st["count"]
            b -= st["bytes"]
            plan.append(("drop", st, None))
            if fits():
                return plan
        for st in cur.execute("SELECT * FROM supervisor_journal.streams WHERE state = 'resolved' "
                              "ORDER BY resolved_at, root, claim_key LIMIT %s FOR UPDATE", (g["global_records"],)).fetchall():
            records, bad = read_stream_records(cur, st["root"], st["claim_key"], st, g["per_stream"])
            if bad:
                continue                                 # corrupt streams are retained, never evicted
            summary = self._summary(st, records)
            sb = summary_budget(summary, st["head_hash"])
            c += 1 - st["count"]
            b += sb - st["bytes"]
            plan.append(("compact", st, summary))
            if fits():
                return plan
        raise JournalRefused("global_cap", "no resolved data left to evict")

    @staticmethod
    def _summary(st: dict[str, Any], records: list[Record]) -> str:
        return ModelJournal._summary_text(Stream(st["root"], st["claim_key"], st["writer_id"], records, resolved_at=st["resolved_at"]))

    @staticmethod
    def _apply_room(cur, plan: list, mode: str = "evict") -> tuple[int, int]:
        cur.execute("SELECT set_config('supervisor_journal.compaction', %s, true)", (mode,))
        dc = db = 0
        for action, st, summary in plan:
            if action == "drop":
                cur.execute("DELETE FROM supervisor_journal.summaries WHERE root = %s AND claim_key = %s", (st["root"], st["claim_key"]))
                cur.execute("DELETE FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s", (st["root"], st["claim_key"]))
                dc -= st["count"]
                db -= st["bytes"]
            else:
                sb = summary_budget(summary, st["head_hash"])
                cur.execute("INSERT INTO supervisor_journal.summaries (root, claim_key, writer_id, resolved_at, summary, head_hash) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (st["root"], st["claim_key"], st["writer_id"], st["resolved_at"], summary, st["head_hash"]))
                cur.execute("DELETE FROM supervisor_journal.records WHERE root = %s AND claim_key = %s", (st["root"], st["claim_key"]))
                cur.execute("UPDATE supervisor_journal.streams SET state = 'compacted', count = 1, bytes = %s WHERE root = %s "
                            "AND claim_key = %s", (sb, st["root"], st["claim_key"]))
                dc += 1 - st["count"]
                db += sb - st["bytes"]
        cur.execute("UPDATE supervisor_journal.global_usage SET count = count + %s, bytes = bytes + %s WHERE id = 1", (dc, db))
        cur.execute("SELECT set_config('supervisor_journal.compaction', '', true)")
        return dc, db

    # --- resolution and compaction -------------------------------------------------------
    def resolve(self, root: str, claim_key: str, *, planstore_decided: bool = False) -> None:
        """Explicit terminal evidence only (the E2a predicates). Releases the remaining reservations."""
        self._usable()
        try:
            with self.conn.cursor() as cur:
                g = cur.execute("SELECT * FROM supervisor_journal.global_usage WHERE id = 1 FOR UPDATE").fetchone()
                self._check_fence(cur)
                s = cur.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s FOR UPDATE",
                                (root, claim_key)).fetchone()
                if s is None or s["writer_id"] != self.writer:
                    raise JournalRefused("foreign_writer" if s else "no_stream")
                if s["state"] != "active":
                    self.conn.rollback()
                    return
                records, bad = read_stream_records(cur, root, claim_key, s, g["per_stream"])
                if bad:
                    raise JournalRefused("corrupt", bad)
                if not (is_confirmed_finish(records) and planstore_decided and not s["outstanding"]) and not is_terminal_resolution(records):
                    raise JournalRefused("unresolved_outstanding", "needs a confirmed finish + decision, or a terminal operator resolution")
                release = s["reserved"] * BUDGET_RECORD_MAX
                cur.execute("UPDATE supervisor_journal.streams SET state = 'resolved', resolved_at = %s, count = count - reserved, "
                            "bytes = bytes - %s, reserved = 0, fault_reserved = 0, outstanding = '[]'::jsonb "
                            "WHERE root = %s AND claim_key = %s", (self.now, release, root, claim_key))
                cur.execute("UPDATE supervisor_journal.global_usage SET count = count - %s, bytes = bytes - %s WHERE id = 1",
                            (s["reserved"], release))
                cur.execute("UPDATE supervisor_journal.writer_usage SET unresolved = unresolved - 1 WHERE writer_id = %s", (self.writer,))
            self.conn.commit()
        except JournalRefused:
            self._safe_rollback()
            raise
        except psycopg.Error as e:
            self._break()
            raise JournalRefused("connection_lost", type(e).__name__) from e


def compact(dsn: str, now: float) -> dict[str, int]:
    """Guarded compaction transaction (any operator): resolved streams older than D become summaries
    (corrupt ones are retained), summaries older than R are dropped. Never touches unresolved data."""
    if not _valid_clock(now):
        raise JournalRefused("clock")
    retained: list[tuple[str, str, str]] = []
    with connect(dsn) as conn, conn.cursor() as cur:
        g = cur.execute("SELECT * FROM supervisor_journal.global_usage WHERE id = 1 FOR UPDATE").fetchone()
        cur.execute("SELECT set_config('supervisor_journal.now', %s, true)", (json.dumps(now),))
        plan: list = []
        for st in cur.execute("SELECT * FROM supervisor_journal.streams WHERE state = 'resolved' AND %s - resolved_at >= %s "
                              "ORDER BY root, claim_key FOR UPDATE", (now, g["compact_after"])).fetchall():
            records, bad = read_stream_records(cur, st["root"], st["claim_key"], st, g["per_stream"])
            if bad:
                retained.append((st["root"], st["claim_key"], bad))
            else:
                plan.append(("compact", st, DurableJournal._summary(st, records)))
        for st in cur.execute("SELECT * FROM supervisor_journal.streams WHERE state = 'compacted' AND %s - resolved_at >= %s "
                              "ORDER BY root, claim_key FOR UPDATE", (now, g["summary_retention"])).fetchall():
            bad = summary_problem(cur, st)
            if bad:
                retained.append((st["root"], st["claim_key"], bad))
            else:
                plan.append(("drop", st, None))
        DurableJournal._apply_room(cur, plan, mode="on")
        conn.commit()
    # Corrupt streams are RETAINED with explicit evidence of why; nothing about them is dropped.
    return {"compacted": sum(1 for p in plan if p[0] == "compact"), "dropped": sum(1 for p in plan if p[0] == "drop"),
            "retained_corrupt": retained}
