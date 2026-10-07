"""E2b-a bounded, read-only recovery scan (plan 028 §4, §8, §9 checks 9-11; TEST-ONLY).

SHARED COHERENT-READ ADAPTER EXPERIMENT, not a production proof path: per stream, ONE
REPEATABLE READ READ ONLY transaction reads the journal stream, the claim receipt, the node's
plan_node_state row and that node's events by direct table access on the disposable database.

Contract (msgs 930/941):
- Reads are bounded AT THE DATABASE: rows per page, a total event-row cap E and a byte budget Z.
  An incomplete read is proof_missing, never proof.
- Every fact used in one classification comes from that ONE snapshot. The review class is derived
  inside the snapshot from the node row; where the snapshot cannot decide (an accepted/rejected
  decision whose prerequisite pins need the whole graph) it is "decided_unverified" and needs an
  operator, never a guess. No independent read is mixed in.
- A review is current only if its full key (root, node, attemptId, epoch, artifact) equals the
  snapshot's node; any other key is moot.
- One scan call handles at most `max_streams` streams and returns a cursor to continue; the
  operator queue is bounded by count and bytes with every entry charged at a fixed worst case, so
  an existing entry is always refreshed in place and never advertises stale permissions; overflow
  is counted, with at most a fixed number of keys retained.
- Classification reuses e1/evidence.classify, e1/coherent.prove_finish and ReviewWorkflow.
  Nothing is written anywhere and nothing is persisted as task truth.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import psycopg
from psycopg.rows import dict_row

from e1.coherent import CoherentFacts, prove_finish
from e1.durable import CONNECT_OPTIONS, _valid_clock, read_stream_records
from e1.evidence import MAX_AT, Classification, Facts, Record, ReviewWorkflow, classify

_EVENTS_PAGE_SQL = """
SELECT t.seq, t.j, t.page_rows FROM (
    SELECT p.seq, p.j, count(*) OVER () AS page_rows, sum(octet_length(p.j)) OVER (ORDER BY p.seq) AS cum
    FROM (SELECT e.seq, row_to_json(e)::text AS j FROM public.plan_attempt_events e
           WHERE e.node_id = %(node)s AND e.seq > %(after)s ORDER BY e.seq LIMIT %(limit)s) p
) t WHERE t.cum <= %(budget)s ORDER BY t.seq
"""


def _positive_int(name: str, v: object) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValueError(f"{name} must be a positive int, got {v!r}")


@dataclass(frozen=True)
class ReadBounds:
    streams_page: int = 16        # P (streams per page)
    max_streams: int = 64         # total streams handled by ONE scan call (continue with the cursor)
    events_max: int = 256         # E (total event rows per stream read)
    events_bytes: int = 1 << 18   # Z (total event bytes per stream read)
    events_page: int = 64         # rows per fetch

    def __post_init__(self) -> None:
        # Validated before any database access: a zero page would make the scan loop non-advancing.
        for n in ("streams_page", "max_streams", "events_max", "events_bytes", "events_page"):
            _positive_int(n, getattr(self, n))


@dataclass
class StreamFacts:
    root: str
    claim_key: str
    writer: str
    records: list[Record]
    corrupt: str | None
    receipt: dict[str, Any] | None
    node_id: str | None
    coherent: CoherentFacts | None


def _uuid(v: Any) -> str | None:
    try:
        return str(uuid.UUID(str(v)))
    except (ValueError, TypeError):
        return None


def read_connection(dsn: str) -> psycopg.Connection:
    """Autocommit session: every snapshot is an EXPLICIT BEGIN ... COMMIT below."""
    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS)


def _read_events(cur, node_id: str, rb: ReadBounds) -> tuple[list[dict[str, Any]], bool]:
    events: list[dict[str, Any]] = []
    after, used = -1, 0
    while True:
        limit = min(rb.events_page, rb.events_max - len(events) + 1)
        rows = cur.execute(_EVENTS_PAGE_SQL, {"node": node_id, "after": after, "limit": limit,
                                              "budget": rb.events_bytes - used}).fetchall()
        if rows and len(rows) < rows[0]["page_rows"]:
            return events, False                         # byte budget Z exhausted inside this page
        for r in rows:
            events.append(json.loads(r["j"]))
            used += len(r["j"].encode("utf-8"))
        if len(events) > rb.events_max:
            return events[: rb.events_max], False       # row cap E exceeded
        if not rows or rows[0]["page_rows"] < limit:
            return events, True
        after = rows[-1]["seq"]


def read_stream_coherent(conn: psycopg.Connection, root: str, claim_key: str, rb: ReadBounds) -> StreamFacts:
    """One REPEATABLE READ READ ONLY snapshot of the journal stream and its PlanStore facts."""
    if not conn.autocommit:
        raise ValueError("use read_connection(): the snapshot must be an explicit transaction")
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        cur = conn.cursor()
        g = cur.execute("SELECT per_stream FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
        s = cur.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s", (root, claim_key)).fetchone()
        records, corrupt = read_stream_records(cur, root, claim_key, s, g["per_stream"])
        root_id = _uuid(root)
        receipt = None
        if root_id:
            receipt = cur.execute("SELECT node_id::text AS node_id, attempt_id, attempt_epoch, executor_ref, actor, content_revision, "
                                  "prereq_digest, outcome FROM public.plan_claim_receipts WHERE root_node_id = %s AND claim_key = %s",
                                  (root_id, claim_key)).fetchone()
        claimed = [r.payload for r in records if r.kind == "claimed" and not r.degraded]
        node_id = _uuid(claimed[-1].get("nodeId")) if claimed else None
        node_id = node_id or (receipt["node_id"] if receipt else None)
        coherent = None
        if node_id:
            node = cur.execute("SELECT row_to_json(s)::text AS j FROM public.plan_node_state s WHERE node_id = %s", (node_id,)).fetchone()
            events, complete = _read_events(cur, node_id, rb)
            count = cur.execute("SELECT count(*) AS n FROM public.plan_attempt_events WHERE node_id = %s", (node_id,)).fetchone()["n"]
            coherent = CoherentFacts(json.loads(node["j"]) if node else None, events, count, complete and len(events) == count)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return StreamFacts(root, claim_key, s["writer_id"] if s else "", records, corrupt, receipt, node_id, coherent)


def snapshot_review(node: dict[str, Any] | None) -> str | None:
    """The GLOBAL review fact as far as the node row in the SAME snapshot can decide it (mirrors the
    raw part of PlanRules.EffectiveAcceptanceOf). A standing decision may still be stale through
    prerequisite pins, which need the whole graph: reported as decided_unverified, never guessed."""
    if node is None:
        return None
    if node.get("work_status") != "done":
        return "not_done"
    if node.get("acc_decision") is None:
        return "candidate"
    if (node.get("acc_content_revision") != node.get("content_revision") or node.get("acc_artifact_ref") != node.get("artifact_ref")
            or node.get("acc_attempt_epoch") != node.get("attempt_epoch")):
        return "operator_classification"
    return "decided_unverified"


def facts_for(sf: StreamFacts, reader: str) -> tuple[Facts, bool]:
    """Classifier facts from one coherent stream read. Returns (facts, node_done_for_attempt)."""
    recs = sf.records
    intents = [r.payload for r in recs if r.kind == "claim_intent"]
    matches = None
    if sf.receipt is not None and intents:
        held = intents[0]
        matches = (sf.receipt["attempt_id"], sf.receipt["executor_ref"], sf.receipt["actor"]) == (
            held.get("attemptId"), held.get("executorRef"), held.get("actor"))
    claimed = [r.payload for r in recs if r.kind == "claimed" and not r.degraded]
    node = sf.coherent.node if sf.coherent else None
    done_for = bool(node and claimed and node.get("work_status") == "done"
                    and node.get("attempt_id") == claimed[-1].get("attemptId")
                    and node.get("attempt_epoch") == claimed[-1].get("attemptEpoch"))
    proof = None
    fin = [r.payload for r in recs if r.kind == "finish_intent" and not r.degraded]
    if fin and sf.coherent is not None and sf.receipt is not None:
        pkg = SimpleNamespace(root_id=sf.root, node_id=sf.node_id, content_revision=sf.receipt["content_revision"],
                              prereq_digest=sf.receipt["prereq_digest"])
        proof = prove_finish(sf.coherent, fin[-1]["held"], pkg)
    elif fin:
        proof = "proof_missing"
    acknowledged = any(r.kind == "review_acknowledged" for r in recs)
    return Facts(reader=reader, claim_found=sf.receipt is not None, claim_receipt_matches=matches, finish_proof=proof,
                 node_done_for_attempt=done_for, review_class=snapshot_review(node), acknowledged=acknowledged), done_for


def classify_stream(sf: StreamFacts, reader: str) -> Classification:
    if sf.writer and sf.writer != reader:
        return Classification("foreign", ("other_writer_state",), ("operator_resolve_foreign_stream",))
    if sf.corrupt:
        # Never repaired, truncated or compacted; incomplete/corrupt evidence is never proof.
        return Classification(f"proof_missing:{sf.corrupt}", ("journal_integrity",), ("operator_reconcile",))
    facts, _ = facts_for(sf, reader)
    c = classify(sf.records, facts)
    if facts.review_class == "decided_unverified" and c.label in ("review:moot", "C8:decided_unverified"):
        # The snapshot cannot tell accepted/rejected from stale-by-prerequisites: an operator confirms.
        base = "review" if c.label.startswith("review") else "C8"
        return Classification(f"{base}:decided_unverified", ("prerequisite_pins",), ("operator_confirm_decision",),
                              planstore_review=c.planstore_review)
    return c


def current_review(sf: StreamFacts, reader: str):
    """`current(review_key)` for ReviewWorkflow.wake, from the SAME snapshot: only the exact current
    (root, node, attemptId, epoch, artifact) of a node Done for this stream's attempt can be live."""
    facts, done_for = facts_for(sf, reader)
    node = sf.coherent.node if sf.coherent else None
    here = (sf.root, sf.node_id, node.get("attempt_id"), node.get("attempt_epoch"), node.get("artifact_ref")) if node else None
    cls = facts.review_class
    live = "operator_classification" if cls in ("operator_classification", "decided_unverified") else cls

    def current(k) -> str:
        return live if (done_for and tuple(k) == here) else "moot"
    return current


# --- bounded operator queue (read model) --------------------------------------------------------

LABEL_MAX = 96
ALLOWED_MAX = 8
ACTION_MAX = 64
ENTRY_CONTENT_MAX = len(json.dumps({"allowed": ["x" * ACTION_MAX] * ALLOWED_MAX, "label": "x" * LABEL_MAX},
                                   separators=(",", ":")).encode("utf-8"))


def _entry(c: Classification) -> str:
    label, allowed = c.label, list(c.allowed)
    if len(label.encode("utf-8")) > LABEL_MAX or len(allowed) > ALLOWED_MAX or any(len(a.encode("utf-8")) > ACTION_MAX for a in allowed):
        label, allowed = "oversize_classification", ["operator_reconcile"]
    return json.dumps({"allowed": allowed, "label": label}, sort_keys=True, separators=(",", ":"))


@dataclass
class OperatorQueue:
    """Each entry is charged its stream-key bytes plus ENTRY_CONTENT_MAX, so refreshing an existing
    entry always fits: an entry is never left advertising an older classification's permissions."""
    max_entries: int                 # Q
    max_bytes: int                   # Y
    overflow_keys_max: int = 8
    entries: dict[tuple[str, str], str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _positive_int("max_entries", self.max_entries)
        _positive_int("max_bytes", self.max_bytes)
        if isinstance(self.overflow_keys_max, bool) or not isinstance(self.overflow_keys_max, int) or self.overflow_keys_max < 0:
            raise ValueError(f"overflow_keys_max must be a non-negative int, got {self.overflow_keys_max!r}")

    @staticmethod
    def charge(k: tuple[str, str]) -> int:
        return len(k[0].encode("utf-8")) + len(k[1].encode("utf-8")) + ENTRY_CONTENT_MAX

    def nbytes(self) -> int:
        return sum(self.charge(k) for k in self.entries)

    def refresh(self, results: dict[tuple[str, str], Classification]) -> dict[str, Any]:
        """Apply one bounded batch of results: resolved/moot entries leave; existing entries are
        updated in place (never evicted); new ones are added while within Q/Y. Overflow is counted
        with at most `overflow_keys_max` keys retained; the overflowed streams' journal evidence is
        untouched (this is only a view)."""
        overflow_count, overflow_keys = 0, []
        for k, c in results.items():
            if c.label in ("resolved", "C0") or c.label.startswith("review:moot"):
                self.entries.pop(k, None)
                continue
            if k in self.entries:
                self.entries[k] = _entry(c)                  # always fits: charged at the worst case
                continue
            if len(self.entries) + 1 > self.max_entries or self.nbytes() + self.charge(k) > self.max_bytes:
                overflow_count += 1
                if len(overflow_keys) < self.overflow_keys_max:
                    overflow_keys.append(k)
                continue
            self.entries[k] = _entry(c)
        return {"queued": len(self.entries), "overflow_count": overflow_count, "overflow_keys": overflow_keys,
                "alert": overflow_count > 0}

    def reconcile(self, conn: psycopg.Connection, chunk: int = 64) -> int:
        """Drop every existing entry whose stream is no longer ACTIVE (resolved, compacted, dropped),
        checked against the authoritative stream rows. Runs on EVERY scan call, independent of the
        scan cursor, so cleanup does not depend on a full pass fitting one call. Bounded: at most Q
        keys, queried `chunk` at a time. Returns the number of entries removed."""
        keys = list(self.entries)
        removed = 0
        for i in range(0, len(keys), chunk):
            part = keys[i:i + chunk]
            conn.execute("BEGIN READ ONLY")
            live = {(r["root"], r["claim_key"]) for r in conn.execute(
                "SELECT s.root, s.claim_key FROM supervisor_journal.streams s "
                "JOIN unnest(%s::text[], %s::text[]) AS k(root, claim_key) ON s.root = k.root AND s.claim_key = k.claim_key "
                "WHERE s.state = 'active'", ([k[0] for k in part], [k[1] for k in part])).fetchall()}
            conn.execute("COMMIT")
            for k in part:
                if k not in live:
                    del self.entries[k]
                    removed += 1
        return removed


# --- scan(now) --------------------------------------------------------------------------------------

@dataclass
class ScanResult:
    results: dict[tuple[str, str], Classification]
    notifications: list[tuple]
    queue: dict[str, Any]
    pages: int
    next_after: tuple[str, str] | None      # cursor to continue; None when the pass is complete


def scan(dsn: str, reader: str, now: float, *, rb: ReadBounds = ReadBounds(), queue: OperatorQueue,
         after: tuple[str, str] = ("", ""), ack_window: float = 10.0, progress_window: float = 20.0,
         _before_queue: Any = None) -> ScanResult:
    """Read-only, bounded scan of at most rb.max_streams active streams after `after` (P per page,
    keyset cursor). Each own stream is read in its own coherent snapshot and classified; other
    writers' streams are reported foreign. Returns INTENDED notifications (fake clock, original
    anchors) and the queue view (counted AFTER cleanup). Sends nothing, writes nothing."""
    if not isinstance(rb, ReadBounds) or not isinstance(queue, OperatorQueue):
        raise ValueError("rb must be ReadBounds and queue an OperatorQueue")
    if not _valid_clock(now):
        raise ValueError(f"now must be a finite, non-negative clock <= MAX_AT, got {now!r}")
    for n, v in (("ack_window", ack_window), ("progress_window", progress_window)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 < v <= MAX_AT):
            raise ValueError(f"{n} must be a positive finite number, got {v!r}")
    if not (isinstance(after, tuple) and len(after) == 2 and all(isinstance(x, str) for x in after)):
        raise ValueError("after must be a (root, claim_key) tuple of str")
    results: dict[tuple[str, str], Classification] = {}
    notes: list[tuple] = []
    pages = 0
    cursor = after
    complete = False
    with read_connection(dsn) as conn:
        notify_max = conn.execute("SELECT notify_max FROM supervisor_journal.global_usage WHERE id = 1").fetchone()["notify_max"]
        while len(results) < rb.max_streams:
            want = min(rb.streams_page, rb.max_streams - len(results))
            conn.execute("BEGIN READ ONLY")
            page = conn.execute("SELECT root, claim_key, writer_id FROM supervisor_journal.streams WHERE state = 'active' "
                                "AND (root, claim_key) > (%s, %s) ORDER BY root, claim_key LIMIT %s",
                                (cursor[0], cursor[1], want)).fetchall()
            conn.execute("COMMIT")
            pages += 1
            for st in page:
                k = (st["root"], st["claim_key"])
                cursor = k
                if st["writer_id"] != reader:
                    results[k] = Classification("foreign", ("other_writer_state",), ("operator_resolve_foreign_stream",))
                    continue
                sf = read_stream_coherent(conn, k[0], k[1], rb)
                results[k] = classify_stream(sf, reader)
                if not sf.corrupt and sf.writer == reader:
                    wf = ReviewWorkflow.rehydrate(sf.records, ack_window=ack_window, progress_window=progress_window,
                                                  notify_max=notify_max)
                    notes.extend(wf.wake(now, current_review(sf, reader)))
            if len(page) < want:
                complete = True
                break
        if _before_queue is not None:
            _before_queue()                              # TEST hook: state may change after the reads
        # Refresh FIRST (may insert entries from this call's possibly stale reads), THEN reconcile
        # every queued key against the authoritative stream rows, THEN count. Entries observed
        # inactive by cleanup cannot be reinserted from earlier reads; later changes await a scan.
        view = queue.refresh(results)
        view["removed_inactive"] = queue.reconcile(conn)
        view["queued"] = len(queue.entries)
    return ScanResult(results, notes, view, pages, None if complete else cursor)
