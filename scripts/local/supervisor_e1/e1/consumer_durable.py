"""E2e durable side (plan 034 rev 3 §6, §7; TEST-ONLY): the delivery from the stored candidate, the
ONE-snapshot revalidation read, and the bounded retrieval adapter. Read-only on every path.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg
from psycopg.rows import dict_row

from e1 import acts as A
from e1 import consumer as C
from e1 import handoff as H
from e1.acts_durable import read_facts
from e1.durable import CONNECT_OPTIONS, read_stream_records
from e1.handoff_durable import _aux


def delivery(dsn: str, handoff_id: str, receipt: dict[str, Any], h1_input: dict[str, Any]) -> C.Delivery:
    """The EXACT stored bytes of an accepted candidate, wrapped. Nothing is re-encoded."""
    with psycopg.connect(dsn, row_factory=dict_row, options=CONNECT_OPTIONS) as c:
        row = c.execute("SELECT candidate_digest, manifest, envelope, task FROM supervisor_journal.e2d_candidates WHERE handoff_id = %s",
                        (handoff_id,)).fetchone()
    if row is None:
        raise C.Refused("candidate_unavailable", handoff_id)
    return C.Delivery(C.WRAPPER, C.CODEC, row["candidate_digest"], row["manifest"].encode("utf-8"), row["envelope"].encode("utf-8"),
                      row["task"].encode("utf-8"), json.dumps(receipt).encode("utf-8"), json.dumps(h1_input).encode("utf-8"))


def fresh(dsn: str, v: C.Verified, unconfirmed: list[dict[str, Any]] = ()) -> C.Fresh:
    """ONE combined REPEATABLE READ snapshot (030 currentness rule) FOR the exact verified delivery
    (msg 1311): root, claim, review key, receipt and packageRef all come from `v`, and the result
    names the candidate it was read for. The receipt's status is evaluated INSIDE the snapshot with
    the E2d receipt_status logic, with the current binding, review class, pins and pending/queue."""
    t, receipt = v.manifest["transition"], v.receipt
    root, claim_key = t["root"], t["claimKey"]
    review_key = C.review_identity(v.manifest)
    package_ref = v.task["packageRef"]
    rid = A.review_id(review_key)
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            cur = conn.cursor()
            snap = cur.execute("SELECT txid_current_snapshot()::text AS s").fetchone()["s"]
            facts = read_facts(cur, root, review_key["nodeId"], (review_key["attemptId"], review_key["attemptEpoch"]), "A",
                               {"snapshot": snap}, claim_key)
            g = cur.execute("SELECT per_stream FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
            s = cur.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s", (root, claim_key)).fetchone()
            records, corrupt = read_stream_records(cur, root, claim_key, s, g["per_stream"])
            row = cur.execute("SELECT kind, seq, record_hash, data FROM supervisor_journal.records WHERE record_id = %s AND root = %s "
                              "AND claim_key = %s", (receipt["recordId"], root, claim_key)).fetchone()
            outstanding, _counters, queue = _aux(cur, root, claim_key)
        finally:
            conn.execute("ROLLBACK")
    view = A.View(records, corrupt)
    status = "mismatch" if corrupt else H.receipt_status(view, rid, receipt, row)
    chain = view.chain(rid)
    current = chain.current.link_id if chain.status == "ok" else None
    try:
        pending = H.pending_effects(records, corrupt, outstanding, list(unconfirmed))
    except H.Refused as e:
        raise C.Refused("uncertainty_unlistable", e.detail) from None
    return C.Fresh(receipt["candidateDigest"], receipt["recordId"], review_key, package_ref, status, current,
                   A.review_state(facts.node, review_key), H.pins_problem(H.pins_of(facts), package_ref),
                   pending, sorted(r for k, r in queue if k == rid), {"snapshot": snap, "revision": facts.revision})


def _matches(rec: Any, pointer: dict[str, Any]) -> bool:
    """The validated record IS the manifest's pointer: same seq, and for a progress pointer the same
    kind, checkpointId and evidenceDigest; for a binding pointer the same linkId."""
    if rec.seq != pointer["seq"] or rec.degraded:
        return False
    d = rec.payload
    if pointer["kind"] == "binding":
        lid = d.get("bind", {}).get("linkId") if rec.kind == "review_acknowledged" else d.get("linkId")
        return rec.kind in ("review_requested", "review_assigned", "review_rebind", "review_acknowledged") and lid == pointer.get("linkId")
    return (rec.kind == pointer["kind"] and d.get("checkpointId") == pointer.get("checkpointId")
            and d.get("evidenceDigest") == pointer.get("evidenceDigest"))


def retriever(dsn: str, v: C.Verified) -> C.Retriever:
    """Bound to the VERIFIED delivery's own stream (msg 1314). One call = one REPEATABLE READ snapshot
    in which the WHOLE stream is read and chain-validated (bounded by per_stream); the request must
    name this stream, and the record must lie in the validated prefix and match the FULL manifest
    pointer (msg 1311). Anything else, or a modified/substituted/unvalidated record, returns None."""
    root, claim_key = v.manifest["transition"]["root"], v.manifest["transition"]["claimKey"]

    def get(pointer: dict[str, Any]) -> dict[str, Any] | None:
        if (pointer.get("root"), pointer.get("claimKey")) != (root, claim_key):
            return None
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                cur = conn.cursor()
                snap = cur.execute("SELECT txid_current_snapshot()::text AS s").fetchone()["s"]
                g = cur.execute("SELECT per_stream FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
                s = cur.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s", (root, claim_key)).fetchone()
                records, corrupt = read_stream_records(cur, root, claim_key, s, g["per_stream"]) if s else ([], "no_stream")
            finally:
                conn.execute("ROLLBACK")
        if corrupt:
            return None
        rec = next((r for r in records if r.seq == pointer["seq"]), None)
        if rec is None or not _matches(rec, pointer):
            return None
        return {"pointer": dict(pointer), "bytes": A.canonical({"kind": rec.kind, "at": rec.at, "data": rec.payload}),
                "basis": {"snapshot": snap}}
    return get
