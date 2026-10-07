"""E2d prepare / commit / activate over the E2c route A adapter (plan 032 rev 6 §3a; TEST-ONLY).

- prepare(): ONE REPEATABLE READ READ ONLY snapshot gives the C-B read, the validated stream, its
  outstanding reservations (pending uncertain effects), counters and queue, all together; then the
  immutable candidate is stored (no authority). A conflicting identity never overwrites a stored
  candidate.
- commit(): one route A transaction through the existing ActsJournal._route_a (project lock first,
  then global -> writer -> stream). Inside it, on the same session and transaction: the retry
  lookup of the pre-assigned record id FIRST, then the freshness re-read, then the predecessor CAS,
  then ONE review_rebind record under the pre-assigned record id (so a lost reply is resolved by
  durable.confirm() on that exact identity).
- verify_receipt(): one snapshot; current | superseded | mismatch.

No conversation is launched, woken or invoked; activation is a client obligation (032 §3a).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from e1 import acts as A
from e1 import handoff as H
from e1.acts_durable import ActsJournal, read_facts
from e1.durable import CONNECT_OPTIONS, read_stream_records

HANDOFF_SCHEMA_SQL = Path(__file__).with_name("handoff_schema.sql")


def install_handoff(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=False) as conn:
        conn.execute(HANDOFF_SCHEMA_SQL.read_text(encoding="utf-8"))
        conn.commit()


def _aux(cur, root: str, claim_key: str) -> tuple[list[list[str]], dict[str, dict[str, Any]], list[tuple[str, str]]]:
    s = cur.execute("SELECT outstanding FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s", (root, claim_key)).fetchone()
    counters: dict[str, dict[str, Any]] = {}
    for r in cur.execute("SELECT stream_key, name, value, saturated FROM supervisor_journal.e2c_counters WHERE root = %s AND "
                         "claim_key = %s ORDER BY stream_key, name LIMIT 1024", (root, claim_key)).fetchall():
        c = counters.setdefault(r["stream_key"], {"saturated": []})
        c[r["name"]] = r["value"]
        if r["saturated"]:
            c["saturated"].append(r["name"])
    queue = [(r["stream_key"], r["reason"]) for r in cur.execute(
        "SELECT stream_key, reason FROM supervisor_journal.e2c_queue WHERE root = %s AND claim_key = %s ORDER BY stream_key, reason "
        "LIMIT 1024", (root, claim_key)).fetchall()]
    return (s["outstanding"] if s else []), counters, queue


def _evidence(view: A.View, rid: str) -> list[dict[str, Any]]:
    """Pointers only: accepted checkpoints and the binding history, from the validated prefix."""
    out = []
    for cp, digest, rec, at in view.books.get(rid, A.KeyBook()).progress:
        out.append({"seq": rec.seq, "kind": rec.kind, "checkpointId": cp, "evidenceDigest": digest})
    chain = view.chain(rid)
    for b in (chain.history + [chain.current]) if chain.status == "ok" else []:
        out.append({"seq": b.seq, "kind": "binding", "linkId": b.link_id})
    return out


@dataclass(frozen=True)
class Prepared:
    package: H.Package
    transition: dict[str, Any]
    fresh: dict[str, Any]
    task: dict[str, Any]
    rid: str
    node_id: str
    attempt: tuple[str, int]
    request: dict[str, Any]


def prepare(aj: ActsJournal, root: str, claim_key: str, review_key: dict[str, Any], *, prepare_id: str, target: str, gate: str, gate_ref: str,
            task: dict[str, Any], conversation_ref: str, now: float, unconfirmed: list[dict[str, Any]] = (),
            note: dict[str, Any] | None = None, imports: list[dict[str, Any]] = (), source_store: dict | None = None,
            principal: str | None = None, authorize: H.Authorizer | None = None, destination: str | None = None) -> Prepared:
    """Review-lead candidate. Everything read comes from ONE snapshot; the candidate grants nothing."""
    rid = A.review_id(review_key)
    node_id, attempt = review_key["nodeId"], (review_key["attemptId"], review_key["attemptEpoch"])
    request = {"attemptKey": {k: review_key[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": review_key["artifactRef"]}
    with psycopg.connect(aj.dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            cur = conn.cursor()
            snap = cur.execute("SELECT txid_current_snapshot()::text AS s").fetchone()["s"]
            facts = read_facts(cur, root, node_id, attempt, "A", {"snapshot": snap}, claim_key)
            g = cur.execute("SELECT per_stream FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
            s = cur.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s", (root, claim_key)).fetchone()
            records, corrupt = read_stream_records(cur, root, claim_key, s, g["per_stream"])
            outstanding, counters, queue = _aux(cur, root, claim_key)
        finally:
            conn.execute("ROLLBACK")
    view = A.View(records, corrupt)
    cb = A.read_cb(view, request, facts, now=now, counters=counters, queue=queue,
                   selector={"rootId": root, "nodeId": node_id, "claimKey": claim_key})
    pending = H.pending_effects(records, corrupt, outstanding, unconfirmed)
    chain = view.chain(rid)
    if chain.status != "ok":
        raise H.Refused(chain.status)
    pred = chain.current
    transition = dict(H.handoff_ids(root, claim_key, rid, pred.link_id, target, prepare_id), prepareId=prepare_id, root=root, claimKey=claim_key, option="review_rebind",
                      gate=gate, gateRef=gate_ref, predecessorBindingId=pred.link_id, fromSession=pred.session, targetSession=target,
                      conversationRef=conversation_ref)
    verified = H.verify_imports(list(imports), source_store or {}, principal or "", authorize, destination) if imports else []
    cls = A.review_state(facts.node, review_key)                  # the SAME snapshot as the C-B read
    pins = H.pins_of(facts)
    pkg = H.build(role="lead", cb=cb, planstore_class=cls, pins=pins, basis_check=[cb["basis"], cb["basis"]], pending=pending, evidence=_evidence(view, rid),
                  task=task, transition=transition, note=note, imports=verified)
    fresh = H.semantic_set("lead", cb, pending, task["packageRef"], pred.link_id, cls, pins)
    _store(aj, transition, rid, pkg, task, now)
    return Prepared(pkg, transition, fresh, task, rid, node_id, attempt, request)


def _store(aj: ActsJournal, transition: dict[str, Any], rid: str, pkg: H.Package, task: dict[str, Any], now: float) -> None:
    """Immutable candidate row. The same handoffId with another digest is a conflict; nothing is
    ever overwritten (the guard refuses UPDATE as well)."""
    with aj._serial:
        aj._usable()
        with aj.conn.cursor() as cur:
            cur.execute("INSERT INTO supervisor_journal.e2d_candidates (handoff_id, root, claim_key, review_id, candidate_digest, "
                        "manifest, envelope, task, prepared_at_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (handoff_id) DO NOTHING",
                        (transition["handoffId"], transition["root"], transition["claimKey"], rid, pkg.candidate_digest,
                         A.canonical(pkg.manifest), A.canonical(pkg.envelope),
                         A.canonical({"text": task["text"], "instructions": task["instructions"], "packageRef": task["packageRef"]}),
                         json.dumps(now)))
            row = cur.execute("SELECT candidate_digest FROM supervisor_journal.e2d_candidates WHERE handoff_id = %s",
                              (transition["handoffId"],)).fetchone()
        aj.conn.commit()
    if row["candidate_digest"] != pkg.candidate_digest:
        raise H.Refused("conflict", "this handoffId already holds a different candidate")


def commit(aj: ActsJournal, handoff: Prepared | str, *, now: float, unconfirmed: list[dict[str, Any]] = ()) -> tuple[A.Decision, dict[str, Any] | None]:
    """One route A transaction: load and verify the IMMUTABLE stored candidate -> retry check ->
    freshness -> predecessor CAS -> one review_rebind. Everything used comes from the stored
    candidate (msg 1260): a caller-held Prepared is only an id and must match it exactly, else
    `tampered_candidate`. Passing just the handoffId works after a crash lost the Prepared."""
    hid = handoff if isinstance(handoff, str) else handoff.transition["handoffId"]
    with psycopg.connect(aj.dsn, row_factory=dict_row, options=CONNECT_OPTIONS) as c:
        loc = c.execute("SELECT root, claim_key FROM supervisor_journal.e2d_candidates WHERE handoff_id = %s", (hid,)).fetchone()
    if loc is None:
        return A.Decision("candidate_unavailable"), None
    root, claim_key = loc["root"], loc["claim_key"]
    held: dict[str, Any] = {}

    def decide(view: A.View, facts: A.PlanFacts) -> A.Decision:
        with aj.conn.cursor() as c2:                      # SAME session and transaction as the route A locks
            row = c2.execute("SELECT candidate_digest, manifest, envelope, task FROM supervisor_journal.e2d_candidates WHERE "
                             "handoff_id = %s", (hid,)).fetchone()
            try:
                manifest, t, task = H.verify_stored(row)
            except H.Refused as e:
                return A.Decision(e.code, reason=str(e.detail))
            if not isinstance(handoff, str) and (handoff.transition != t or handoff.package.candidate_digest != row["candidate_digest"]
                                                 or handoff.package.manifest != manifest):
                return A.Decision("tampered_candidate", reason="the caller's Prepared differs from the stored candidate")
            held.update(t=t, digest=row["candidate_digest"])
            existing = c2.execute("SELECT root, claim_key, kind, data FROM supervisor_journal.records WHERE record_id = %s",
                                  (t["recordId"],)).fetchone()
            outstanding, counters, queue = _aux(c2, root, claim_key)
        key = manifest["state"]["mandatory"]["identity"]
        rid = A.review_id(key)
        if view.corrupt:
            # A stream that does not validate can neither confirm a retry nor accept a commit.
            return A.Decision("corrupt", reason=view.corrupt, stream_key=rid)
        fresh_prepare = H.semantic_from_manifest(manifest)
        if existing is None:
            try:
                request = {"attemptKey": {k: key[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": key["artifactRef"]}
                cb = A.read_cb(view, request, facts, now=now, counters=counters, queue=queue,
                               selector={"rootId": root, "nodeId": key["nodeId"], "claimKey": claim_key})
                pending = H.pending_effects(view.records, view.corrupt, outstanding, unconfirmed)
                fresh = H.semantic_set("lead", cb, pending, task["packageRef"], t["predecessorBindingId"],
                                       A.review_state(facts.node, key), H.pins_of(facts))
            except (A.Refused, H.Refused) as e:
                return A.Decision("stale_candidate", reason=getattr(e, "code", "refused"), stream_key=rid)
        else:
            fresh = fresh_prepare                         # (a) decides first; freshness is not consulted
        return H.decide_commit(view, rid, t, row["candidate_digest"], existing, fresh, fresh_prepare)

    with psycopg.connect(aj.dsn, row_factory=dict_row, options=CONNECT_OPTIONS) as c:
        m = json.loads(c.execute("SELECT manifest FROM supervisor_journal.e2d_candidates WHERE handoff_id = %s", (hid,)).fetchone()["manifest"])
    key = m["state"]["mandatory"]["identity"]
    rec_id = m["transition"]["recordId"]
    d = aj._route_a(root, claim_key, key["nodeId"], (key["attemptId"], key["attemptEpoch"]), decide, record_id=rec_id)
    if d.outcome not in ("accepted", "committed"):
        return d, None
    return d, receipt(aj.dsn, held["t"], held["digest"])


def receipt(dsn: str, t: dict[str, Any], digest: str) -> dict[str, Any]:
    with psycopg.connect(dsn, row_factory=dict_row, options=CONNECT_OPTIONS) as c:
        row = c.execute("SELECT seq, record_hash FROM supervisor_journal.records WHERE record_id = %s", (t["recordId"],)).fetchone()
    return {"handoffId": t["handoffId"], "candidateDigest": digest, "bindingLinkId": t["linkId"], "recordId": t["recordId"],
            "seq": row["seq"], "recordHash": row["record_hash"]}


def verify_receipt(dsn: str, root: str, claim_key: str, rid: str, rec: dict[str, Any]) -> str:
    """ONE snapshot: the record under the pre-assigned id, chain-validated, and its binding status."""
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            cur = conn.cursor()
            g = cur.execute("SELECT per_stream FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
            s = cur.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s", (root, claim_key)).fetchone()
            records, corrupt = read_stream_records(cur, root, claim_key, s, g["per_stream"])
            row = cur.execute("SELECT kind, seq, record_hash, data FROM supervisor_journal.records WHERE record_id = %s AND root = %s "
                              "AND claim_key = %s", (rec["recordId"], root, claim_key)).fetchone()
        finally:
            conn.execute("ROLLBACK")
    if corrupt:
        return "mismatch"
    return H.receipt_status(A.View(records, None), rid, rec, row)


def redeliver(dsn: str, handoff_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The SAME immutable candidate, by id: (digest, envelope, exact Task bytes), each verified
    against the manifest, so a crash that lost the Python Prepared leaves the package deliverable.
    Delivery never changes authority."""
    with psycopg.connect(dsn, row_factory=dict_row, options=CONNECT_OPTIONS) as c:
        row = c.execute("SELECT candidate_digest, manifest, envelope, task FROM supervisor_journal.e2d_candidates WHERE handoff_id = %s",
                        (handoff_id,)).fetchone()
    manifest, _, task = H.verify_stored(row)
    return row["candidate_digest"], json.loads(row["envelope"]), task
