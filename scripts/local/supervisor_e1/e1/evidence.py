"""E2a evidence model (plan 026 rev 5; test-only). Pure: no I/O, no clock of its own.

A CONCEPTUAL model of the supervisor journal: it shows which facts suffice to classify every
crash point and enforces the 026 bounds. It makes NO durability, restart, fsync, atomicity or
wake guarantee — it is an in-memory list. Nothing here performs or authorizes an effect:
classify() returns operator-only resolutions and never an automatic action.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# --- record vocabulary (026 §4) -----------------------------------------------

INTENTS = {
    # intent kind -> terminal record kinds reserved for it before the effect
    "claim_intent": ("claimed",),
    "launch_intent": ("launched", "exited", "result_captured"),   # launched/exited are MODEL-ONLY in E2a
    "finish_intent": ("finish_outcome",),
    "notify_intent": ("notify_outcome",),
    "dispatch_intent": ("dispatch_outcome",),                      # E2c (plan 030 C-D): send to an agent session
}
TERMINALS = {t for ts in INTENTS.values() for t in ts}
RESERVE_KINDS = {"fault", "operator_resolution"}       # use the stream's fault/operator reserve
ORDINARY = {"review_requested", "review_acknowledged", "review_progress"}
# E2c evidence (plan 030 C-D; e1/acts.py). Ordinary, refusable records in the same streams; the E2a
# classifier does not interpret them (a stream carrying them may classify "unclassified").
E2C_ORDINARY = {"package_ref", "worker_ack", "worker_progress", "review_assigned", "review_rebind", "act_observation",
                "superseded_as_of_read", "finished", "superseded", "transport_observed", "consumed_observed"}
KINDS = set(INTENTS) | TERMINALS | RESERVE_KINDS | ORDINARY | E2C_ORDINARY
FAULT_RESERVE = 2
# Operator decisions that CONFIRM a terminal outcome (may resolve a stream). Anything else -- e.g.
# "release" whose reply is unknown, "retry_permitted_once", "new_claim_permitted" -- is permission
# to act, not a confirmed resolution.
TERMINAL_RESOLUTIONS = {"confirmed_released", "confirmed_finished", "abandoned_no_effects"}

REF_FIELDS_MAX = 512      # any key ending in "Ref"/"ref"
NOTE_FIELDS_MAX = 1024    # note / finding / reason
COMMAND_MAX = 1024        # command
OTHER_STR_MAX = 1024      # anything else: no blobs
MAX_DEPTH = 4
MAX_ITEMS = 32            # per dict or list
RECORD_MAX_BYTES = 2048   # exact encoded size of a WHOLE record (payload + kind/writer/root/key/seq/at)
MAX_AT = 1e12             # model clock: finite, non-negative, bounded (fake time)
# Longest JSON serialization of ANY admissible clock value (shortest round-trip repr of a finite double
# in [0, MAX_AT] is at most 17 significant digits + "." + "e-ddd", e.g. "2.2250738585072014e-308").
# Worst-case accounting uses this width, not the width of MAX_AT itself.
AT_REPR_MAX = 23
MAX_SEQ = 2**63 - 1
FALLBACK_CODE_MAX = 32    # codes in the invalidPayload fallback are truncated to this


class JournalRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _positive_int(name: str, v: object) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValueError(f"{name} must be a positive int, got {v!r}")


def _positive_num(name: str, v: object) -> None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0 or v != v:
        raise ValueError(f"{name} must be a positive number, got {v!r}")


@dataclass(frozen=True)
class Bounds:
    per_stream: int = 64           # N (records + reserved slots)
    unresolved_streams: int = 32   # M
    global_records: int = 2000     # S (active + resolved-uncompacted + summaries; reserved slots count)
    global_bytes: int = 1 << 20    # B (exact encoded bytes; reserved slots count at RECORD_MAX_BYTES)
    compact_after: float = 30 * 86400.0    # D
    summary_retention: float = 90 * 86400.0  # R
    notify_max: int = 3            # K

    def __post_init__(self) -> None:
        for n in ("per_stream", "unresolved_streams", "global_records", "global_bytes", "notify_max"):
            _positive_int(n, getattr(self, n))
        for n in ("compact_after", "summary_retention"):
            _positive_num(n, getattr(self, n))
        if self.global_bytes < 8 * RECORD_MAX_BYTES:
            raise ValueError("global_bytes must hold at least one fully reserved stream")


def _check_payload(v: Any, key: str = "", depth: int = 0) -> None:
    """Typed, bounded payload: str/int/bool/None, and dict/list nested up to MAX_DEPTH."""
    if depth > MAX_DEPTH:
        raise JournalRefused("payload_shape", "nested too deep")
    if v is None or isinstance(v, bool):
        return
    if isinstance(v, int):
        if abs(v) > 2**63 - 1:
            raise JournalRefused("payload_shape", f"{key} integer out of Int64")
        return
    if isinstance(v, str):
        n = len(v.encode("utf-8"))
        k = key.lower()
        limit = (REF_FIELDS_MAX if k.endswith("ref") else NOTE_FIELDS_MAX if k in ("note", "finding", "reason")
                 else COMMAND_MAX if k == "command" else OTHER_STR_MAX)
        if n > limit:
            raise JournalRefused("byte_cap", f"{key} is {n} bytes > {limit}")
        return
    if isinstance(v, dict):
        if len(v) > MAX_ITEMS:
            raise JournalRefused("payload_shape", "too many keys")
        for k2, v2 in v.items():
            if not isinstance(k2, str):
                raise JournalRefused("payload_shape", "non-string key")
            _check_payload(v2, k2, depth + 1)
        return
    if isinstance(v, (list, tuple)):
        if len(v) > MAX_ITEMS:
            raise JournalRefused("payload_shape", "too many items")
        for item in v:
            _check_payload(item, key, depth + 1)
        return
    raise JournalRefused("payload_shape", f"{key}: unsupported type {type(v).__name__}")


@dataclass(frozen=True)
class Record:
    seq: int
    kind: str
    writer: str
    root: str
    claim_key: str
    at: float
    data: str          # canonical JSON of the record payload (immutable)

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.data)

    @property
    def degraded(self) -> bool:
        """True when the record is the bounded invalidPayload fallback: the required content is missing."""
        return bool(self.payload.get("invalidPayload"))

    @property
    def size(self) -> int:
        return encoded_size(self.kind, self.writer, self.root, self.claim_key, self.seq, self.at, self.data)


def encoded_size(kind: str, writer: str, root: str, claim_key: str, seq: int, at: float, data: str) -> int:
    """Exact UTF-8 size of the record as it would be retained (all metadata included)."""
    return len(json.dumps({"kind": kind, "writer": writer, "root": root, "key": claim_key, "seq": seq, "at": at,
                           "data": data}, separators=(",", ":")).encode("utf-8"))


@dataclass
class Stream:
    root: str
    claim_key: str
    writer: str
    records: list[Record] = field(default_factory=list)
    reserved: int = 0                                  # slots set aside for terminal/fault records
    outstanding: list[list[str]] = field(default_factory=list)   # remaining terminal kinds per open intent
    fault_reserved: int = 0
    resolved_at: float | None = None
    summary: str | None = None                         # canonical JSON summary when compacted (records dropped)

    @property
    def key(self) -> tuple[str, str]:
        return (self.root, self.claim_key)

    def count(self) -> int:
        return 1 if self.summary is not None else len(self.records) + self.reserved

    def nbytes(self) -> int:
        if self.summary is not None:
            return len(self.summary.encode("utf-8"))
        # Reserved slots are budgeted at the TRUE worst case of any record that may consume them.
        return sum(r.size for r in self.records) + self.reserved * RECORD_MAX_BYTES


def _fallback_text(code: str) -> str:
    return json.dumps({"code": code[:FALLBACK_CODE_MAX], "invalidPayload": True}, sort_keys=True, separators=(",", ":"))


_RESERVED_KINDS_LONGEST = max(TERMINALS | RESERVE_KINDS, key=len)


def worst_reserved_record_size(writer: str, root: str, claim_key: str) -> int:
    """Worst-case encoded size of ANY record that may consume a reservation in this stream (the
    bounded invalidPayload fallback with a maximal code, the longest reserved kind, the largest seq
    and the largest bounded clock value). An intent is admitted only if this fits RECORD_MAX_BYTES."""
    at0 = encoded_size(_RESERVED_KINDS_LONGEST, writer, root, claim_key, MAX_SEQ, 0, _fallback_text("x" * FALLBACK_CODE_MAX))
    return at0 - len("0") + AT_REPR_MAX


class ModelJournal:
    """Append-only, single-writer streams keyed (rootId, claimKey). In memory only."""

    def __init__(self, writer: str, bounds: Bounds | None = None, *, now: float = 0.0):
        self.writer = writer
        self.bounds = bounds or Bounds()
        self.streams: dict[tuple[str, str], Stream] = {}
        self._seq = 0
        self.now = now

    @property
    def now(self) -> float:
        return self._now

    @now.setter
    def now(self, value: float) -> None:
        # Validated on assignment, so a reserved outcome can never meet an invalid clock (and be refused).
        if not (isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= MAX_AT
                and len(json.dumps(value)) <= AT_REPR_MAX):
            raise JournalRefused("clock", "model clock must be finite, non-negative and <= MAX_AT")
        self._now = value

    def reserved_seqs(self) -> int:
        """Sequence numbers promised to reserved outcome/fault slots across all streams."""
        return sum(st.reserved for st in self.streams.values())

    # --- totals across ALL retained data (active, resolved-uncompacted, summaries) ---
    def total_count(self) -> int:
        return sum(s.count() for s in self.streams.values())

    def total_bytes(self) -> int:
        return sum(s.nbytes() for s in self.streams.values())

    def unresolved(self) -> int:
        return sum(1 for s in self.streams.values() if s.resolved_at is None)

    def _plan_room(self, count: int, nbytes: int) -> list[tuple[str, Stream]]:
        """PROSPECTIVE eviction plan (no mutation): only RESOLVED data, oldest first -- drop summaries,
        then compact resolved streams. Raises when even the full plan cannot make room, leaving the
        model untouched."""
        c, b = self.total_count(), self.total_bytes()

        def fits() -> bool:
            return c + count <= self.bounds.global_records and b + nbytes <= self.bounds.global_bytes
        plan: list[tuple[str, Stream]] = []
        if fits():
            return plan
        for st in sorted((x for x in self.streams.values() if x.summary is not None), key=lambda x: x.resolved_at or 0):
            c -= st.count()
            b -= st.nbytes()
            plan.append(("drop", st))
            if fits():
                return plan
        for st in sorted((x for x in self.streams.values() if x.resolved_at is not None and x.summary is None
                          and not unsafe_resolved(x.records)),             # HK-ISSUE-017 legacy: retained, never evicted
                         key=lambda x: x.resolved_at or 0):
            summary = self._summary_text(st)
            c += 1 - st.count()
            b += len(summary.encode("utf-8")) - st.nbytes()
            plan.append(("compact", st))
            if fits():
                return plan
        raise JournalRefused("global_cap", "no resolved data left to evict")

    def _apply_room(self, plan: list[tuple[str, Stream]]) -> None:
        for action, st in plan:
            if action == "drop":
                del self.streams[st.key]
            else:
                self._compact(st)

    def _encode(self, kind: str, w: str, root: str, claim_key: str, data: dict[str, Any]) -> tuple[str, int]:
        _check_payload(data)
        text = json.dumps(data, sort_keys=True, separators=(",", ":"))
        size = encoded_size(kind, w, root, claim_key, self._seq + 1, self.now, text)
        if size > RECORD_MAX_BYTES:
            raise JournalRefused("record_cap", f"{kind} encodes to {size} > {RECORD_MAX_BYTES} bytes")
        return text, size

    def append(self, root: str, claim_key: str, kind: str, data: dict[str, Any], *, writer: str | None = None) -> Record:
        if kind not in KINDS:
            raise JournalRefused("unknown_kind", kind)
        w = writer or self.writer
        key = (root, claim_key)
        s = self.streams.get(key)
        new_stream = s is None
        if s is None:
            if kind != "claim_intent":
                raise JournalRefused("no_stream", f"{kind} before claim_intent")
            if self.unresolved() >= self.bounds.unresolved_streams:
                raise JournalRefused("unresolved_cap")
            s = Stream(root, claim_key, w)
        else:
            if s.writer != w:
                raise JournalRefused("foreign_writer", f"stream owned by {s.writer}")
            if s.summary is not None or s.resolved_at is not None:
                raise JournalRefused("resolved", "stream is resolved")

        # Does this record consume a reservation made before its effect?
        consumes = None
        if kind in TERMINALS:
            consumes = next((o for o in s.outstanding if kind in o), None)
            if consumes is None:
                raise JournalRefused("no_open_intent", f"{kind} without a matching intent")
        elif kind in RESERVE_KINDS and s.fault_reserved > 0:
            consumes = "fault"

        if consumes is not None:
            # Already admitted (count and worst-case bytes). It is NEVER refused: an outcome that does
            # not fit its allowed shape is recorded as bounded fault evidence under the same reservation.
            try:
                text, _ = self._encode(kind, w, root, claim_key, data)
            except JournalRefused as e:
                # Guaranteed to fit: the intent was admitted only with this worst case in headroom.
                text = _fallback_text(e.code)
            if consumes == "fault":
                s.fault_reserved -= 1
            else:
                consumes.remove(kind)
                s.outstanding = [o for o in s.outstanding if o]
            s.reserved -= 1
        else:
            text, size = self._encode(kind, w, root, claim_key, data)
            if kind in INTENTS:
                if worst_reserved_record_size(w, root, claim_key) > RECORD_MAX_BYTES:
                    raise JournalRefused("metadata_headroom", "writer/root/key leave no room for a reserved outcome")
                terminals = list(INTENTS[kind])
                extra_fault = FAULT_RESERVE - s.fault_reserved
                slots = len(terminals) + extra_fault
                if self._seq + self.reserved_seqs() + 1 + slots > MAX_SEQ:
                    raise JournalRefused("seq_exhausted", f"{kind} needs {1 + slots} sequence numbers incl. reserved slots")
                if s.count() + 1 + slots > self.bounds.per_stream:
                    raise JournalRefused("stream_cap", f"{kind} needs {1 + slots} slots incl. reserved terminal/fault capacity")
                self._apply_room(self._plan_room(1 + slots, size + slots * RECORD_MAX_BYTES))
                s.reserved += slots
                s.fault_reserved += extra_fault
                s.outstanding.append(terminals)
            else:   # ordinary workflow records (and reserve kinds without reserve left): refusable
                if self._seq + self.reserved_seqs() + 1 > MAX_SEQ:
                    raise JournalRefused("seq_exhausted", kind)
                if s.count() + 1 > self.bounds.per_stream:
                    raise JournalRefused("stream_cap", kind)
                self._apply_room(self._plan_room(1, size))

        self._seq += 1
        rec = Record(self._seq, kind, w, root, claim_key, self.now, text)
        s.records.append(rec)
        if new_stream:
            self.streams[key] = s
        return rec

    def resolve(self, root: str, claim_key: str, *, planstore_decided: bool = False) -> None:
        """Mark a stream resolved (and therefore compactable). Requires EXPLICIT terminal evidence:
        - a POSITIVE finish outcome (applied/unchanged, valid payload) with nothing outstanding AND an
          authoritative PlanStore decision for that attempt (`planstore_decided`), or
        - an operator_resolution whose decision is in TERMINAL_RESOLUTIONS with a reconciliationRef.
        Rejected/unknown/invalid outcomes, a release with an unknown reply, or mere permissions
        never resolve a stream. Outstanding reservations are never released silently."""
        s = self.streams[(root, claim_key)]
        if s.resolved_at is not None:
            return
        finished = is_confirmed_finish(s.records) and planstore_decided and not s.outstanding
        if not finished and not is_terminal_resolution(s.records):
            raise JournalRefused("unresolved_outstanding", "needs a confirmed finish + decision, or a terminal operator resolution")
        if not finished:
            f = final_resolution(s.records)
            if f.problem == "resolution_not_final":
                raise JournalRefused("resolution_not_final", f"{f.after_count}: {','.join(f.after_ids)}")
        s.resolved_at = self.now
        s.reserved, s.fault_reserved, s.outstanding = 0, 0, []

    @staticmethod
    def _summary_text(s: Stream) -> str:
        kinds = [r.kind for r in s.records]
        last = {r.kind: r.payload for r in s.records}
        return json.dumps({"root": s.root, "claimKey": s.claim_key, "writer": s.writer, "resolvedAt": s.resolved_at,
                           "kinds": kinds[-8:], "finishOutcome": last.get("finish_outcome"),
                           "resolution": last.get("operator_resolution")}, sort_keys=True, separators=(",", ":"))

    def _compact(self, s: Stream) -> None:
        if s.resolved_at is None:
            raise JournalRefused("unresolved_outstanding", "only resolved streams compact")
        s.summary = self._summary_text(s)
        s.records = []

    def compact(self) -> None:
        """Resolved streams older than D become summaries; summaries older than R are dropped.
        Unresolved streams are never touched, nor are legacy unsafe resolved ones (HK-ISSUE-017): kept with records."""
        for s in list(self.streams.values()):
            if s.resolved_at is None or (s.summary is None and unsafe_resolved(s.records)):
                continue
            age = self.now - s.resolved_at
            if s.summary is None and age >= self.bounds.compact_after:
                self._compact(s)
            elif s.summary is not None and age >= self.bounds.summary_retention:
                del self.streams[s.key]

    def prefix(self, root: str, claim_key: str, n: int) -> list[Record]:
        """The first n records of a stream: a simulated crash keeps only these (conceptual)."""
        return list(self.streams[(root, claim_key)].records[:n])


def unanswered_intents(records: list[Record]) -> list[tuple[Record, list[str]]]:
    """Every intent whose reserved terminals are not all recorded, with the terminals still awaited. FIFO pairing over
    the WHOLE prefix: each terminal record answers the FIRST open intent still awaiting its kind (the single
    implementation shared by handoff.pending_effects and final_resolution; HK-ISSUE-017)."""
    open_: list[tuple[Record, list[str]]] = []
    for r in records:
        if r.kind in INTENTS:
            open_.append((r, list(INTENTS[r.kind])))
        for o in open_:
            if r.kind in o[1]:
                o[1].remove(r.kind)
                break
    return [(r, rest) for r, rest in open_ if rest]


FINAL_IDS_MAX = 8


@dataclass(frozen=True)
class FinalResolution:
    problem: str | None               # None | "no_terminal_resolution" | "resolution_not_final"
    after_ids: tuple[str, ...] = ()   # "<kind>@<seq>" of unanswered intents AFTER the terminal resolution (first 8)
    after_count: int = 0


def final_resolution(records: list[Record]) -> FinalResolution:
    """Whether the stream's LAST operator_resolution is terminal (is_terminal_resolution, unchanged) and FINAL: no intent
    recorded after it is unanswered. A terminal resolution closes only the intents recorded BEFORE it (HK-ISSUE-017)."""
    if not is_terminal_resolution(records):
        return FinalResolution("no_terminal_resolution")
    t = [r for r in records if r.kind == "operator_resolution"][-1]
    after = [f"{r.kind}@{r.seq}" for r, _ in unanswered_intents(records) if r.seq > t.seq]
    if after:
        return FinalResolution("resolution_not_final", tuple(after[:FINAL_IDS_MAX]), len(after))
    return FinalResolution(None)


def unsafe_resolved(records: list[Record]) -> bool:
    """A stream resolved by a terminal resolution that was NOT final (written before HK-ISSUE-017's resolve guard):
    compaction must retain its records, never summarize or evict them."""
    return final_resolution(records).problem == "resolution_not_final"


def is_confirmed_finish(records: list[Record]) -> bool:
    outs = [r.payload for r in records if r.kind == "finish_outcome"]
    return bool(outs) and not outs[-1].get("invalidPayload") and outs[-1].get("outcome") in ("applied", "unchanged")


def is_terminal_resolution(records: list[Record]) -> bool:
    res = [r.payload for r in records if r.kind == "operator_resolution"]
    if not res:
        return False
    last = res[-1]
    return (not last.get("invalidPayload") and last.get("decision") in TERMINAL_RESOLUTIONS
            and isinstance(last.get("reconciliationRef"), str) and bool(last["reconciliationRef"].strip()))


# --- classification (026 §5): operator-only resolutions -------------------------

@dataclass(frozen=True)
class Facts:
    """What the operator can observe now. None means "not read"."""
    reader: str                                  # the supervisor instance asking
    claim_found: bool | None = None              # GET claims/{key}: True found, False 404 (= not observed)
    claim_receipt_matches: bool | None = None    # found receipt == held root/key/actor/attemptId/executorRef
    server_completion_confirmed: bool = False    # CONFIRMED server-side completion of the in-flight request
    finish_proof: str | None = None              # coherent.prove_finish(): proved|superseded|unconfirmed|intervening|proof_missing
    node_done_for_attempt: bool | None = None    # PlanStore: Done for THIS stream's attempt/epoch
    review_class: str | None = None              # derive_review() of the node: GLOBAL PlanStore fact
    acknowledged: bool = False


@dataclass(frozen=True)
class Classification:
    label: str
    unknowns: tuple[str, ...]
    allowed: tuple[str, ...]          # OPERATOR actions only
    automatic: tuple[str, ...] = ()   # always empty: asserted by tests
    planstore_review: str | None = None   # global PlanStore review fact, reported SEPARATELY from this stream's handoff


def classify(prefix: list[Record], f: Facts, bounds: Bounds | None = None) -> Classification:
    b = bounds or Bounds()
    if not prefix:
        return Classification("C0", (), ())
    if any(r.writer != f.reader for r in prefix):
        return Classification("foreign", ("other_writer_state",), ("operator_resolve_foreign_stream",))
    kinds = [r.kind for r in prefix]
    if kinds[-1] == "operator_resolution":
        if is_terminal_resolution(prefix):
            return Classification("resolved", (), ())
        # Permission to act is NOT a confirmed resolution: the underlying state stays as it was.
        trimmed = list(prefix)
        while trimmed and trimmed[-1].kind == "operator_resolution":
            trimmed.pop()
        prior = classify(trimmed, f, b)
        decision = prefix[-1].payload.get("decision", "invalid")
        return Classification(f"{prior.label}+permission:{decision}", prior.unknowns, prior.allowed,
                              planstore_review=prior.planstore_review)
    # A trailing fault record classifies by the step it interrupted.
    core = [k for k in kinds if k not in ("fault", "operator_resolution")] or ["fault"]
    last = core[-1]
    if last == "claim_intent":
        if f.claim_found is True and f.claim_receipt_matches is not True:
            return Classification("C1:foreign_receipt", ("receipt_owner",), ("operator_resolve_foreign_stream",))
        if f.claim_found is True:
            return Classification("C1:committed_replay", ("effects_none_by_this_writer",), ("release", "abandon"))
        if f.claim_found is False and f.server_completion_confirmed:
            return Classification("C1:confirmed_not_committed", (), ("operator_permit_new_claim",))
        return Classification("C1:not_observed", ("claim_committed",),
                              ("await_confirmed_server_completion", "operator_reconcile"))
    if last == "claimed":
        return Classification("C2", ("anything_started",), ("release",))
    if last == "launch_intent":
        return Classification("C3", ("spawned", "effects"), ("inspect_workspace", "operator_reconcile"))
    if last == "launched":
        return Classification("C4", ("exit", "effects", "result"), ("inspect_workspace", "operator_kill_owned_group", "operator_reconcile"))
    if last == "exited":
        return Classification("C5", ("parsed_result",), ("release_after_inspection",))
    if last in ("result_captured", "fault") and "finish_intent" not in kinds:
        return Classification("C6", ("finish_attempted",), ("operator_confirm_preconditions_then_new_finish_intent", "release"))
    if last == "finish_intent":
        p = f.finish_proof
        if p == "proved" and f.node_done_for_attempt is not True:
            return Classification("C7:proof_missing", ("finish_committed",), ("operator_reconcile",), planstore_review=f.review_class)
        if p == "proved":
            return Classification("C7:proved", (), ("record_finish_outcome_applied",))
        if p == "superseded":
            return Classification("C7:superseded", (), ("reconcile_from_planstore",))
        if p == "unconfirmed":
            return Classification("C7:unconfirmed", ("finish_committed",), ("await_confirmed_server_completion", "operator_decision"))
        if p == "intervening":
            return Classification("C7:intervening", ("finish_committed",), ("operator_reconcile",))
        return Classification("C7:proof_missing", ("finish_committed",), ("operator_reconcile",))
    # THIS stream's handoff is confirmed only by its own finish outcome AND PlanStore Done for its
    # attempt. A node that is globally Done (or a review candidate) because of SOMEONE ELSE's finish
    # never confirms this stream, and this stream's candidate artifact is never attached to it.
    # Same validated predicate as resolve(): only a VALID applied/unchanged outcome (a C7 proof is
    # recorded as finish_outcome "applied" before it counts).
    confirmed_done = is_confirmed_finish(prefix) and f.node_done_for_attempt is True
    sends = kinds.count("notify_intent")          # every send intent counts against K, delivered or unknown
    g = f.review_class
    if last == "finish_outcome" or (confirmed_done and "review_requested" not in kinds and last not in ("notify_intent", "notify_outcome")):
        if not confirmed_done:
            return Classification("C8:not_confirmed_done", ("this_stream_done",), ("operator_reconcile",), planstore_review=g)
        if g == "candidate":
            return Classification("C8", ("review_requested",), ("request_review",), planstore_review=g)
        return Classification(f"C8:{g}", (), ("operator_classify",) if g == "operator_classification" else (), planstore_review=g)
    if "review_requested" in kinds and last in ("review_requested", "notify_intent", "notify_outcome",
                                                "review_acknowledged", "review_progress"):
        # Authoritative CURRENT facts first: a request for an attempt that PlanStore no longer shows as
        # this stream's Done candidate is moot (decided, reopened, cancelled, other epoch/artifact).
        if f.node_done_for_attempt is not True:
            return Classification("review:moot", (), (), planstore_review=g)
        if g == "operator_classification":
            return Classification("review:operator_classification", (), ("operator_classify",), planstore_review=g)
        if g != "candidate":
            return Classification("review:moot", (), (), planstore_review=g)
    if last == "notify_intent":
        if sends >= b.notify_max:
            return Classification("C9:bound_reached", ("delivery",), ("operator_queue",), planstore_review=g)
        return Classification("C9", ("delivery",), ("resend_same_dedup_key",), planstore_review=g)
    if last in ("review_requested", "notify_outcome") and not f.acknowledged:
        if sends >= b.notify_max:
            return Classification("C10:bound_reached", ("held_by_anyone",), ("operator_queue",), planstore_review=g)
        return Classification("C10", ("held_by_anyone",), ("escalate_same_lead",), planstore_review=g)
    if last in ("review_acknowledged", "review_progress"):
        return Classification("C11", ("progress",), ("remind_same_lead",))
    return Classification("unclassified", ("state",), ("operator_reconcile",))


# --- review candidates vs accept-eligibility (026 §7), derived from the plan view ----

@dataclass(frozen=True)
class ReviewDerivation:
    review_class: str            # candidate | terminal_rejected | operator_classification | accepted | not_done
    accept_eligible: str         # eligible | ineligible:<why> | unverified:prerequisites | n/a


DECISIONS = ("accepted", "rejected")


def older_attempt_decision(decision: Any, decision_epoch: Any, current_epoch: Any) -> bool:
    """HK-ISSUE-012 (plan 038): True only when a VALID recorded decision provably belongs to an attempt
    epoch STRICTLY OLDER than the current one. PlanStore issues epoch+1 on every start or reopen, never
    resets it, and refuses a decision epoch above the current one (PlanRules.ValidateState), so an older
    epoch is a different attempt whatever its attempt id. The record stays history (plan 012); it is
    neither current nor revived. Anything malformed (missing, bool, non-int, < 1, same or future
    epoch, unknown decision) is False, which keeps the caller's fail-closed classification."""
    def epoch(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and v >= 1
    return decision in DECISIONS and epoch(decision_epoch) and epoch(current_epoch) and decision_epoch < current_epoch


def derive_review(node: dict[str, Any], leaf: dict[str, Any] | None) -> ReviewDerivation:
    """From one plan-view node and its readiness leaf. Stores nothing.

    The public view exposes gates and content/attempt facts but not the CURRENT prerequisite
    digest, so a passing candidate is 'unverified:prerequisites' rather than claimed eligible.
    A `stale` acceptance that provably belongs to an older attempt epoch (plan 038) leaves this
    attempt unreviewed: it is a candidate, never `accepted`; any other `stale` stays operator work."""
    if node.get("work") != "done":
        return ReviewDerivation("not_done", "n/a")
    eff = node.get("effectiveAcceptance")
    if eff == "accepted":
        return ReviewDerivation("accepted", "n/a")
    if eff == "rejected":
        return ReviewDerivation("terminal_rejected", "n/a")
    if eff == "stale":
        acc = node.get("acceptance")
        if not (isinstance(acc, dict) and older_attempt_decision(acc.get("decision"), acc.get("attemptEpoch"), node.get("attemptEpoch"))):
            return ReviewDerivation("operator_classification", "n/a")
    elif eff != "none":
        return ReviewDerivation("not_done", "n/a")
    if leaf is None or leaf.get("gatesHold") is not True:
        return ReviewDerivation("candidate", "ineligible:gates")
    if node.get("attemptContentRevision") is None or node.get("attemptPrereqDigest") is None:
        return ReviewDerivation("candidate", "ineligible:unpinned_attempt")
    if node.get("attemptContentRevision") != node.get("contentRevision"):
        return ReviewDerivation("candidate", "ineligible:content_drift")
    if not node.get("artifactRef"):
        return ReviewDerivation("candidate", "ineligible:no_artifact")
    return ReviewDerivation("candidate", "unverified:prerequisites")


# --- review workflow with a fake clock (026 §7) ---------------------------------

ReviewKey = tuple  # (root, node, attemptId, attemptEpoch, artifactRef)


def evidence_digest(content: bytes) -> str:
    """Progress evidence is compared by CONTENT, never by its reference string."""
    return hashlib.sha256(content).hexdigest()


@dataclass
class ReviewState:
    lead: str
    requested_at: float
    deadline: float
    sends: int = 0
    acknowledged: bool = False
    last_checkpoint: int = 0
    evidence_seen: set[str] = field(default_factory=set)


class ReviewWorkflow:
    """Deadlines on an injected fake clock; 'wake' is an explicit call. No real scheduler or wake.
    A review's identity is (root, node, attemptId, epoch, artifact): a reused attemptId with a new
    epoch or artifact is a DIFFERENT review and is never deduplicated against the old one."""

    def __init__(self, *, ack_window: float, progress_window: float, notify_max: int):
        self.ack_window, self.progress_window, self.notify_max = ack_window, progress_window, notify_max
        self.reviews: dict[ReviewKey, ReviewState] = {}
        self.queue: list[ReviewKey] = []

    def request(self, review: ReviewKey, lead: str, now: float) -> None:
        self.reviews.setdefault(tuple(review), ReviewState(lead, now, now + self.ack_window))

    def acknowledge(self, review: ReviewKey, lead: str, now: float) -> None:
        """Only the responsible lead; a repeated acknowledgment is idempotent (no new deadline)."""
        r = self.reviews[tuple(review)]
        if lead != r.lead:
            raise ValueError("only the responsible lead acknowledges")
        if r.acknowledged:
            return
        r.acknowledged, r.deadline = True, now + self.progress_window

    def progress(self, review: ReviewKey, lead: str, checkpoint_id: int, evidence: str, now: float) -> bool:
        """Only the responsible lead, after acknowledging. Resets the deadline only for a NEW (higher)
        checkpoint id carrying NEW evidence content (`evidence` is evidence_digest(content) or a
        PlanStore-anchored checkpoint id)."""
        r = self.reviews[tuple(review)]
        if lead != r.lead or not r.acknowledged:
            raise ValueError("only the responsible, acknowledged lead records progress")
        advancing = checkpoint_id > r.last_checkpoint and evidence not in r.evidence_seen
        if checkpoint_id > r.last_checkpoint:
            r.last_checkpoint = checkpoint_id
        if advancing:
            r.evidence_seen.add(evidence)
            r.deadline = now + self.progress_window
        return advancing

    @classmethod
    def rehydrate(cls, records: list, *, ack_window: float, progress_window: float, notify_max: int) -> "ReviewWorkflow":
        """Rebuild from a record prefix using the ORIGINAL anchors (each record's `at`), never 'now':
        a restart cannot extend a deadline. Send counts come from notify_intent records."""
        wf = cls(ack_window=ack_window, progress_window=progress_window, notify_max=notify_max)
        for rec in records:
            d = rec.payload
            if "review" not in d:
                continue
            key = tuple(d["review"])
            if rec.kind == "review_requested":
                wf.request(key, d["lead"], rec.at)
            elif rec.kind == "review_acknowledged":
                wf.acknowledge(key, d["lead"], rec.at)
            elif rec.kind == "review_progress":
                wf.progress(key, d["lead"], d["checkpointId"], d["evidence"], rec.at)
            elif rec.kind == "notify_intent" and key in wf.reviews:
                r = wf.reviews[key]
                r.sends += 1
                r.deadline = rec.at + (progress_window if r.acknowledged else ack_window)
        return wf

    def wake(self, now: float, current: Any) -> list:
        """`current(review_key)` returns the AUTHORITATIVE current classification derived from PlanStore
        for exactly that (root, node, attemptId, epoch, artifact): 'candidate', 'operator_classification',
        or anything else (moot). Moot reviews stop; stale ones go to operator classification; only live
        candidates escalate."""
        out = []
        for k in list(self.reviews):
            cls = current(k)
            if cls != "candidate":
                del self.reviews[k]
                if k in self.queue:
                    self.queue.remove(k)           # a stopped review is no longer advertised anywhere
                out.append(("operator_classification" if cls == "operator_classification" else "moot", k, ""))
        return out + self._escalate(now)

    def _escalate(self, now: float) -> list:
        """Called explicitly by the test (fake wake). Returns intended notifications only — to the
        SAME lead while under the bound, else the operator queue. Never reassigns, never decides."""
        out = []
        for k, r in self.reviews.items():
            if now < r.deadline or k in self.queue:
                continue
            if r.sends < self.notify_max:
                r.sends += 1
                out.append(("notify_same_lead", k, r.lead))
                r.deadline = now + (self.progress_window if r.acknowledged else self.ack_window)
            else:
                self.queue.append(k)
                out.append(("operator_queue", k, r.lead))
        return out
