"""E2c route A over the E2b-a durable journal (plan 030 rev 7 §6, §8, §12; TEST-ONLY, disposable DB).

`ActsJournal` IS a `DurableJournal`: same session, writer lock, epoch fence, record encoding,
reservations, idempotent ids, hash chain and corrupt-stream retention. It adds one transaction
shape, route A, used for every E2c write:

    BEGIN (READ COMMITTED)
      pg_advisory_xact_lock(hashtext('hekate-plan-project:' || project))   -- the lock EVERY PlanStore
                                                                           -- writer takes (PlanStore.cs 132/235/346/538)
      read the PlanStore facts (node row, that attempt's events, dependency edges, event_seq)
      global_usage -> writer_usage (+ fence) -> stream   FOR UPDATE        -- the E2b-a order
      read + validate the stream, decide (e1/acts.py), then ONE of:
        append exactly one record (DurableJournal._append_tx) | bump one counter | nothing
    COMMIT

The project lock is held until COMMIT, so every PlanStore fact the decision used is the current
one until the record is durable (managed nodes cannot change outside the plan contract: the
PlanStoreSchema triggers fence them). Lock order: project -> global -> writer -> stream; PlanStore
writers never take journal locks and E2b-a appends never take the project lock, so no cycle.

The C-B read is ONE REPEATABLE READ READ ONLY snapshot over the same PlanStore rows and the
journal stream, and writes nothing on any path. Nothing here writes PlanStore, wakes anyone, or is
a production mechanism. The fixture clock is the adapter's `now` (as in E2b-a), not the database clock.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

from e1 import acts as A
from e1.durable import CONNECT_OPTIONS, CommitUnknown, DurableJournal, read_stream_records
from e1.evidence import INTENTS, JournalRefused, Record

ACTS_SCHEMA_SQL = Path(__file__).with_name("acts_schema.sql")
EVENTS_MAX = 64          # attempt events read per decision (bounded; only this attempt's and its reopen)


def install_acts(dsn: str) -> None:
    """Apply the E2c fixture tables on top of an installed journal schema (disposable DB only)."""
    with psycopg.connect(dsn, autocommit=False) as conn:
        conn.execute(ACTS_SCHEMA_SQL.read_text(encoding="utf-8"))
        conn.commit()


def _uuid(v: Any) -> str:
    try:
        return str(uuid.UUID(str(v)))
    except (ValueError, TypeError) as e:
        raise A.Malformed("format", "not a uuid") from e


def read_facts(cur: psycopg.Cursor, root: str, node_id: str, attempt: tuple[str, int] | None, route: str,
               basis: dict[str, Any], claim_key: str | None = None) -> A.PlanFacts:
    """The PlanStore facts one decision or read uses, from the CURRENT transaction (route A: under
    the project lock; C-B: inside the one snapshot)."""
    node = cur.execute("SELECT row_to_json(s)::text AS j FROM public.plan_node_state s WHERE node_id = %s AND root_node_id = %s",
                       (node_id, root)).fetchone()
    node = json.loads(node["j"]) if node else None
    events: list[dict[str, Any]] = []
    if node is not None and attempt is not None:
        events = cur.execute("SELECT kind, attempt_id, attempt_epoch, node_state_revision FROM public.plan_attempt_events "
                             "WHERE node_id = %s AND ((attempt_id = %s AND attempt_epoch = %s) OR (kind = 'attempt_reopened' "
                             "AND attempt_epoch = %s)) ORDER BY seq LIMIT %s",
                             (node_id, attempt[0], attempt[1], attempt[1] + 1, EVENTS_MAX)).fetchall()
    deps = cur.execute("SELECT d.predecessor_id::text AS p, d.gate, s.state_revision AS r FROM public.plan_dependencies d "
                       "JOIN public.plan_node_state s ON s.node_id = d.predecessor_id WHERE d.successor_id = %s "
                       "ORDER BY d.predecessor_id LIMIT 64", (node_id,)).fetchall()
    seq = cur.execute("SELECT event_seq FROM public.managed_plans WHERE root_node_id = %s", (root,)).fetchone()
    receipt = None
    if claim_key is not None:
        receipt = cur.execute("SELECT outcome, node_id::text AS node_id, attempt_id, attempt_epoch, executor_ref, content_revision, "
                              "content_digest, prereq_digest FROM public.plan_claim_receipts WHERE root_node_id = %s AND claim_key = %s",
                              (root, claim_key)).fetchone()
    b = dict(basis, eventSeq=seq["event_seq"] if seq else None, dependencies=A.digest([[d["p"], d["gate"], d["r"]] for d in deps]))
    return A.PlanFacts(route, node, revision=node.get("state_revision") if node else None, basis=b, events=tuple(events),
                       receipt=dict(receipt) if receipt else None)


class ActsJournal(DurableJournal):
    """One supervisor writer instance with route A act intake (E2c).

    Single-writer serialization boundary: the writer has ONE session (the E2b-a fence refuses a
    second live session for the same writer before any intake), and every route A transaction on
    that session runs under `_serial`, so concurrent callers in this process are serialized and
    each one's decision (including the binding compare-and-set) sees every earlier commit."""

    def __init__(self, *a, **kw):
        # Re-entrant: route A calls verify_fence()/_break() internally on the same session.
        self._serial = threading.RLock()
        super().__init__(*a, **kw)

    # EVERY entry point that uses the one session runs under `_serial` (msg 1185): otherwise a
    # concurrent plain append, malformed counter, resolve or fence check could COMMIT or ROLL BACK
    # an in-flight route A transaction and release the project lock before its append.
    def _route_a(self, *a, **kw) -> A.Decision:
        with self._serial:
            return self._route_a_locked(*a, **kw)

    def append(self, *a, **kw):
        with self._serial:
            return super().append(*a, **kw)

    def resolve(self, *a, **kw):
        with self._serial:
            return super().resolve(*a, **kw)

    def verify_fence(self) -> None:
        with self._serial:
            return super().verify_fence()

    def open(self):
        with self._serial:
            return super().open()

    def reacquire(self):
        with self._serial:
            return super().reacquire()

    def close(self) -> None:
        with self._serial:
            return super().close()

    def _route_a_locked(self, root: str, claim_key: str, node_id: str, attempt: tuple[str, int] | None,
                 decide: Callable[[A.View, A.PlanFacts], A.Decision], *, record_id: str | None = None,
                 facts_override: A.PlanFacts | None = None, after_append: Callable[[Any, Record], None] | None = None) -> A.Decision:
        """Run `decide` inside one route A transaction and apply its result. Mirrors
        DurableJournal.append for the append path: an identical resend is a no-op, a lost COMMIT
        reply is CommitUnknown (adapter broken until reacquire()), fault hooks fire at
        before_commit/after_commit, `_expected` advances only on a committed record, and an intent
        re-checks the fence before any effect may start."""
        self._usable()
        if facts_override is not None and facts_override.route != "B":
            raise ValueError("only an independent (route B) read may be supplied; route A facts are read under the lock")
        root, node_id = _uuid(root), _uuid(node_id)
        skey = (root, claim_key)
        pending = None
        try:
            with self.conn.cursor() as cur:
                proj = cur.execute("SELECT project_id::text AS p FROM public.managed_plans WHERE root_node_id = %s", (root,)).fetchone()
                if proj is None:
                    raise JournalRefused("no_plan", root)
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('hekate-plan-project:' || %s::text))", (proj["p"],))
                txid = cur.execute("SELECT txid_current() AS t").fetchone()["t"]
                facts = facts_override or read_facts(cur, root, node_id, attempt, "A", {"txid": txid, "projectLock": True}, claim_key)
                g = cur.execute("SELECT * FROM supervisor_journal.global_usage WHERE id = 1 FOR UPDATE").fetchone()
                self._check_fence(cur)
                s = cur.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s FOR UPDATE",
                                skey).fetchone()
                if s is None or s["writer_id"] != self.writer:
                    raise JournalRefused("foreign_writer" if s else "no_stream", "E2c acts need this writer's claim stream")
                records, corrupt = read_stream_records(cur, root, claim_key, s, g["per_stream"])
                view = A.View(records, corrupt)
                d = decide(view, facts)
                ck = view.counter_key(d.stream_key)          # bounded: known keys + one shared bucket
                if d.counter and ck:
                    self._bump(cur, root, claim_key, ck, d.counter)
                if d.queue and ck:
                    cur.execute("INSERT INTO supervisor_journal.e2c_queue (root, claim_key, stream_key, reason, at_json) "
                                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                                (root, claim_key, ck, d.queue, json.dumps(self.now)))
                if d.append is None:
                    self.conn.commit()                       # counter/queue only (diagnostic): no record, no effect
                    return d
                if s["state"] != "active":
                    raise JournalRefused("resolved", "stream is resolved")
                kind, data = d.append
                rid = str(uuid.UUID(record_id)) if record_id else str(uuid.uuid4())
                exp = self._expected.get(skey)
                rec, created = self._append_tx(cur, root, claim_key, kind, data, rid, exp)
                if not created:
                    self.conn.rollback()
                    self._expected[skey] = rec.seq + 1
                    if kind in INTENTS:
                        self.verify_fence()
                    return A.Decision("duplicate", original=rec, stream_key=d.stream_key)
                if after_append:
                    after_append(cur, rec)                   # same transaction, after the stream lock
                if self.fault:
                    self.fault("before_commit", self)
                pending = {"record_id": rid, "root": root, "claim_key": claim_key, "kind": kind, "data": data, "expected_seq": rec.seq}
        except (JournalRefused, A.Malformed, A.Refused):
            self._safe_rollback()
            raise
        except psycopg.Error as e:
            self._break()                                    # COMMIT never sent: not committed
            raise JournalRefused("connection_lost", type(e).__name__) from e
        try:
            self.conn.commit()
        except psycopg.Error as e:
            self._break()
            self.unknown = pending
            raise CommitUnknown(pending["record_id"], type(e).__name__) from e
        if self.fault:
            try:
                self.fault("after_commit", self)
            except Exception as e:  # noqa: BLE001 -- the REPLY was lost after a real COMMIT
                self._break()
                self.unknown = pending
                raise CommitUnknown(pending["record_id"], f"reply lost: {type(e).__name__}") from e
        self._expected[skey] = rec.seq + 1
        if kind in INTENTS:
            self.verify_fence()
        return A.Decision(d.outcome, append=d.append, stream_key=d.stream_key, reason=d.reason,
                          original=rec)

    def _bump(self, cur, root: str, claim_key: str, sk: str, name: str) -> None:
        cur.execute("INSERT INTO supervisor_journal.e2c_counters (root, claim_key, stream_key, name, value, saturated) "
                    "VALUES (%s, %s, %s, %s, 1, false) ON CONFLICT (root, claim_key, stream_key, name) DO UPDATE SET "
                    "value = LEAST(e2c_counters.value + 1, %s), "
                    "saturated = e2c_counters.saturated OR e2c_counters.value + 1 >= %s",
                    (root, claim_key, sk, name, A.COUNTER_MAX, A.COUNTER_MAX))

    # --- operations ---------------------------------------------------------------------------------------
    def dispatch(self, root: str, claim_key: str, key: dict[str, Any]) -> str:
        """package_ref (once) then dispatch_intent, each its own route A transaction. The claim's
        executorRef must conform and equal workerSession byte for byte, read under the project lock."""
        key = A.parse_exec_key(key)
        if key["rootId"] != root or key["claimKey"] != claim_key:
            raise A.Malformed("stream", "dispatch key does not belong to this (root, claimKey) stream")
        sha = hashlib.sha256(key["packageRef"].encode("utf-8")).hexdigest()
        attempt = (key["attemptId"], key["attemptEpoch"])
        pkg = json.loads(key["packageRef"])

        def check(view: A.View, facts: A.PlanFacts) -> str | None:
            n, r = facts.node or {}, facts.receipt
            ref = n.get("executor_ref")
            if not (isinstance(ref, str) and A.SESSION.fullmatch(ref) and ref == key["workerSession"]
                    and n.get("work_status") == "in_progress" and n.get("attempt_id") == key["attemptId"]
                    and n.get("attempt_epoch") == key["attemptEpoch"]):
                return "not_dispatchable"
            # The authoritative receipt for THIS claim: node, attempt, epoch, executor and pins.
            if r is None or r["outcome"] != "claimed" or (r["node_id"], r["attempt_id"], r["attempt_epoch"], r["executor_ref"]) != (
                    key["nodeId"], key["attemptId"], key["attemptEpoch"], key["workerSession"]):
                return "receipt_mismatch"
            if pkg["kind"] == "supplied.v1":
                try:
                    digest_ok = A.content_digest_from_planstore(r["content_digest"]) == pkg["contentDigest"]
                except A.Malformed:
                    digest_ok = False
                if (r["content_revision"], r["prereq_digest"]) != (pkg["contentRevision"], pkg["prereqDigest"]) or not digest_ok:
                    return "receipt_mismatch"
            if view.dispatch.get(key["runId"]):
                return "run_id_reused"
            return None

        def package(view: A.View, facts: A.PlanFacts) -> A.Decision:
            why = check(view, facts)
            if why:
                return A.Decision(why, queue=why if why != "run_id_reused" else None, stream_key=A.exec_id(key))
            if sha in view.packages:
                return A.Decision("present")
            return A.Decision("accepted", append=("package_ref", {"packageSha256": sha, "jcs": key["packageRef"]}))

        d = self._route_a(root, claim_key, key["nodeId"], attempt, package)
        if d.outcome not in ("accepted", "present", "duplicate"):
            raise A.Refused(d.outcome)
        stored = {f: key[f] for f in A.EXEC_FIELDS if f != "packageRef"}
        stored["packageSha256"] = sha

        def intent(view: A.View, facts: A.PlanFacts) -> A.Decision:
            why = check(view, facts)
            if why:
                return A.Decision(why, queue=why if why != "run_id_reused" else None, stream_key=A.exec_id(key))
            return A.Decision("accepted", append=("dispatch_intent", {"exec": A.exec_id(key), "key": stored}))

        def claim_run(cur, rec: Record) -> None:
            cur.execute("SAVEPOINT run_id")
            try:
                cur.execute("INSERT INTO supervisor_journal.e2c_runs (run_id, root, claim_key, exec_id) VALUES (%s, %s, %s, %s)",
                            (key["runId"], root, claim_key, A.exec_id(key)))
            except psycopg.errors.UniqueViolation:
                cur.execute("ROLLBACK TO SAVEPOINT run_id")
                raise A.Refused("run_id_reused", "runId is already dispatched in another stream")  # whole txn rolls back

        d = self._route_a(root, claim_key, key["nodeId"], attempt, intent, after_append=claim_run)
        if d.outcome != "accepted":
            raise A.Refused(d.outcome)
        return A.exec_id(key)

    def intake(self, root: str, claim_key: str, raw: bytes | str, *, route_b_facts: A.PlanFacts | None = None) -> A.Decision:
        """C-C act intake. Route A by default (facts read under the project lock). With
        `route_b_facts` (an INDEPENDENT read the caller made earlier) the act can only become an
        audit-only observation; the journal append itself is still under the E2b-a locks."""
        try:
            act = A.parse_act(raw)
            A.check_stream(act, root, claim_key)
        except A.Malformed as e:
            self._malformed(root, claim_key)
            return A.Decision("malformed", counter="malformed", reason=e.code)
        attempt = (act.key["attemptId"], act.key["attemptEpoch"])
        obs = str(uuid.uuid4())
        rid = A.act_record_id(act.act_id) if route_b_facts is None else str(uuid.uuid5(A.ACT_NS, "obs:" + act.act_id + ":" + obs))
        return self._route_a(root, claim_key, act.key["nodeId"], attempt,
                             lambda view, facts: A.decide_act(view, act, facts, obs_id=obs),
                             record_id=rid, facts_override=route_b_facts)

    def _malformed(self, root: str, claim_key: str) -> None:
        with self._serial:
            self._malformed_locked(root, claim_key)

    def _malformed_locked(self, root: str, claim_key: str) -> None:
        self._usable()
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1 FROM supervisor_journal.global_usage WHERE id = 1 FOR UPDATE")
                self._check_fence(cur)
                if cur.execute("SELECT 1 FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s FOR UPDATE",
                               (root, claim_key)).fetchone():
                    self._bump(cur, root, claim_key, "_stream", "malformed")
            self.conn.commit()
        except psycopg.Error as e:
            self._break()
            raise JournalRefused("connection_lost", type(e).__name__) from e

    def confirm_act(self, root: str, claim_key: str, node_id: str, observation_id: str, reconciliation_ref: str) -> A.Decision:
        """Operator act: makes one route B observation effective at its ORIGINAL `at`."""
        return self._route_a(root, claim_key, node_id, None, lambda view, facts: A.decide_confirm(view, observation_id, reconciliation_ref))

    def request_review(self, root: str, claim_key: str, key: dict[str, Any]) -> A.Decision:
        return self._route_a(root, claim_key, key["nodeId"], (key["attemptId"], key["attemptEpoch"]),
                             lambda view, facts: A.decide_request(view, key, facts))

    def bind(self, root: str, claim_key: str, node_id: str, rid: str, kind: str, predecessor: str, session: str, *, gate: str,
             gate_ref: str, before_decide: Callable[[], None] | None = None) -> A.Decision:
        """review_assigned / review_rebind: the compare-and-set runs INSIDE the locked transaction
        against the chain as it is at that moment (`predecessor` is what the caller read earlier).
        `before_decide` is a TEST hook that runs inside the transaction, after the locks."""
        def decide(view: A.View, facts: A.PlanFacts) -> A.Decision:
            if before_decide:
                before_decide()
            return A.decide_binding(view, rid, kind, predecessor, session, gate=gate, gate_ref=gate_ref)
        return self._route_a(root, claim_key, node_id, None, decide)

    def record_end(self, root: str, claim_key: str, key: dict[str, Any]) -> A.Decision:
        eid = A.exec_id(key)

        def decide(view: A.View, facts: A.PlanFacts) -> A.Decision:
            end = A.stream_end(key, facts)
            if end is None or eid in view.ends:
                return A.Decision("refused" if end is None else "duplicate")
            return A.Decision("accepted", append=(end[0], {"exec": eid, "reason": end[1]}))
        return self._route_a(root, claim_key, key["nodeId"], (key["attemptId"], key["attemptEpoch"]), decide)

    def transport(self, root: str, claim_key: str, fact: str, ref: str) -> Record:
        """A transport/launch fact, historical only (plain E2b-a append; no PlanStore facts used)."""
        return self.append(root, claim_key, "transport_observed", {"source": "agent-bridge", "fact": fact, "ref": ref})


LEAF_FIELDS = ("work_status", "attempt_id", "attempt_epoch", "artifact_ref", "content_revision", "state_revision", "acc_decision")


def evidence_unavailable(facts: A.PlanFacts, reason: str) -> dict[str, Any]:
    """The plan survives evidence failure: the PlanStore leaf read in the same snapshot, unchanged,
    with every evidence field unknown. No identity is resolved (that needs the journal)."""
    leaf = {k: facts.node.get(k) for k in LEAF_FIELDS} if facts.node else None
    return {"identity": None, "basis": dict(facts.basis, route=facts.route, revision=facts.revision), "leaf": leaf,
            "evidence": "unavailable", "evidenceReason": reason[:200], "current": "unknown",
            "historical": {"items": [], "truncated": False, "nextCursor": None}}


def independent_facts(dsn: str, root: str, node_id: str) -> A.PlanFacts:
    """A route B read: its own short snapshot, NOT the transaction that later appends."""
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        f = read_facts(conn.cursor(), root, node_id, None, "B", {"independentRead": True})
        conn.execute("COMMIT")
    return f


def read_cb(dsn: str, root: str, claim_key: str, node_id: str, request: dict[str, Any], *, now: float, attempt: tuple[str, int] | None = None,
            pending: dict[str, str] | None = None, after: int = 0, limit: int = A.PAGE_DEFAULT,
            before_journal: Callable[[], None] | None = None) -> dict[str, Any]:
    """C-B: ONE REPEATABLE READ READ ONLY snapshot over PlanStore rows and the journal. Writes
    nothing (the transaction is READ ONLY) on every path, including evidence failure.
    `before_journal` is a TEST hook run between the PlanStore and journal reads of the SAME snapshot."""
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, options=CONNECT_OPTIONS) as conn:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            cur = conn.cursor()
            snap = cur.execute("SELECT txid_current_snapshot()::text AS s").fetchone()["s"]
            facts = read_facts(cur, _uuid(root), _uuid(node_id), attempt, "A", {"snapshot": snap})
            if before_journal:
                before_journal()
            # Evidence failure never touches the plan view (030 §2.10): the journal read runs in a
            # savepoint; if it fails, the already-read PlanStore leaf is returned with evidence unknown.
            cur.execute("SAVEPOINT journal_read")
            try:
                g = cur.execute("SELECT per_stream FROM supervisor_journal.global_usage WHERE id = 1").fetchone()
                s = cur.execute("SELECT * FROM supervisor_journal.streams WHERE root = %s AND claim_key = %s", (root, claim_key)).fetchone()
                if g is None or s is None:
                    raise LookupError("no_stream" if g is not None else "no_journal")
                records, corrupt = read_stream_records(cur, root, claim_key, s, g["per_stream"])
                counters: dict[str, dict[str, Any]] = {}
                for r in cur.execute("SELECT stream_key, name, value, saturated FROM supervisor_journal.e2c_counters WHERE root = %s "
                                     "AND claim_key = %s ORDER BY stream_key, name LIMIT 1024", (root, claim_key)).fetchall():
                    c = counters.setdefault(r["stream_key"], {"saturated": []})
                    c[r["name"]] = r["value"]
                    if r["saturated"]:
                        c["saturated"].append(r["name"])
                queue = [(r["stream_key"], r["reason"]) for r in cur.execute(
                    "SELECT stream_key, reason FROM supervisor_journal.e2c_queue WHERE root = %s AND claim_key = %s "
                    "ORDER BY stream_key, reason LIMIT 1024", (root, claim_key)).fetchall()]
            except (psycopg.Error, LookupError) as e:
                cur.execute("ROLLBACK TO SAVEPOINT journal_read")
                return evidence_unavailable(facts, f"{type(e).__name__}:{e}" if isinstance(e, LookupError) else type(e).__name__)
            view = A.View(records, corrupt)
            return A.read_cb(view, request, facts, now=now, counters=counters, queue=queue, pending=pending, after=after, limit=limit,
                             selector={"rootId": root, "nodeId": node_id, "claimKey": claim_key})
        finally:
            conn.execute("ROLLBACK")
