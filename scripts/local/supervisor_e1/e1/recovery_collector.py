"""Read-only journal collector (HK-ISSUE-015 manifest, second increment; root msgs 2346/2348/2349/2350/2351).

collect_observation(dsn, writer, deadline_s=120.0) -> dict reads ONE writer's journal streams from a live
supervisor_journal database and returns a sanitized `hekate-journal-observation.v0` dict -- exactly the input of
e1.recovery_manifest.project. It is a LIBRARY function only: no locator, no CLI, no file, no print, no write.

CONTRACT (root review 2350/2351):
- One connection: autocommit, dict rows, connect_timeout=10, options default_transaction_read_only=on,
  statement_timeout=30000, lock_timeout=5000; ONE `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` transaction;
  ROLLBACK and close always run (finally).
- Queries, in order: Q1 per_stream (a missing row or a value outside 1..PER_STREAM_MAX refuses read_failed BEFORE
  any stream is read); Q2 writer epoch (none -> writer_unknown); Q3 the writer's streams with DB-side caps (root and
  claim_key <= 1024 octets, head_hash exactly 64, outstanding <= 65536 octets), LIMIT 65 (65 -> input_overflow); per
  stream: the EXISTING durable.read_stream_records (active/resolved) or durable.summary_problem plus one identical
  bounded summary select (compacted); then, only when the reader returned no problem, ONE bounded seq/record_hash
  query bound to the reader's records.
- A stream HEADER that cannot satisfy the projection (identity over cap or pattern-invalid, outstanding not a list of
  lists of terminals, resolvedAt inconsistent with state) refuses the WHOLE collection (read_failed): a stream
  identity is never invented. A stream whose RECORDS or fields cannot be read or sanitized is kept with `corrupt` set
  (the reader's code, "summary_*", "hash_bind:<step>" or "sanitize:<field>") and records [] -- the projection lists it
  unlistable. A compacted stream with a summary problem carries the placeholder summary {kindsTail: [], resolution: null}.
- Sanitization is a whitelist: stream {root, claimKey, state, outstanding, headHash, resolvedAt, corrupt}; record
  {seq, kind, hash, fields}; fields = operator_resolution {decision, reconciliationRef} plus invalidPayload:true for a
  degraded record. Raw payload text, at_json, intent_digest, writer_epoch, record_id and the DSN never enter the result.
  Values are validated with e1.recovery_manifest's own patterns and are never rewritten.
- The canonical observation size is counted INCREMENTALLY and exactly (header + every stream + separating commas);
  over OBSERVATION_MAX it refuses input_overflow before any further stream is read.
- SOFT DEADLINE ONLY: no query is issued once deadline_s has elapsed (collector_timeout), and a watchdog requests
  cancellation of the running statement (conn.cancel()). connect_timeout, statement_timeout and lock_timeout bound
  individual operations, but cancellation, connection teardown and result draining can themselves block: NO bound on
  the call's total duration is guaranteed.
- Errors are typed (CollectorRefused) and carry only an exception TYPE name, never a message that could echo the DSN.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from e1 import recovery_manifest as RM
from e1.durable import SUMMARY_MAX_BYTES, read_stream_records, summary_problem
from e1.evidence import TERMINALS

ID_CAP = 1024                 # root / claim_key octets selected; longer -> NULL -> whole read_failed
OUTSTANDING_CAP = 65536       # outstanding::text octets selected
OBSERVATION_MAX = 16 << 20    # canonical observation bytes
CONNECT_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=5000"

_Q3 = """
SELECT CASE WHEN octet_length(root) <= %(cap)s THEN root END AS root,
       CASE WHEN octet_length(claim_key) <= %(cap)s THEN claim_key END AS claim_key,
       writer_id, state, count, bytes, reserved, fault_reserved, next_seq, resolved_at,
       CASE WHEN octet_length(head_hash) = 64 THEN head_hash END AS head_hash,
       CASE WHEN octet_length(outstanding::text) <= %(ocap)s THEN outstanding END AS outstanding
  FROM supervisor_journal.streams WHERE writer_id = %(w)s ORDER BY root, claim_key LIMIT %(lim)s"""
_HASHES = """
SELECT seq, CASE WHEN octet_length(record_hash) = 64 THEN record_hash END AS h
  FROM supervisor_journal.records WHERE root = %(r)s AND claim_key = %(k)s ORDER BY seq LIMIT %(lim)s"""
_SUMMARY = ("SELECT CASE WHEN octet_length(summary) <= %(max)s THEN summary END AS summary "
            "FROM supervisor_journal.summaries WHERE root = %(r)s AND claim_key = %(k)s")


class CollectorRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _canon(v: Any) -> bytes:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _ok(v: Any, pattern) -> bool:
    return isinstance(v, str) and RM._encodable(v) and bool(pattern.fullmatch(v))


def _header(i: int, row: dict[str, Any]) -> dict[str, Any]:
    """The projection header of one stream, or the WHOLE collection refuses (identity is never invented)."""
    def bad(field: str) -> CollectorRefused:
        return CollectorRefused("read_failed", f"stream {i}: {field}")
    if not _ok(row["root"], RM.IDENT):
        raise bad("root")
    if not _ok(row["claim_key"], RM.IDENT):
        raise bad("claim_key")
    if row["state"] not in RM.STATES:
        raise bad("state")
    if not _ok(row["head_hash"], RM.HEX64):
        raise bad("head_hash")
    out = row["outstanding"]
    if not (isinstance(out, list) and all(isinstance(o, list) and 1 <= len(o) <= RM.TERMINALS_PER_INTENT_MAX
                                          and all(isinstance(k, str) and k in TERMINALS for k in o) for o in out)):
        raise bad("outstanding")
    ra = row["resolved_at"]
    if (ra is None) != (row["state"] == "active") or (ra is not None and not (isinstance(ra, (int, float)) and ra >= 0)):
        raise bad("resolved_at")
    return {"root": row["root"], "claimKey": row["claim_key"], "state": row["state"], "outstanding": [list(o) for o in out],
            "headHash": row["head_hash"], "resolvedAt": ra, "corrupt": None, "records": [], "summary": None}


def _fields(rec) -> dict[str, Any] | str:
    """Whitelisted record fields, or a sanitize problem code (the stream is then kept as corrupt)."""
    f: dict[str, Any] = {}
    if rec.degraded:
        f["invalidPayload"] = True
    if rec.kind == "operator_resolution" and not rec.degraded:
        p = rec.payload
        for key, code, pattern in (("decision", "decision", RM.DECISION), ("reconciliationRef", "reconciliation_ref", RM.REF)):
            if key in p:
                if not _ok(p[key], pattern):
                    return f"sanitize:{code}"                  # lowercase: corrupt codes must match the projection's CODE
                f[key] = p[key]
    return f


def collect_observation(dsn: str, writer: str, *, deadline_s: float = 120.0, now: Callable[[], float] = time.monotonic,
                        _before_query: Callable[[Any], None] | None = None) -> dict[str, Any]:
    """The sanitized observation of ONE writer (see the module contract). `_before_query` is a TEST hook only."""
    if not _ok(writer, RM.IDENT):
        raise CollectorRefused("read_failed", "writer id")
    t0 = now()
    try:
        conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=10, options=CONNECT_OPTIONS)
    except Exception as e:  # noqa: BLE001 -- typed; the message (which can echo the DSN) is never kept
        raise CollectorRefused("connect_failed", type(e).__name__) from None
    timer = threading.Timer(max(0.0, deadline_s - (now() - t0)), _cancel, args=(conn,))
    timer.daemon = True
    timer.start()
    cur = conn.cursor()

    def before() -> None:
        if now() - t0 >= deadline_s:
            raise CollectorRefused("collector_timeout", "soft deadline reached; no further query issued")
        if _before_query is not None:
            _before_query(cur)

    try:
        before()
        cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        before()
        g = cur.execute("SELECT per_stream FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
        per_stream = g["per_stream"] if g else None
        if not (isinstance(per_stream, int) and not isinstance(per_stream, bool) and 1 <= per_stream <= RM.PER_STREAM_MAX):
            raise CollectorRefused("read_failed", "per_stream")
        before()
        w = cur.execute("SELECT epoch FROM supervisor_journal.writer_usage WHERE writer_id = %s", (writer,)).fetchone()
        if w is None:
            raise CollectorRefused("writer_unknown")
        before()
        rows = cur.execute(_Q3, {"cap": ID_CAP, "ocap": OUTSTANDING_CAP, "w": writer, "lim": RM.STREAMS_MAX + 1}).fetchall()
        if len(rows) > RM.STREAMS_MAX:
            raise CollectorRefused("input_overflow", f"> {RM.STREAMS_MAX} streams")
        headers = [_header(i, r) for i, r in enumerate(rows)]
        obs: dict[str, Any] = {"schema": RM.INPUT_SCHEMA, "writer": {"id": writer, "epoch": w["epoch"]},
                               "bounds": {"perStream": per_stream}, "streams": []}
        total = len(_canon(obs))                                  # includes the empty "[]"; each stream adds itself + a comma
        for row, s in zip(rows, headers):
            if s["state"] == "compacted":
                before()
                problem = summary_problem(cur, row)
                summary = None
                if problem is None:
                    before()
                    srow = cur.execute(_SUMMARY, {"max": SUMMARY_MAX_BYTES, "r": row["root"], "k": row["claim_key"]}).fetchone()
                    summary, problem = _summary(srow)
                s["corrupt"], s["summary"] = problem, summary or {"kindsTail": [], "resolution": None}
            else:
                before()
                records, bad = read_stream_records(cur, row["root"], row["claim_key"], row, per_stream)
                if bad is None:
                    before()
                    hashes = cur.execute(_HASHES, {"r": row["root"], "k": row["claim_key"], "lim": per_stream + 1}).fetchall()
                    bad = _bind(records, hashes, row["head_hash"])
                if bad is None:
                    out = []
                    for rec, h in zip(records, hashes):
                        f = _fields(rec)
                        if isinstance(f, str):
                            bad = f
                            break
                        out.append({"seq": rec.seq, "kind": rec.kind, "hash": h["h"], "fields": f})
                s["corrupt"], s["records"] = (bad, []) if bad is not None else (None, out)
            total += len(_canon(s)) + (1 if obs["streams"] else 0)
            if total > OBSERVATION_MAX:
                raise CollectorRefused("input_overflow", f"observation > {OBSERVATION_MAX} bytes")
            obs["streams"].append(s)
        try:
            RM._validate(obs)                                     # the collector's output must be valid projection input
        except RM.ProjectionRefused as e:
            raise CollectorRefused("read_failed", f"observation invalid: {e.code}") from None
        return obs
    except CollectorRefused:
        raise
    except psycopg.Error as e:
        if now() - t0 >= deadline_s:
            raise CollectorRefused("collector_timeout", type(e).__name__) from None
        raise CollectorRefused("read_failed", type(e).__name__) from None
    finally:
        timer.cancel()
        for step in (lambda: cur.execute("ROLLBACK"), conn.close):
            try:
                step()
            except Exception:  # noqa: BLE001 -- cleanup never masks the outcome nor echoes credentials
                pass


def _cancel(conn) -> None:
    try:
        conn.cancel()                                             # a REQUEST; it may itself block or fail
    except Exception:  # noqa: BLE001
        pass


def _bind(records, hashes, head: str) -> str | None:
    """Bind the hash list to the reader's validated records: same count, same contiguous seqs, hex64, last == head."""
    if len(hashes) != len(records):
        return "hash_bind:count"
    for i, (rec, h) in enumerate(zip(records, hashes), 1):
        if h["seq"] != i or rec.seq != i:
            return "hash_bind:seq"
        if not _ok(h["h"], RM.HEX64):
            return "hash_bind:format"
    if hashes and hashes[-1]["h"] != head:
        return "hash_bind:head"
    return None


def _summary(srow) -> tuple[dict[str, Any] | None, str | None]:
    if srow is None or srow["summary"] is None:
        return None, "summary_size"
    try:
        doc = json.loads(srow["summary"])
    except ValueError:
        return None, "summary_format"
    kinds = doc.get("kinds") if isinstance(doc, dict) else None
    if not (isinstance(kinds, list) and all(isinstance(k, str) and k in RM.KINDS for k in kinds)):
        return None, "summary_format"
    res = doc.get("resolution")
    resolution = None
    if res is not None:
        if not (isinstance(res, dict) and _ok(res.get("decision"), RM.DECISION) and _ok(res.get("reconciliationRef"), RM.REF)):
            return None, "sanitize:summary_resolution"
        resolution = {"decision": res["decision"], "reconciliationRef": res["reconciliationRef"]}
    return {"kindsTail": kinds[-RM.TAIL_MAX:], "resolution": resolution}, None
