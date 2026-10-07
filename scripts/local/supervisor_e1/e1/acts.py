"""E2c act boundary (plan 030 rev 7 §12; TEST-ONLY, fixture constants only). Pure: no I/O, no clock.

Worker execution acknowledgment, progress and the review obligation, layered on the E2a/E2b-a
journal. Records live in the existing (root, claimKey) streams, so reservations, required outcomes,
idempotent ids, hash-chain validation and corrupt-stream retention are the E2a/E2b-a rules. This
module only:

- parses and validates the act wire (exact integers, frozen §4 shapes, fixture limits);
- DECIDES what one inbound act, binding change or confirmation does, from a validated record
  list plus PlanStore facts (`decide_act`, `decide_binding`, `decide_confirm`). The caller (the
  in-memory model below, or e1/acts_durable.py inside one route A transaction) applies it;
- derives bookkeeping, binding chains, deadline phases and the C-B read from records + facts.

Nothing here performs or authorizes an effect, writes PlanStore, wakes anyone, or is a production
identity/auth/default contract (030 §4, msg 1145). Every constant below is a fixture constant.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from e1.evidence import DECISIONS, Bounds, ModelJournal, Record, older_attempt_decision
from e1.exact import WireError, loads_exact

# --- fixture constants (030 §4.1; NOT accepted production defaults) ---------------------------------

INT_MAX = 2**53 - 1
G = 64                       # novel progress checkpoints (and retained digests) per key
V = 16                       # route B observations per key
K = 3                        # notification intents per key per purpose
COUNTER_MAX = 2**32 - 1      # saturating u32
PAYLOAD_MAX = 16 * 1024      # act wire body, canonical JSON
STR_MAX = 1024
WINDOW_MIN, WINDOW_MAX = 60, 86_400
ACK_WINDOW, FIRST_PROGRESS_WINDOW, PROGRESS_WINDOW = 900, 1800, 1800
CB_KEYS_MAX, PAGE_MAX, PAGE_DEFAULT, BODY_MAX = 32, 200, 50, 1 << 20

# E2c streams need more room than the E2a defaults (N=64): G progress records plus the ACK,
# dispatch, package, review and binding records and their reservations. Scoped to E2c only; the
# E2a/E2b-a defaults are unchanged.
E2C_BOUNDS = Bounds(per_stream=256, unresolved_streams=32, global_records=4000, global_bytes=8 << 20)

COUNTERS = ("malformed", "stale", "foreign_session", "conflict", "out_of_order", "not_novel", "novelty_unknown",
            "progress_overflow", "duplicate", "observation_overflow")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PRINTABLE = re.compile(r"^[!-~]+$")                          # printable ASCII, no spaces
CLAIM_KEY = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")          # PlanStore.IsValidClaimKey
PRINCIPAL = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SESSION = re.compile(r"^hkw1:([a-z0-9][a-z0-9-]{0,63}):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

WORKER_ACTS = ("worker_ack", "worker_progress")
REVIEW_ACTS = ("review_acknowledged", "review_progress")
ACK_KINDS = ("worker_ack", "review_acknowledged")
EXEC_FIELDS = ("rootId", "nodeId", "attemptId", "attemptEpoch", "claimKey", "runId", "packageRef", "workerSession")
ATTEMPT_FIELDS = ("rootId", "nodeId", "attemptId", "attemptEpoch")
REVIEW_FIELDS = ATTEMPT_FIELDS + ("artifactRef", "lead", "leadSession")
ACT_NS = uuid.UUID("5e2c0000-0000-4000-8000-000000000030")


class Malformed(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def canonical(obj: Any) -> str:
    """RFC 8785 JCS for the values used here (ASCII strings, integers, objects, arrays, null)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


# --- wire validation (030 §4, §4.1) --------------------------------------------------------------------

def wire_int(v: Any, what: str, lo: int, hi: int = INT_MAX) -> int:
    """A JSON integer token in [lo, hi]. Booleans, strings, null and (already rejected by
    loads_exact) fractional or exponent tokens are malformed, never coerced."""
    if isinstance(v, bool) or not isinstance(v, int):
        raise Malformed("integer", f"{what} must be an integer, got {type(v).__name__}")
    if not lo <= v <= hi:
        raise Malformed("integer_range", f"{what}={v} outside {lo}..{hi}")
    return v


def wire_str(v: Any, what: str, pattern: re.Pattern | None = None, max_len: int = STR_MAX) -> str:
    if not isinstance(v, str) or not 1 <= len(v) <= max_len:
        raise Malformed("string", f"{what} must be a 1..{max_len} character string")
    if pattern is not None and not pattern.fullmatch(v):
        raise Malformed("format", f"{what} does not match {pattern.pattern}")
    return v


def exact_fields(obj: Any, fields: Iterable[str], what: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise Malformed("shape", f"{what} must be an object")
    want = set(fields)
    if set(obj) != want:
        missing, extra = sorted(want - set(obj)), sorted(set(obj) - want)
        raise Malformed("fields", f"{what}: missing {missing} extra {extra}")
    return obj


def parse_package(text: Any) -> dict[str, Any]:
    """`packageRef`: a JCS string in exactly one of the two frozen forms (fixture constants)."""
    if not isinstance(text, str) or not 1 <= len(text) <= STR_MAX:
        raise Malformed("package", "packageRef must be a JCS string of at most 1024 characters")
    try:
        doc = loads_exact(text)
    except WireError as e:
        raise Malformed("package_json", e.code) from e
    if not isinstance(doc, dict) or doc.get("kind") not in ("supplied.v1", "stored.v1"):
        raise Malformed("package_kind", "unknown packageRef kind")
    if doc["kind"] == "supplied.v1":
        exact_fields(doc, ("kind", "suppliedSha256", "instructions", "contentRevision", "contentDigest", "prereqDigest"), "packageRef")
        exact_fields(doc["instructions"], ("system", "fast", "deep"), "packageRef.instructions")
        for name in ("suppliedSha256", "contentDigest"):
            wire_str(doc[name], name, HEX64, 64)
        for name in ("system", "fast", "deep"):
            wire_str(doc["instructions"][name], f"instructions.{name}", HEX64, 64)
        wire_int(doc["contentRevision"], "contentRevision", 1)
        # D: exactly as the PlanStore receipt returns it -- PlanRules.DigestOf is 64 lowercase hex.
        wire_str(doc["prereqDigest"], "prereqDigest", HEX64, 64)
    else:
        exact_fields(doc, ("kind", "storeRef", "sha256"), "packageRef")
        wire_str(doc["storeRef"], "storeRef", PRINTABLE, 256)
        wire_str(doc["sha256"], "sha256", HEX64, 64)
    if canonical(doc) != text:
        raise Malformed("package_not_jcs", "packageRef is not in canonical JCS form")
    return doc


def parse_attempt(obj: dict[str, Any], what: str) -> dict[str, Any]:
    wire_str(obj["rootId"], f"{what}.rootId", UUID_RE, 36)
    wire_str(obj["nodeId"], f"{what}.nodeId", UUID_RE, 36)
    wire_str(obj["attemptId"], f"{what}.attemptId", None, 256)
    wire_int(obj["attemptEpoch"], f"{what}.attemptEpoch", 1)       # a started attempt is >= 1
    return {f: obj[f] for f in ATTEMPT_FIELDS}


def parse_exec_key(obj: Any) -> dict[str, Any]:
    exact_fields(obj, EXEC_FIELDS, "executionKey")
    key = parse_attempt(obj, "executionKey")
    key["claimKey"] = wire_str(obj["claimKey"], "claimKey", CLAIM_KEY, 128)
    if obj["claimKey"] in (".", ".."):
        raise Malformed("format", "claimKey dot segment")
    key["runId"] = wire_str(obj["runId"], "runId", PRINTABLE, 256)
    parse_package(obj["packageRef"])
    key["packageRef"] = obj["packageRef"]
    key["workerSession"] = wire_str(obj["workerSession"], "workerSession", SESSION, 256)
    return key


def parse_review_key(obj: Any, *, session_required: bool = True) -> dict[str, Any]:
    exact_fields(obj, REVIEW_FIELDS, "reviewKey")
    key = parse_attempt(obj, "reviewKey")
    key["artifactRef"] = wire_str(obj["artifactRef"], "artifactRef", PRINTABLE, 512)
    key["lead"] = wire_str(obj["lead"], "lead", PRINCIPAL, 64)
    if obj["leadSession"] is None and not session_required:
        key["leadSession"] = None
    else:
        s = wire_str(obj["leadSession"], "leadSession", SESSION, 256)
        if SESSION.fullmatch(s).group(1) != key["lead"]:
            raise Malformed("format", "leadSession principal is not the lead")
        key["leadSession"] = s
    return key


@dataclass(frozen=True)
class Act:
    kind: str
    key: dict[str, Any]
    act_seq: int
    checkpoint_id: int | None
    evidence_digest: str | None
    canonical: str                 # canonical JSON of the whole act (duplicate vs conflict)

    @property
    def worker(self) -> bool:
        return self.kind in WORKER_ACTS

    @property
    def stream_key(self) -> str:
        """Bookkeeping key: the full ExecutionKey, or the review identity (AttemptKey + artifact),
        which a lead-session rebind does not change."""
        return exec_id(self.key) if self.worker else review_id(self.key)

    @property
    def session(self) -> str:
        return self.key["workerSession"] if self.worker else self.key["leadSession"]

    @property
    def act_id(self) -> str:
        """030 §4: ActId = (FULL ExecutionKey or FULL ReviewKey incl. lead and leadSession, kind, actSeq).
        Bookkeeping and deadlines stay on `stream_key`, so a rebind changes ActIds, never anchors."""
        full = exec_id(self.key) if self.worker else digest({f: self.key[f] for f in REVIEW_FIELDS})
        return digest([full, self.kind, self.act_seq])


def parse_act(raw: bytes | str) -> Act:
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > PAYLOAD_MAX:
        raise Malformed("payload_size", f"{len(raw)} > {PAYLOAD_MAX} bytes")
    try:
        doc = loads_exact(raw)
    except WireError as e:
        raise Malformed("json", e.code) from e
    if not isinstance(doc, dict) or doc.get("kind") not in WORKER_ACTS + REVIEW_ACTS:
        raise Malformed("kind", "unknown act kind")
    progress = doc["kind"] in ("worker_progress", "review_progress")
    fields = ("kind", "key", "actSeq") + (("checkpointId", "evidenceDigest") if progress else ())
    exact_fields(doc, fields, "act")
    key = parse_exec_key(doc["key"]) if doc["kind"] in WORKER_ACTS else parse_review_key(doc["key"])
    seq = wire_int(doc["actSeq"], "actSeq", 1)
    cp = wire_int(doc["checkpointId"], "checkpointId", 1) if progress else None
    ev = wire_str(doc["evidenceDigest"], "evidenceDigest", HEX64, 64) if progress else None
    return Act(doc["kind"], key, seq, cp, ev, canonical(doc))


def check_stream(act: Act, root: str, claim_key: str) -> None:
    """An act is taken only into ITS stream: rootId (and, for a worker, claimKey) must equal the
    stream it is submitted to. Anything else is malformed, never matched loosely (msg 1180)."""
    if act.key["rootId"] != root or (act.worker and act.key["claimKey"] != claim_key):
        raise Malformed("stream", "act key does not belong to this (root, claimKey) stream")


def content_digest_from_planstore(value: Any) -> str:
    """Explicit boundary conversion (msg 1180): PlanStore's structured-content digest is 64 UPPERCASE
    hex (seam.CONTENT_DIGEST); the frozen 030 wire H is lowercase. Validate, then lowercase."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-F]{64}", value):
        raise Malformed("format", "PlanStore contentDigest must be 64 uppercase hex")
    return value.lower()


def exec_id(key: dict[str, Any]) -> str:
    return digest({f: key[f] for f in EXEC_FIELDS})


def attempt_of(key: dict[str, Any]) -> dict[str, Any]:
    return {f: key[f] for f in ATTEMPT_FIELDS}


def review_id(key: dict[str, Any]) -> str:
    return digest(dict(attempt_of(key), artifactRef=key["artifactRef"]))


def act_record_id(act_id: str) -> str:
    """Deterministic client record id (E2b-a idempotent INSERT): the same act maps to one id."""
    return str(uuid.uuid5(ACT_NS, act_id))


# --- PlanStore facts (read by the caller; route A = inside the one locked transaction) ----------------

@dataclass(frozen=True)
class PlanFacts:
    """What one read returned. `route == "A"`: read inside the route A transaction (project lock
    held through the append) or the one combined C-B snapshot. `route == "B"`: an independent read.
    `node` uses PlanStore's plan_node_state column names. `events` are that node's attempt events
    ({kind, attempt_id, attempt_epoch}) needed to name a supersession. `prerequisites` is always
    "unverified": the current prerequisite digest needs whole-graph PlanRules (msg 1170)."""
    route: str
    node: dict[str, Any] | None
    revision: int | None = None                 # node state_revision as read
    basis: dict[str, Any] = field(default_factory=dict)
    events: tuple[dict[str, Any], ...] = ()
    prerequisites: str = "unverified"
    receipt: dict[str, Any] | None = None       # plan_claim_receipts row for (root, claimKey), when read

    def summary(self) -> dict[str, Any]:
        n = self.node or {}
        return {"route": self.route, "stateRevision": self.revision, "work": n.get("work_status"), "attemptId": n.get("attempt_id"),
                "attemptEpoch": n.get("attempt_epoch"), "prerequisites": self.prerequisites}


FACT_INTS = ("attempt_epoch", "content_revision", "attempt_content_revision", "state_revision", "acc_content_revision",
             "acc_attempt_epoch")


def facts_problem(facts: PlanFacts) -> str | None:
    """Authoritative facts that cannot be carried fail closed (030 §4.1): a missing node, or an
    integer that is not an int in 0..2^53-1. Never passed through to C-B or a decision."""
    if facts.node is None:
        return "no_planstore_facts"
    for name in FACT_INTS:
        v = facts.node.get(name)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= INT_MAX):
            return f"fact_out_of_range:{name}"
    if facts.revision is not None and (isinstance(facts.revision, bool) or not isinstance(facts.revision, int)
                                       or not 0 <= facts.revision <= INT_MAX):
        return "fact_out_of_range:revision"
    return None


def review_state(node: dict[str, Any] | None, key: dict[str, Any]) -> str:
    """For an EXACT ReviewKey (AttemptKey + artifact): candidate | decided | operator_classification | moot.
    `decided` needs the authoritative decision identity to equal the exact key (acc_attempt_id,
    acc_attempt_epoch, acc_artifact_ref) and the current content (acc_content_revision). A decision
    occurred; whether the acceptance is still valid by prerequisites stays unverified (msg 1176).
    A decision recorded against anything else is operator_classification, never `decided`, EXCEPT a
    valid decision that provably belongs to a strictly OLDER attempt epoch (plan 038, HK-ISSUE-012):
    this attempt was never reviewed, so it is a `candidate`. An older accepted decision is never
    `decided` and never revives approval; the record stays history (plan 012)."""
    if not (node is not None and node.get("work_status") == "done" and node.get("attempt_id") == key["attemptId"]
            and node.get("attempt_epoch") == key["attemptEpoch"] and node.get("artifact_ref") == key["artifactRef"]):
        return "moot"
    if node.get("acc_decision") is None:
        return "candidate"
    if older_attempt_decision(node.get("acc_decision"), node.get("acc_attempt_epoch"), key["attemptEpoch"]):
        return "candidate"
    if (node.get("acc_decision") in DECISIONS and node.get("acc_attempt_id") == key["attemptId"]
            and node.get("acc_attempt_epoch") == key["attemptEpoch"]
            and node.get("acc_artifact_ref") == key["artifactRef"] and node.get("acc_content_revision") == node.get("content_revision")):
        return "decided"
    return "operator_classification"


def stream_end(key: dict[str, Any], facts: PlanFacts) -> tuple[str, str | None] | None:
    """(finished|superseded, reason) from CURRENT facts, or None while the attempt is live.
    A normal matching InProgress -> Done for exactly this attempt is `finished` whoever recorded it
    (PlanStore keeps executorRef and pins on Done, PlanRules.cs:368-369)."""
    n = facts.node
    if n is None:
        return None
    same = n.get("attempt_id") == key["attemptId"] and n.get("attempt_epoch") == key["attemptEpoch"]
    if same and n.get("work_status") == "in_progress":
        return None
    if same and n.get("work_status") == "done":
        return ("finished", None)
    for e in facts.events:
        if e.get("attempt_id") == key["attemptId"] and e.get("attempt_epoch") == key["attemptEpoch"]:
            if e.get("kind") == "attempt_released":
                return ("superseded", "released")
            if e.get("kind") == "attempt_cancelled":
                return ("superseded", "cancelled")
        if e.get("kind") == "attempt_reopened" and e.get("attempt_epoch") == key["attemptEpoch"] + 1:
            return ("superseded", "reopened")
    if n.get("work_status") == "cancelled":
        return ("superseded", "cancelled")
    if n.get("work_status") == "todo":
        return ("superseded", "released")
    return ("superseded", "epoch_replaced")


# --- deriving state from a validated record list ----------------------------------------------------------

@dataclass
class Binding:
    link_id: str
    lead: str
    session: str | None
    seq: int
    at: float
    kind: str


@dataclass
class Chain:
    status: str                       # ok | unknown_key | ambiguous_key | ambiguous_binding | corrupt
    current: Binding | None = None
    history: list[Binding] = field(default_factory=list)
    requested: Record | None = None
    detail: str = ""


@dataclass
class KeyBook:
    """Bookkeeping DERIVED from records (no second source of truth)."""
    ack: tuple[Record, float] | None = None          # (record, anchor at)
    progress: list[tuple[int, str, Record, float]] = field(default_factory=list)   # (checkpoint, digest, rec, at)
    last_act_seq: int = 0
    act_ids: dict[str, tuple[str, Record]] = field(default_factory=dict)          # act_id -> (act canonical digest, record)

    @property
    def last_checkpoint(self) -> int:
        return max((p[0] for p in self.progress), default=0)

    @property
    def digests(self) -> set[str]:
        return {p[1] for p in self.progress}


class View:
    """Index over ONE validated stream (the E2b-a reader already checked seq, chain and payloads).
    `corrupt` is the reader's reason when only a valid prefix could be read."""

    def __init__(self, records: list[Record], corrupt: str | None = None):
        self.records, self.corrupt = records, corrupt
        self.packages: dict[str, str] = {}                       # packageSha256 -> JCS
        self.dispatch: dict[str, list[tuple[Record, dict[str, Any]]]] = {}    # runId -> [(rec, key)]
        self.outcomes: dict[str, Record] = {}                    # exec id -> dispatch_outcome
        self.ends: dict[str, Record] = {}                        # exec id -> finished/superseded
        self.observations: dict[str, list[Record]] = {}          # stream key -> act_observation
        self.obs_by_id: dict[str, Record] = {}
        self.confirmed: dict[str, Record] = {}                   # observation id -> confirm_act record
        self.requests: dict[str, list[Record]] = {}              # review id -> review_requested
        self.links: dict[str, list[Record]] = {}                 # review id -> binding links
        self.transport: list[Record] = []
        self.books: dict[str, KeyBook] = {}
        for r in records:
            self._index(r)

    def book(self, k: str) -> KeyBook:
        return self.books.setdefault(k, KeyBook())

    def counter_key(self, sk: str | None) -> str | None:
        """Counters and queue entries are kept per KNOWN key (a dispatched ExecutionKey or a requested
        review) plus ONE shared bucket for everything else, so arbitrary inbound keys can never grow
        the diagnostic tables: rows <= (dispatches + reviews + 2) x counter names (msg 1180)."""
        if sk is None or sk == "_stream":
            return sk
        known = sk in self.requests or any(exec_id(k) == sk for runs in self.dispatch.values() for _, k in runs)
        return sk if known else "_unknown_key"

    def _index(self, r: Record) -> None:
        if r.degraded:
            return
        d = r.payload
        if r.kind == "package_ref":
            self.packages[d["packageSha256"]] = d["jcs"]
        elif r.kind == "dispatch_intent":
            key = dict(d["key"])
            key["packageRef"] = self.packages.get(d["key"]["packageSha256"])
            del key["packageSha256"]
            self.dispatch.setdefault(key["runId"], []).append((r, key))
        elif r.kind == "dispatch_outcome":
            self.outcomes[d["exec"]] = r
        elif r.kind in ("finished", "superseded"):
            self.ends.setdefault(d["exec"], r)
        elif r.kind == "act_observation":
            self.observations.setdefault(d["streamKey"], []).append(r)
            self.obs_by_id[d["obsId"]] = r
        elif r.kind == "operator_resolution" and d.get("decision") == "confirm_act":
            obs = self.obs_by_id.get(d.get("observationId"))
            if obs is not None and d["observationId"] not in self.confirmed:
                self.confirmed[d["observationId"]] = r
                self._accept(obs.payload["act"], obs, obs.at)          # anchors at the ORIGINAL observation at
        elif r.kind == "review_requested" and "reviewId" in d:
            self.requests.setdefault(d["reviewId"], []).append(r)
        elif r.kind in ("review_assigned", "review_rebind"):
            self.links.setdefault(d["reviewId"], []).append(r)
        elif r.kind == "transport_observed":
            self.transport.append(r)
        if r.kind in WORKER_ACTS + REVIEW_ACTS and "streamKey" in d:
            if r.kind == "review_acknowledged" and d.get("bind"):
                self.links.setdefault(d["streamKey"], []).append(r)
            self._accept(d, r, r.at)

    def _accept(self, d: dict[str, Any], r: Record, at: float) -> None:
        b = self.book(d["streamKey"])
        b.act_ids[d["actId"]] = (d["actDigest"], r)
        b.last_act_seq = max(b.last_act_seq, d["actSeq"])
        if d["kind"] in ACK_KINDS:
            if b.ack is None:
                b.ack = (r, at)
        else:
            b.progress.append((d["checkpointId"], d["evidenceDigest"], r, at))

    # --- binding chain (030 §8, msgs 1148/1152): linkage + append seq, never `at` -------------------
    def chain(self, rid: str) -> Chain:
        reqs = self.requests.get(rid, [])
        if not reqs:
            return Chain("unknown_key")
        if len(reqs) > 1:
            return Chain("ambiguous_key", detail="more than one review_requested")
        root = reqs[0]
        rd = root.payload
        nodes: dict[str, tuple[Record, str | None]] = {rd["linkId"]: (root, None)}
        for link in self.links.get(rid, []):
            d = link.payload
            lid = d["bind"]["linkId"] if link.kind == "review_acknowledged" else d["linkId"]
            pred = d["bind"]["predecessorBindingId"] if link.kind == "review_acknowledged" else d["predecessorBindingId"]
            if lid in nodes:
                return Chain("ambiguous_binding", requested=root, detail="duplicate link id")
            nodes[lid] = (link, pred)
        succ: dict[str, list[str]] = {}
        for lid, (rec, pred) in nodes.items():
            if pred is None:
                continue
            if pred not in nodes:
                return Chain("ambiguous_binding", requested=root, detail=f"gap: predecessor {pred} missing")
            if nodes[pred][0].seq >= rec.seq:
                return Chain("ambiguous_binding", requested=root, detail="append sequence not increasing")
            succ.setdefault(pred, []).append(lid)
        if any(len(v) > 1 for v in succ.values()):
            return Chain("ambiguous_binding", requested=root, detail="fork")
        history, cur = [], rd["linkId"]
        while True:
            rec, _ = nodes[cur]
            d = rec.payload
            if rec.kind == "review_acknowledged":
                lead, session = d["lead"], d["session"]
            else:
                lead, session = d["lead"], d["leadSession"]
            history.append(Binding(cur, lead, session, rec.seq, rec.at, rec.kind))
            nxt = succ.get(cur)
            if not nxt:
                break
            cur = nxt[0]
        if len(history) != len(nodes):
            return Chain("ambiguous_binding", requested=root, detail="unreachable link")
        return Chain("ok", history[-1], history[:-1], root)


# --- decisions -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    """What the caller does. `append` -> (kind, payload); `counter` -> increment that counter;
    `queue` -> at most one operator-queue entry for (key, reason). A duplicate returns `original`."""
    outcome: str                                  # accepted | observed | duplicate | <counter name> | refused
    append: tuple[str, dict[str, Any]] | None = None
    counter: str | None = None
    queue: str | None = None
    original: Record | None = None
    reason: str = ""
    stream_key: str | None = None


def _counter(name: str, sk: str | None, reason: str = "", queue: str | None = None) -> Decision:
    return Decision(name, counter=name, queue=queue, reason=reason, stream_key=sk)


def _dispatch_for(view: View, key: dict[str, Any]) -> tuple[Record, dict[str, Any]] | None | str:
    runs = view.dispatch.get(key["runId"], [])
    if not runs:
        return None
    if len(runs) > 1:
        return "ambiguous"
    return runs[0]


def _worker_current(key: dict[str, Any], facts: PlanFacts) -> str | None:
    """None when current; else the stale reason. Route A facts are stable through the append."""
    n = facts.node
    if n is None:
        return "no_node"
    if n.get("work_status") != "in_progress" or n.get("attempt_id") != key["attemptId"] or n.get("attempt_epoch") != key["attemptEpoch"]:
        return "not_in_progress_for_attempt"
    if n.get("attempt_content_revision") is None or n.get("attempt_prereq_digest") is None:
        return "stale_content"                                 # PlanRules.PinFailure: unpinned attempt
    if n.get("content_revision") != n.get("attempt_content_revision"):
        return "stale_content"                                 # PlanRules.PinFailure 214-217 (msg 1170)
    pkg = json.loads(key["packageRef"])
    if pkg["kind"] == "supplied.v1" and (pkg["contentRevision"] != n.get("attempt_content_revision")
                                         or pkg["prereqDigest"] != n.get("attempt_prereq_digest")):
        return "package_pins"
    return None


def decide_act(view: View, act: Act, facts: PlanFacts, *, obs_id: str | None = None) -> Decision:
    """030 §6 steps 2-6 for one parsed act (step 1, shape, is parse_act). Route B facts produce an
    audit-only observation at most; only route A facts can produce an effective act."""
    sk = act.stream_key
    book = view.books.get(sk, KeyBook())
    bad = facts_problem(facts)
    if bad:
        return _counter("stale", sk, bad)                          # unprovable facts never make an act current
    # --- step 2/3: current + session ---
    if act.worker:
        found = _dispatch_for(view, act.key)
        if found is None:
            return _counter("stale", sk, "no_dispatch_intent")
        if found == "ambiguous":
            return _counter("stale", sk, "ambiguous_dispatch", queue="ambiguous_key")
        _, dk = found
        if any(dk[f] != act.key[f] for f in ATTEMPT_FIELDS + ("claimKey", "packageRef")):
            return _counter("stale", sk, "dispatch_identity")
        if exec_id(act.key) in view.ends:
            return _counter("stale", sk, "stream_ended")
        end = stream_end(act.key, facts)
        if end is not None:
            return _counter("stale", sk, f"{end[0]}:{end[1]}" if end[1] else end[0])
        why = _worker_current(act.key, facts)
        if why:
            return _counter("stale", sk, why)
        if act.key["workerSession"] != dk["workerSession"] or act.key["workerSession"] != facts.node.get("executor_ref"):
            return _counter("foreign_session", sk, "worker_session", queue="foreign_session")
        bind = None
    else:
        chain = view.chain(sk)
        if chain.status != "ok":
            return _counter("stale", sk, chain.status, queue=chain.status if chain.status != "unknown_key" else None)
        state = review_state(facts.node, act.key)
        if state == "operator_classification":
            return _counter("stale", sk, state, queue="operator_classification")
        if state != "candidate":
            return _counter("stale", sk, f"review_{state}")
        cur = chain.current
        if act.key["lead"] != cur.lead:
            return _counter("foreign_session", sk, "not_the_lead", queue="foreign_session")
        bind = None
        if cur.session is None:
            if act.kind != "review_acknowledged":
                return _counter("foreign_session", sk, "unbound_session", queue="foreign_session")
            bind = {"predecessorBindingId": cur.link_id, "linkId": str(uuid.uuid4())}   # atomic bind+ACK
        elif act.key["leadSession"] != cur.session:
            return _counter("foreign_session", sk, "lead_session", queue="foreign_session")
    # --- step 4: identity of the act ---
    act_digest = hashlib.sha256(act.canonical.encode("utf-8")).hexdigest()
    held = book.act_ids.get(act.act_id)
    if held is None:
        for o in view.observations.get(sk, []):
            if o.payload["act"]["actId"] == act.act_id:
                held = (o.payload["act"]["actDigest"], o)
                break
    if held is not None:
        if held[0] == act_digest:
            return Decision("duplicate", counter="duplicate", original=held[1], stream_key=sk)
        return _counter("conflict", sk, "same ActId, different payload", queue="conflict")
    if act.kind in ACK_KINDS and book.ack is not None:
        return _counter("conflict", sk, "re-ACK", queue="conflict")
    if act.act_seq <= book.last_act_seq:
        return _counter("out_of_order", sk, "actSeq not increasing")
    if act.kind not in ACK_KINDS and book.ack is None:
        return _counter("out_of_order", sk, "progress before an accepted ACK")
    # --- step 5: novelty (fails closed on unreadable bookkeeping) ---
    if act.kind not in ACK_KINDS:
        if view.corrupt:
            return _counter("novelty_unknown", sk, view.corrupt, queue="novelty_unknown")
        if act.checkpoint_id <= book.last_checkpoint or act.evidence_digest in book.digests:
            return _counter("not_novel", sk)
        if len(book.progress) >= G:
            return _counter("progress_overflow", sk, queue="progress_overflow")
    body = {"kind": act.kind, "streamKey": sk, "actId": act.act_id, "actDigest": act_digest, "actSeq": act.act_seq,
            "session": act.session}
    if act.worker:
        body["exec"] = exec_id(act.key)
    else:
        body["reviewId"] = sk
        body["lead"] = act.key["lead"]
    if act.kind not in ACK_KINDS:
        body["checkpointId"], body["evidenceDigest"] = act.checkpoint_id, act.evidence_digest
    if bind:
        body["bind"] = bind
    # --- step 6: append ---
    if facts.route == "A":
        body["facts"] = facts.summary()
        return Decision("accepted", append=(act.kind, body), stream_key=sk)
    if bind:
        body.pop("bind")                      # an unverified observation never binds a session
    if len(view.observations.get(sk, [])) >= V:
        return _counter("observation_overflow", sk)
    return Decision("observed", append=("act_observation", {
        "streamKey": sk, "obsId": obs_id or str(uuid.uuid4()), "act": body, "readFacts": facts.summary(),
        "currentness": "unverified"}), stream_key=sk)


def decide_confirm(view: View, observation_id: str, reconciliation_ref: str) -> Decision:
    """Operator `confirm_act`: makes one route B observation effective, anchored at its ORIGINAL
    observation `at` (View._accept). Steps 4-5 are re-run against the bookkeeping as it is now."""
    obs = view.obs_by_id.get(observation_id)
    if obs is None or not isinstance(reconciliation_ref, str) or not reconciliation_ref.strip():
        return Decision("refused", reason="unknown observation or empty reconciliationRef")
    if observation_id in view.confirmed:
        return Decision("duplicate", original=view.confirmed[observation_id])
    d = obs.payload["act"]
    sk = d["streamKey"]
    book = view.books.get(sk, KeyBook())
    if d["kind"] in REVIEW_ACTS:
        chain = view.chain(sk)
        if chain.status != "ok" or chain.current.session is None or chain.current.session != d["session"]:
            # An unverified observation never binds; the session must already be the current binding.
            return _counter("foreign_session", sk, "confirm needs the current bound lead session", queue="foreign_session")
    if d["actId"] in book.act_ids:
        return _counter("conflict", sk, "act already effective", queue="conflict")
    if d["kind"] in ACK_KINDS and book.ack is not None:
        return _counter("conflict", sk, "re-ACK", queue="conflict")
    if d["actSeq"] <= book.last_act_seq:
        return _counter("out_of_order", sk)
    if d["kind"] not in ACK_KINDS:
        if book.ack is None:
            return _counter("out_of_order", sk, "progress before an accepted ACK")
        if view.corrupt:
            return _counter("novelty_unknown", sk, view.corrupt, queue="novelty_unknown")
        if d["checkpointId"] <= book.last_checkpoint or d["evidenceDigest"] in book.digests:
            return _counter("not_novel", sk)
        if len(book.progress) >= G:
            return _counter("progress_overflow", sk, queue="progress_overflow")
    return Decision("accepted", append=("operator_resolution", {"decision": "confirm_act", "observationId": observation_id,
                                                                "reconciliationRef": reconciliation_ref}), stream_key=sk)


def decide_request(view: View, key: dict[str, Any], facts: PlanFacts) -> Decision:
    """`review_requested` (the chain root) for an exact Done candidate. key: AttemptKey + artifactRef
    + lead + leadSession (null = unbound)."""
    rid = review_id(key)
    if view.requests.get(rid):
        return Decision("duplicate", original=view.requests[rid][0], stream_key=rid)
    if facts.route != "A" or facts_problem(facts) or review_state(facts.node, key) != "candidate":
        return Decision("refused", reason="not a current Done candidate for this exact key", stream_key=rid)
    return Decision("accepted", append=("review_requested", {
        "reviewId": rid, "reviewKey": dict(attempt_of(key), artifactRef=key["artifactRef"]), "lead": key["lead"],
        "leadSession": key["leadSession"], "linkId": str(uuid.uuid4())}), stream_key=rid)


def decide_binding(view: View, rid: str, kind: str, predecessor: str, session: str, *, gate: str, gate_ref: str) -> Decision:
    """`review_assigned` (operator pre-binding of an unbound review) or `review_rebind` (needs the old
    session's release, gate="release" with gate_ref == old session, or gate="operator"). A
    compare-and-set: `predecessor` must be the CURRENT binding at this moment, else stale_binding."""
    chain = view.chain(rid)
    if chain.status != "ok":
        return Decision(chain.status, queue=chain.status if chain.status != "unknown_key" else None, stream_key=rid)
    cur = chain.current
    if predecessor != cur.link_id:
        return Decision("stale_binding", reason=f"predecessor {predecessor} is not the current binding", stream_key=rid)
    m = SESSION.fullmatch(session or "")
    if m is None or m.group(1) != cur.lead:
        return Decision("refused", reason="session must be the lead's hkw1 session", stream_key=rid)
    if kind == "review_assigned":
        if cur.session is not None or gate != "operator" or not gate_ref.strip():
            return Decision("refused", reason="assignment needs an unbound review and an operator reconciliationRef", stream_key=rid)
    elif kind == "review_rebind":
        if cur.session is None:
            return Decision("refused", reason="nothing bound to rebind", stream_key=rid)
        if not ((gate == "release" and gate_ref == cur.session) or (gate == "operator" and gate_ref.strip())):
            return Decision("refused", reason="rebind needs the old session's release or an operator act", stream_key=rid)
    else:
        raise ValueError(kind)
    return Decision("accepted", append=(kind, {"reviewId": rid, "predecessorBindingId": predecessor, "linkId": str(uuid.uuid4()),
                                               "lead": cur.lead, "leadSession": session, "gate": gate, "gateRef": gate_ref}),
                    stream_key=rid)


# --- deadlines (030 §7): one live phase per stream, original anchors only ------------------------------

@dataclass(frozen=True)
class Phase:
    name: str                  # ack | first_progress | progress | ended
    anchor_kind: str | None
    anchor_at: float | None
    deadline: float | None

    def overdue(self, now: float) -> bool:
        return self.deadline is not None and now >= self.deadline


def phase_of(start: Record | None, book: KeyBook, ended: bool, windows: tuple[float, float, float]) -> Phase:
    ack_w, first_w, prog_w = windows
    if ended:
        return Phase("ended", None, None, None)
    if book.ack is None:
        if start is None:
            return Phase("ack", None, None, None)
        return Phase("ack", start.kind, start.at, start.at + ack_w)
    if not book.progress:
        rec, at = book.ack
        return Phase("first_progress", rec.kind, at, at + first_w)
    _, _, rec, at = max(book.progress, key=lambda p: p[0])          # last NOVEL progress by checkpoint, never by `at`
    return Phase("progress", rec.kind, at, at + prog_w)


def check_windows(windows: tuple[int, int, int]) -> tuple[int, int, int]:
    for w in windows:
        wire_int(w, "window", WINDOW_MIN, WINDOW_MAX)
    return windows


# --- C-B (030 §8): exact identity, per-field proof, bounded historical page --------------------------------

class Refused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _resolve_exec(view: View, req: dict[str, Any]) -> tuple[Record, dict[str, Any]]:
    if "executionKey" in req:
        exact_fields(req, ("executionKey",), "request")
        want = parse_exec_key(req["executionKey"])
        found = _dispatch_for(view, want)
        if found is None:
            raise Refused("unknown_key")
        if found == "ambiguous":
            raise Refused("ambiguous_key")
        diff = [f for f in EXEC_FIELDS if found[1][f] != want[f]]
        if diff:
            raise Refused("identity_mismatch", diff)
        return found
    optional = ("claimKey", "packageRef", "workerSession")
    allowed = {"attemptKey", "runId", *optional}
    if not isinstance(req, dict) or "attemptKey" not in req or not set(req) <= allowed:
        raise Malformed("request", "unknown request form")
    ak = parse_attempt(exact_fields(req["attemptKey"], ATTEMPT_FIELDS, "attemptKey"), "attemptKey")
    if "runId" in req:
        wire_str(req["runId"], "runId", PRINTABLE, 256)
        runs = view.dispatch.get(req["runId"], [])
    else:
        runs = [x for rs in view.dispatch.values() for x in rs if attempt_of(x[1]) == ak]
    if not runs:
        raise Refused("unknown_key")
    if len(runs) > 1:
        raise Refused("ambiguous_key")
    rec, key = runs[0]
    diff = [f for f in ATTEMPT_FIELDS if key[f] != ak[f]] + [f for f in optional if f in req and req[f] != key[f]]
    if diff:
        raise Refused("identity_mismatch", diff)
    return rec, key


def _resolve_review(view: View, req: dict[str, Any]) -> tuple[str, Chain, dict[str, Any]]:
    if "reviewKey" in req:
        exact_fields(req, ("reviewKey",), "request")
        want = parse_review_key(req["reviewKey"], session_required=False)
    else:
        exact_fields(req, ("attemptKey", "artifactRef"), "request")
        ak = parse_attempt(exact_fields(req["attemptKey"], ATTEMPT_FIELDS, "attemptKey"), "attemptKey")
        want = dict(ak, artifactRef=wire_str(req["artifactRef"], "artifactRef", PRINTABLE, 512))
    rid = review_id(want)
    chain = view.chain(rid)
    if chain.status != "ok":
        raise Refused(chain.status, chain.detail or None)
    cur = chain.current
    key = dict(attempt_of(want), artifactRef=want["artifactRef"], lead=cur.lead, leadSession=cur.session)
    if "reviewKey" in req:
        if want["lead"] != cur.lead:
            raise Refused("identity_mismatch", ["lead"])
        if want["leadSession"] != cur.session:
            if any(b.session == want["leadSession"] and want["leadSession"] is not None for b in chain.history):
                raise Refused("stale_binding", {"currentBindingAt": cur.at})
            raise Refused("identity_mismatch", ["leadSession"])
    return rid, chain, key


def _pending_proof(status: str) -> str:
    """A pending append whose confirm() status is not `committed` proves nothing (029 rules)."""
    return status if status in ("not_observed", "compacted") else "missing"


def read_cb(view: View, request: dict[str, Any], facts: PlanFacts, *, now: float, counters: dict[str, dict[str, Any]] | None = None,
            queue: Iterable[tuple[str, str]] = (), pending: dict[str, str] | None = None, latest_revision: int | None = None,
            after: int = 0, limit: int = PAGE_DEFAULT,
            windows: tuple[int, int, int] = (ACK_WINDOW, FIRST_PROGRESS_WINDOW, PROGRESS_WINDOW),
            selector: dict[str, str] | None = None) -> dict[str, Any]:
    """One C-B response for one key. Read-only. `facts.route == "A"` means `facts` and `view` came
    from ONE combined snapshot; otherwise `current` is unknown. `pending` maps a stream key's
    unconfirmed append ("ack" / "progress") to its confirm() status. Raises Refused / Malformed for
    identity problems (nothing is guessed)."""
    wire_int(limit, "limit", 1, PAGE_MAX)
    wire_int(after, "after", 0)
    counters, pending = counters or {}, pending or {}
    if "executionKey" in request or ("attemptKey" in request and "artifactRef" not in request):
        start, key = _resolve_exec(view, request)
        sk, worker = exec_id(key), True
        identity = {"executionKey": key}
        chain = None
    else:
        sk, chain, key = _resolve_review(view, request)
        start, worker = chain.requested, False
        identity = {"reviewKey": key}
    # The resolved identity, the stream/node the caller selected, and the PlanStore row that was
    # read must all agree BEFORE anything is derived (msg 1188): facts of another node never answer.
    if selector is not None:
        diff = [f for f in ("rootId", "nodeId") if selector.get(f) != key[f]]
        if worker and selector.get("claimKey") != key["claimKey"]:
            diff.append("claimKey")
        if diff:
            raise Refused("selector_mismatch", diff)
    if facts.node is not None and "node_id" in facts.node and facts.node["node_id"] != key["nodeId"]:
        raise Refused("selector_mismatch", ["facts.nodeId"])
    book = view.books.get(sk, KeyBook())
    basis = dict(facts.basis, route=facts.route, revision=facts.revision)
    out: dict[str, Any] = {"identity": identity, "basis": basis, "counters": counters.get(sk, {}),
                           "queue": sorted(r for k, r in queue if k == sk)}
    # --- historical (as-of evidence; never current), bounded and paged ---
    hist: list[dict[str, Any]] = []
    for o in view.observations.get(sk, []):
        hist.append({"seq": o.seq, "kind": "act_observation", "currentness": "unverified", "asOf": o.payload["readFacts"],
                     "confirmed": o.payload["obsId"] in view.confirmed})
    if chain is not None:
        hist += [{"seq": b.seq, "kind": "binding", "leadSession": b.session, "at": b.at} for b in chain.history]
    for t in view.transport:
        hist.append({"seq": t.seq, "kind": "transport_observed", "fact": t.payload.get("fact"), "asOf": t.at})
    if worker and sk in view.ends:
        e = view.ends[sk]
        hist.append({"seq": e.seq, "kind": e.kind, "reason": e.payload.get("reason")})
    hist.sort(key=lambda h: h["seq"])
    page = [h for h in hist if h["seq"] > after][:limit + 1]
    out["historical"] = {"items": page[:limit], "truncated": len(page) > limit,
                         "nextCursor": page[limit - 1]["seq"] if len(page) > limit else None}
    # --- current: only from one combined (route A) snapshot ---
    if facts.route != "A":
        out["current"] = "unknown"
        return out
    bad = facts_problem(facts)
    if bad:
        out["current"] = "unknown"                                 # missing/unrepresentable authority: no live phase
        out["unknownReason"] = bad
        return out
    stale_basis = latest_revision is not None and facts.revision is not None and facts.revision < latest_revision
    rstate = None
    if worker:
        end = stream_end(key, facts)
        ended = end is not None
    else:
        rstate = review_state(facts.node, key)
        end = {"decided": ("ended", "decided"), "moot": ("moot", None)}.get(rstate)
        ended = end is not None
    ph = phase_of(start, book, ended, windows)
    # Required proof per field (msg 1145): an ACK proven in the validated prefix stays proven even
    # when later progress bookkeeping is damaged; corruption that reaches the ACK makes it unknown.
    ack_pending, prog_pending = pending.get(f"{sk}:ack"), pending.get(f"{sk}:progress")
    if book.ack is not None:
        ack_proof = "complete"                                     # in the validated prefix: proven
    elif ack_pending not in (None, "committed"):
        ack_proof = _pending_proof(ack_pending)                    # an ACK append whose outcome is unknown
    elif view.corrupt:
        ack_proof = "corrupt"                                      # the damaged suffix may hide the ACK itself
    else:
        ack_proof = "complete"                                     # proven absent: no ACK yet
    if ack_proof != "complete":
        prog_proof = ack_proof                                     # progress depends on the ACK
    elif view.corrupt:
        prog_proof = "corrupt"
    elif prog_pending not in (None, "committed"):
        prog_proof = _pending_proof(prog_pending)
    else:
        prog_proof = "complete"
    phase_proof = ack_proof if ph.name in ("ack", "first_progress") else prog_proof
    if ph.name == "first_progress" and prog_proof != "complete":
        phase_proof = prog_proof                                   # a hidden progress could have ended this phase
    if ph.name == "ended":
        phase_proof = "complete"
    fields: dict[str, Any] = {}

    def put(name: str, value: Any, proof: str) -> None:
        if stale_basis:
            proof = "unverified"
        fields[name] = {"value": value if proof == "complete" else "unknown", "proof": proof}

    if worker:
        if ended:
            status = end[0] if end[0] == "finished" else f"superseded:{end[1]}"
        elif book.ack is None:
            status = "ack_overdue" if ph.overdue(now) else ("delivered" if (sk in view.outcomes and view.outcomes[sk].payload.get("outcome") == "delivered")
                                                            else "dispatch_intended")
        elif not book.progress:
            status = "first_progress_overdue" if ph.overdue(now) else "taken_up_self_reported"
        else:
            status = "progress_overdue" if ph.overdue(now) else "progress_self_reported"
        status_proof = phase_proof if not ended else "complete"
        put("status", status, status_proof)
    else:
        status = end[0] if ended else ("review_requested" if book.ack is None else
                                       "review_progressing" if book.progress else "review_acknowledged")
        if not ended and ph.overdue(now):
            status = "review_overdue"
        if rstate == "operator_classification":
            status = "operator_classification"                     # a decision against another identity: explicit, not ended
        put("review", status, phase_proof if not ended and rstate != "operator_classification" else "complete")
        if rstate == "decided":
            # The decision on this exact key occurred; whether the acceptance still holds by
            # prerequisites needs whole-graph PlanRules and is reported separately (msg 1176).
            fields["acceptanceValidity"] = {"value": "unknown", "proof": "unverified"}
    put("ack", {"seq": book.ack[0].seq, "at": book.ack[1]} if book.ack else None, ack_proof)
    put("progress", {"checkpoints": len(book.progress), "lastCheckpoint": book.last_checkpoint,
                     "capReached": len(book.progress) >= G} if book.ack else None, prog_proof)
    put("deadlinePhase", {"phase": ph.name, "anchorKind": ph.anchor_kind, "anchorAt": ph.anchor_at, "deadline": ph.deadline,
                          "overdue": ph.overdue(now)}, phase_proof)
    fields["prerequisites"] = {"value": "unknown", "proof": "unverified"}       # needs whole-graph PlanRules (msg 1170)
    out["current"] = fields
    if stale_basis:
        out["staleFields"] = sorted(k for k in fields if k != "prerequisites")
    body = canonical(out)
    if len(body.encode("utf-8")) > BODY_MAX:
        raise Refused("body_too_large")
    return out


# --- in-memory model (reuses ModelJournal: reservations, required outcomes, bounded records) ------------

@dataclass
class Counters:
    values: dict[str, dict[str, int]] = field(default_factory=dict)
    saturated: dict[str, set[str]] = field(default_factory=dict)

    def bump(self, sk: str, name: str) -> None:
        d = self.values.setdefault(sk, {})
        cur = d.get(name, 0)
        if cur >= COUNTER_MAX:
            self.saturated.setdefault(sk, set()).add(name)
            return
        d[name] = cur + 1
        if d[name] >= COUNTER_MAX:
            self.saturated.setdefault(sk, set()).add(name)

    def view(self) -> dict[str, dict[str, Any]]:
        return {sk: dict(v, saturated=sorted(self.saturated.get(sk, ()))) for sk, v in self.values.items()}


class ActsModel:
    """Model-level E2c over ONE (root, claimKey) stream of a ModelJournal. Facts are supplied by the
    test (route A = 'as if read inside the locked transaction'); e1/acts_durable.py makes route A real."""

    def __init__(self, root: str, claim_key: str, *, writer: str = "supervisor-e2c", bounds: Bounds = E2C_BOUNDS, now: float = 0.0):
        self.mj = ModelJournal(writer, bounds, now=now)
        self.root, self.ck = root, claim_key
        self.counters = Counters()
        self.queue: set[tuple[str, str]] = set()
        self.corrupt: str | None = None            # TEST: simulate a damaged suffix (reader returns a prefix)
        self.corrupt_from: int | None = None
        self.mj.append(root, claim_key, "claim_intent", {"attemptId": "e2c"})
        self.mj.append(root, claim_key, "claimed", {"outcome": "claimed"})

    @property
    def now(self) -> float:
        return self.mj.now

    @now.setter
    def now(self, v: float) -> None:
        self.mj.now = v

    def records(self) -> list[Record]:
        recs = self.mj.streams[(self.root, self.ck)].records
        return [r for r in recs if self.corrupt_from is None or r.seq < self.corrupt_from]

    def view(self) -> View:
        return View(self.records(), self.corrupt)

    def _apply(self, d: Decision) -> Decision:
        ck = self.view().counter_key(d.stream_key)
        if d.counter and ck:
            self.counters.bump(ck, d.counter)
        if d.queue and ck:
            self.queue.add((ck, d.queue))                     # a set: at most one per key per reason
        if d.append:
            self.mj.append(self.root, self.ck, *d.append)
        return d

    def dispatch(self, key: dict[str, Any], facts: PlanFacts, *, allow_duplicate_run: bool = False) -> str:
        """dispatch_intent (+ its package_ref). The claim's executorRef must conform and equal the
        session (not dispatchable otherwise: nothing recorded, one operator entry)."""
        key = parse_exec_key(key)
        n = facts.node or {}
        if not isinstance(n.get("executor_ref"), str) or not SESSION.fullmatch(n["executor_ref"]) or n["executor_ref"] != key["workerSession"]:
            self.queue.add((exec_id(key), "not_dispatchable"))
            raise Refused("not_dispatchable")
        if self.view().dispatch.get(key["runId"]) and not allow_duplicate_run:
            raise Refused("run_id_reused")
        sha = hashlib.sha256(key["packageRef"].encode("utf-8")).hexdigest()
        if sha not in self.view().packages:
            self.mj.append(self.root, self.ck, "package_ref", {"packageSha256": sha, "jcs": key["packageRef"]})
        stored = {f: key[f] for f in EXEC_FIELDS if f != "packageRef"}
        stored["packageSha256"] = sha
        self.mj.append(self.root, self.ck, "dispatch_intent", {"exec": exec_id(key), "key": stored})
        return exec_id(key)

    def dispatch_outcome(self, eid: str, outcome: str, message_id: str | None = None) -> None:
        self.mj.append(self.root, self.ck, "dispatch_outcome", {"exec": eid, "outcome": outcome, "messageId": message_id})

    def intake(self, raw: bytes | str, facts: PlanFacts) -> Decision:
        try:
            act = parse_act(raw)
            check_stream(act, self.root, self.ck)
        except Malformed as e:
            self.counters.bump("_stream", "malformed")
            return Decision("malformed", counter="malformed", reason=e.code)
        return self._apply(decide_act(self.view(), act, facts))

    def confirm(self, observation_id: str, reconciliation_ref: str) -> Decision:
        return self._apply(decide_confirm(self.view(), observation_id, reconciliation_ref))

    def request_review(self, key: dict[str, Any], facts: PlanFacts) -> Decision:
        return self._apply(decide_request(self.view(), key, facts))

    def bind(self, rid: str, kind: str, predecessor: str, session: str, *, gate: str, gate_ref: str) -> Decision:
        return self._apply(decide_binding(self.view(), rid, kind, predecessor, session, gate=gate, gate_ref=gate_ref))

    def record_end(self, key: dict[str, Any], facts: PlanFacts) -> Decision:
        if facts.route != "A":
            return Decision("refused", reason="stream ends are recorded from route A facts only")
        end = stream_end(key, facts)
        if end is None or exec_id(key) in self.view().ends:
            return Decision("refused" if end is None else "duplicate")
        return self._apply(Decision("accepted", append=(end[0], {"exec": exec_id(key), "reason": end[1]})))

    def transport(self, fact: str, ref: str) -> None:
        """A transport/launch fact (bridge ACK, launched{pid}, completed{artifact_ref}): historical only."""
        self.mj.append(self.root, self.ck, "transport_observed", {"source": "agent-bridge", "fact": fact, "ref": ref})

    def read(self, request: dict[str, Any], facts: PlanFacts, **kw) -> dict[str, Any]:
        return read_cb(self.view(), request, facts, now=kw.pop("now", self.now), counters=self.counters.view(),
                       queue=self.queue, **kw)
