"""E2d selective-context handoff (plan 032 rev 6, sha256 60a3bdfa...; TEST-ONLY, fixture constants only). Pure.

Builds the `handoff-envelope.v0` beside an unchanged, H1-SHAPED task package, binds every delivered
byte in one canonical manifest (`candidateDigest`), applies the per-role proof policy and the single
byte/reference accounting rule, verifies imports, extracts the commit-freshness semantic set, and
decides the review-lead commit (retry check FIRST, then freshness, then the predecessor CAS).

Scope (032 §6): package + review-lead rollover only. No worker R/S, no conversation launch, wake,
ACK or invocation. The task part here is H1-shaped test data, not a ChatAgent H1 rendering. The
import authorization is an explicit two-sided STUB (source read AND destination use); real
authorization belongs to the owning runtime (032 D10). Nothing here is a production contract.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from e1 import acts as A
from e1.evidence import INTENTS, Record

POLICY = "handoff.v0"
ENVELOPE = "handoff-envelope.v0"
REQUIRED_MAX, REQUIRED_REFS = 64 * 1024, 256
OPTIONAL_MAX, OPTIONAL_REFS = 32 * 1024, 64
TOTAL_MAX = 96 * 1024
NOTE_MAX = 1024
EVIDENCE_REQUIRED = A.G                       # evidence-index entries in the required set
HANDOFF_NS = uuid.UUID("5e2d0000-0000-4000-8000-000000000032")


class Refused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(f"{code}: {detail}" if detail is not None else code)
        self.code, self.detail = code, detail


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def nbytes(text: str) -> int:
    return len(text.encode("utf-8"))


# --- identities (pre-assigned at prepare; independent of the digest, so nothing is self-referential) ----

def handoff_ids(root: str, claim_key: str, rid: str, predecessor: str, target: str, prepare_id: str) -> dict[str, str]:
    """handoffId = (WHO hands off to WHOM, under which caller-supplied prepare operation), never the
    content (msg 1256). An exact retry reuses its prepareId (same handoffId: same digest => the
    same candidate, another digest => conflict); a genuinely new prepare (e.g. after stale_candidate)
    uses a new prepareId and gets a new handoffId even for the same predecessor and target."""
    if not isinstance(prepare_id, str) or not A.UUID_RE.fullmatch(prepare_id):
        raise Refused("prepare_id", "prepareId must be a lowercase uuid")
    hid = str(uuid.uuid5(HANDOFF_NS, A.canonical([root, claim_key, rid, predecessor, target, prepare_id])))
    return {"handoffId": hid, "recordId": str(uuid.uuid5(HANDOFF_NS, "record:" + hid)),
            "linkId": str(uuid.uuid5(HANDOFF_NS, "link:" + hid))}


# --- pending uncertain effects (mandatory visibility) --------------------------------------------------

def pending_effects(records: list[Record], corrupt: str | None, outstanding: list[list[str]],
                    unconfirmed: list[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Every pending uncertain effect, LISTED (status may be unknown, never omitted):
    - open intents: an intent record whose reserved terminal outcome has not been recorded
      (cross-checked against the stream row's `outstanding`, read in the same snapshot);
    - unconfirmed appends the caller holds (CommitUnknown) with their confirm() status.
    Raises if the list cannot be built (a corrupt stream hides what may be pending)."""
    if corrupt:
        raise Refused("pending_unlistable", corrupt)
    open_: list[tuple[Record, list[str]]] = []
    for r in records:
        if r.kind in INTENTS:
            open_.append((r, list(INTENTS[r.kind])))
        for o in open_:
            if r.kind in o[1]:
                o[1].remove(r.kind)
                break
    items = [{"kind": "open_intent", "id": f"{r.kind}@{r.seq}", "status": "unknown", "awaiting": rest}
             for r, rest in open_ if rest]
    if sorted(t for _, rest in open_ for t in rest) != sorted(t for o in outstanding for t in o):
        raise Refused("pending_unlistable", "open intents disagree with the stream's outstanding reservations")
    for u in unconfirmed:
        items.append({"kind": "commit_unknown", "id": u["recordId"], "status": u.get("status", "unknown"), "awaiting": [u["kind"]]})
    return items


# --- imports (032 §3; consumer msg 1230) --------------------------------------------------------------------

Authorizer = Callable[[str, str, dict[str, Any]], str | None]     # (principal, "source_read"|"destination_use", target) -> token


def verify_imports(imports: list[dict[str, Any]], source_store: dict[tuple[str, str], dict[str, Any]], principal: str,
                   authorize: Authorizer, destination_conversation: str) -> list[dict[str, Any]]:
    """Each import keeps its ORIGINAL ref {conversationId, eventId, messageId, contentHash} and
    provenance, gains a destination mapping, and is verified: the source must resolve in ITS OWN
    conversation (no repointing), the bytes must hash to contentHash, the same ref may not appear
    twice, and the principal must be authorized for BOTH the source read and the destination use."""
    seen: set[tuple[str, str]] = set()
    out = []
    for i, imp in enumerate(imports):
        ref = imp["ref"]
        k = (ref["conversationId"], ref["eventId"])
        if k in seen:
            raise Refused("import_ambiguous", k)
        seen.add(k)
        if ref["conversationId"] == destination_conversation:
            raise Refused("import_rewritten", "a ref was repointed to the destination conversation")
        src = source_store.get(k)
        if src is None or src["messageId"] != ref["messageId"] or src["contentHash"] != ref["contentHash"]:
            raise Refused("import_rewritten", "the ref does not resolve unchanged in its own conversation")
        if sha(imp["text"]) != ref["contentHash"] or imp["text"] != src["text"]:
            raise Refused("import_hash_mismatch", k)
        read_tok = authorize(principal, "source_read", ref)
        use_tok = authorize(principal, "destination_use", {"conversationId": destination_conversation, "ref": ref})
        if not read_tok or not use_tok:
            raise Refused("import_unauthorized", {"sourceRead": bool(read_tok), "destinationUse": bool(use_tok)})
        out.append({"ref": dict(ref), "provenance": src["provenance"], "text": imp["text"],
                    "destination": {"conversationId": destination_conversation, "importId": f"import-{i}"},
                    "auth": {"sourceRead": read_tok, "destinationUse": use_tok}})
    return out


# --- the package -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Package:
    envelope: dict[str, Any]
    manifest: dict[str, Any]
    candidate_digest: str
    bytes_required: int
    bytes_total: int
    refs_required: int
    refs_optional: int


def _mandatory(role: str, cb: dict[str, Any], transition: dict[str, Any], task: dict[str, Any], planstore_class: str) -> dict[str, Any]:
    """Mandatory proof at PREPARE (032 §3 role policy). Anything unproved refuses the package.
    Liveness comes from the PlanStore class read in the SAME snapshot (`planstore_class`:
    A.review_state for a lead, "live" for a worker), never from a C-B presentation status, whose
    proof legitimately depends on ACK/progress and may be unknown (it is a diagnostic; msg 1256)."""
    if cb.get("current") in (None, "unknown") or cb.get("identity") is None:
        raise Refused("mandatory_unproved", "no combined-snapshot current section")
    cur = cb["current"]
    for t in ("text",):
        if not isinstance(task.get(t), str) or not task[t]:
            raise Refused("mandatory_unproved", "task text")
    if set(task.get("instructions", {})) != {"system", "fast", "deep"}:
        raise Refused("mandatory_unproved", "task instructions")
    if role == "lead":
        rk = cb["identity"].get("reviewKey")
        if rk is None or rk.get("leadSession") != transition["fromSession"]:
            raise Refused("mandatory_unproved", "predecessor binding is not current")
        if planstore_class != "candidate":
            raise Refused("mandatory_unproved", f"review is not a live candidate ({planstore_class})")
        return {"identity": rk, "planstoreClass": planstore_class}
    if role == "worker":
        ek = cb["identity"].get("executionKey")
        if ek is None or planstore_class != "live":
            raise Refused("mandatory_unproved", f"attempt is not current ({planstore_class})")
        return {"identity": ek, "planstoreClass": planstore_class}
    raise Refused("role", role)


def build(*, role: str, cb: dict[str, Any], planstore_class: str, pins: dict[str, Any], basis_check: list[dict[str, Any]],
          pending: list[dict[str, Any]], evidence: list[dict[str, Any]], task: dict[str, Any], transition: dict[str, Any],
          note: dict[str, Any] | None = None, imports: list[dict[str, Any]] = ()) -> Package:
    """Build one immutable candidate. `basis_check` lists the basis of every part that came from a
    read; they must all be the SAME snapshot (mixing reads is refused). `imports` are already
    verified (verify_imports). Raises Refused on any mandatory failure or required overflow."""
    if any(b != cb["basis"] for b in basis_check):
        raise Refused("mixed_snapshot", "every read part must come from the one C-B snapshot")
    mandatory = dict(_mandatory(role, cb, transition, task, planstore_class), pins=pins)
    stale = pins_problem(pins, task["packageRef"])
    if stale:
        raise Refused("stale_content", stale)                     # Task pins must be current at prepare too
    for p in pending:
        if not {"kind", "id", "status"} <= set(p):
            raise Refused("pending_unlistable", p)
    cur = cb["current"]
    phase = cur["deadlinePhase"]
    ordered_ev = sorted(evidence, key=lambda e: -e["seq"])
    req_ev, extra_ev = ordered_ev[:EVIDENCE_REQUIRED], ordered_ev[EVIDENCE_REQUIRED:]
    if note is not None and nbytes(note["text"]) > NOTE_MAX:
        raise Refused("note_too_large", nbytes(note["text"]))
    manifest: dict[str, Any] = {
        "policy": {"version": POLICY, "role": role, "caps": {"required": [REQUIRED_MAX, REQUIRED_REFS],
                                                             "optional": [OPTIONAL_MAX, OPTIONAL_REFS], "total": TOTAL_MAX}},
        "transition": dict(transition, status="candidate"),
        "task": {"packageRef": task["packageRef"], "suppliedSha256": sha(task["text"]),
                 "instructions": {k: sha(v) for k, v in sorted(task["instructions"].items())}},
        "authority": {"pending": pending, "queue": list(cb.get("queue", []))},
        "state": {"mandatory": mandatory, "basis": cb["basis"]},
        "obligations": {"phase": phase, "queue": list(cb.get("queue", []))},
        "evidenceIndex": req_ev,
        "optional": {"diagnostics": None, "evidence": [], "imports": [], "note": None},
        "selection": {"order": ["diagnostics", "evidence", "imports", "note"], "included": 0, "omitted": 0, "cursor": None},
    }
    payload: dict[str, Any] = {"imports": [], "note": None}
    task_bytes = nbytes(task["text"]) + sum(nbytes(v) for v in task["instructions"].values())

    def size() -> tuple[int, int, int]:
        env = {"version": ENVELOPE, "manifest": manifest, "payload": payload}
        b = task_bytes + nbytes(A.canonical(env))
        refs_req = len(manifest["evidenceIndex"]) + len(pending) + len(manifest["authority"]["queue"])
        refs_opt = len(manifest["optional"]["evidence"]) + len(manifest["optional"]["imports"])
        return b, refs_req, refs_opt

    b_req, refs_req, _ = size()
    if b_req > REQUIRED_MAX or refs_req > REQUIRED_REFS:
        raise Refused("handoff_overflow", {"bytes": b_req, "refs": refs_req})
    diagnostics = {k: cur[k] for k in ("ack", "progress", "deadlinePhase", "review", "status") if k in cur}
    diagnostics["counters"] = cb.get("counters", {})
    diagnostics["asOf"] = "prepare"
    items: list[tuple[str, Any]] = [("diagnostics", diagnostics)] + [("evidence", e) for e in extra_ev] + \
        [("imports", i) for i in imports] + ([("note", note)] if note is not None else [])
    for idx, (kind, item) in enumerate(items):
        snapshot = (json.dumps(manifest), json.dumps(payload))
        if kind == "diagnostics":
            manifest["optional"]["diagnostics"] = item
        elif kind == "evidence":
            manifest["optional"]["evidence"].append(item)
        elif kind == "imports":
            manifest["optional"]["imports"].append({k: item[k] for k in ("ref", "provenance", "destination", "auth")}
                                                   | {"textSha256": sha(item["text"])})
            payload["imports"].append(item["text"])
        else:
            manifest["optional"]["note"] = {"sha256": sha(item["text"]), "bytes": nbytes(item["text"]), "author": item["author"]}
            payload["note"] = item["text"]
        manifest["selection"]["included"] = idx + 1
        b, _, r_opt = size()
        if b - b_req > OPTIONAL_MAX or r_opt > OPTIONAL_REFS or b > TOTAL_MAX:
            m, pl = snapshot                                        # prefix rule: this and everything after is omitted
            manifest.clear(); manifest.update(json.loads(m))
            payload.clear(); payload.update(json.loads(pl))
            manifest["selection"].update(included=idx, omitted=len(items) - idx, cursor=idx)
            break
    # The final `selection` values are themselves delivered bytes: re-measure BOTH partitions on the
    # final package (msg 1256). The required partition is the package with every optional item
    # removed but the final selection kept; drop trailing optional items until both caps hold.
    def required_size() -> tuple[int, int]:
        m2 = json.loads(json.dumps(manifest))
        m2["optional"] = {"diagnostics": None, "evidence": [], "imports": [], "note": None}
        env2 = {"version": ENVELOPE, "manifest": m2, "payload": {"imports": [], "note": None}}
        return task_bytes + nbytes(A.canonical(env2)), refs_req
    while True:
        b, refs_req, refs_opt = size()
        b_req, _ = required_size()
        if b_req > REQUIRED_MAX or refs_req > REQUIRED_REFS:
            raise Refused("handoff_overflow", {"bytes": b_req, "refs": refs_req})
        if b - b_req <= OPTIONAL_MAX and refs_opt <= OPTIONAL_REFS and b <= TOTAL_MAX:
            break
        n = manifest["selection"]["included"]
        if n == 0:
            raise Refused("handoff_overflow", {"bytes": b})
        kind, _ = items[n - 1]
        if kind == "diagnostics":
            manifest["optional"]["diagnostics"] = None
        elif kind == "evidence":
            manifest["optional"]["evidence"].pop()
        elif kind == "imports":
            manifest["optional"]["imports"].pop()
            payload["imports"].pop()
        else:
            manifest["optional"]["note"], payload["note"] = None, None
        manifest["selection"].update(included=n - 1, omitted=len(items) - (n - 1), cursor=n - 1)
    env = {"version": ENVELOPE, "manifest": manifest, "payload": payload}
    return Package(env, manifest, sha(A.canonical(manifest)), b_req, b, refs_req, refs_opt)


# --- commit-freshness semantic set (032 §3a) -----------------------------------------------------------

def pins_of(facts: A.PlanFacts) -> dict[str, Any]:
    """The CURRENT PlanStore content and dependency facts the task package depends on (msg 1260):
    the node's content revision, the attempt pins, the artifact, and the dependency-edge digest read
    in the same transaction. Basis/snapshot/transaction ids are deliberately excluded."""
    n = facts.node or {}
    return {"contentRevision": n.get("content_revision"), "attemptContentRevision": n.get("attempt_content_revision"),
            "attemptPrereqDigest": n.get("attempt_prereq_digest"), "artifactRef": n.get("artifact_ref"),
            "dependencies": facts.basis.get("dependencies")}


def pins_problem(pins: dict[str, Any], package_ref: str) -> str | None:
    """PlanRules.PinFailure as far as one node row can tell: an unpinned attempt, content revised
    since the attempt was pinned, or a supplied package whose pins are not the attempt's."""
    if pins.get("attemptContentRevision") is None or pins.get("attemptPrereqDigest") is None:
        return "unpinned_attempt"
    if pins.get("contentRevision") != pins.get("attemptContentRevision"):
        return "content_revised"
    pkg = json.loads(package_ref)
    if pkg.get("kind") == "supplied.v1" and (pkg["contentRevision"], pkg["prereqDigest"]) != (
            pins["attemptContentRevision"], pins["attemptPrereqDigest"]):
        return "package_pins"
    return None


def semantic_set(role: str, cb: dict[str, Any], pending: list[dict[str, Any]], package_ref: str, predecessor: str,
                 planstore_class: str, pins: dict[str, Any]) -> dict[str, Any]:
    """What must be EQUAL between prepare and commit, built from the facts of EACH read (the commit
    side from the route A transaction). Snapshot bases and transaction ids are never part of it;
    diagnostic fields (ACK, progress, phase, counters) are excluded by policy."""
    cur = cb.get("current")
    if cur in (None, "unknown"):
        return {"unprovable": True}
    key = cb["identity"].get("reviewKey") or cb["identity"].get("executionKey")
    return {"identity": {k: key[k] for k in A.ATTEMPT_FIELDS} | ({"artifactRef": key["artifactRef"]} if "artifactRef" in key else {}),
            "class": planstore_class, "packageRef": package_ref, "pins": pins, "predecessor": predecessor,
            "pending": sorted([p["kind"], p["id"], p["status"]] for p in pending), "queue": sorted(cb.get("queue", []))}


def semantic_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """The prepare-side semantic set, reconstructed from the IMMUTABLE stored manifest only."""
    st, auth, t = manifest["state"]["mandatory"], manifest["authority"], manifest["transition"]
    key = st["identity"]
    return {"identity": {k: key[k] for k in A.ATTEMPT_FIELDS} | ({"artifactRef": key["artifactRef"]} if "artifactRef" in key else {}),
            "class": st["planstoreClass"], "packageRef": manifest["task"]["packageRef"], "pins": st["pins"],
            "predecessor": t["predecessorBindingId"], "pending": sorted([p["kind"], p["id"], p["status"]] for p in auth["pending"]),
            "queue": sorted(auth["queue"])}


def verify_stored(row: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """An immutable candidate as stored: the digest equals sha(JCS(manifest)), the envelope carries
    exactly that manifest, and the retained Task bytes hash to the manifest's task digests. Returns
    (manifest, transition, task). Anything else is `tampered_candidate`."""
    if row is None:
        raise Refused("candidate_unavailable")
    manifest = json.loads(row["manifest"])
    envelope = json.loads(row["envelope"])
    task = json.loads(row["task"])
    if sha(A.canonical(manifest)) != row["candidate_digest"]:
        raise Refused("tampered_candidate", "digest")
    # The envelope must be EXACTLY the canonical envelope of this manifest and its bound payload: no
    # other version, no extra keys, no unbound bytes anywhere (msg 1266).
    pl = envelope.get("payload") if isinstance(envelope, dict) else None
    if (not isinstance(pl, dict) or set(pl) != {"imports", "note"} or not isinstance(pl["imports"], list)
            or row["envelope"] != A.canonical({"version": ENVELOPE, "manifest": manifest, "payload": {"imports": pl["imports"], "note": pl["note"]}})):
        raise Refused("tampered_candidate", "envelope is not the canonical envelope of this manifest")
    # The retained Task bytes are closed too (msg 1273): exactly {text, instructions{system, fast,
    # deep}, packageRef}, all strings, stored as their canonical JCS bytes. No other field.
    if (not isinstance(task, dict) or set(task) != {"text", "instructions", "packageRef"}
            or not isinstance(task["instructions"], dict) or set(task["instructions"]) != {"system", "fast", "deep"}
            or not all(isinstance(v, str) for v in [task["text"], task["packageRef"], *task["instructions"].values()])
            or row["task"] != A.canonical(task)):
        raise Refused("tampered_candidate", "task is not exactly the bound Task fields")
    mt = manifest["task"]
    if sha(task["text"]) != mt["suppliedSha256"] or {k: sha(v) for k, v in sorted(task["instructions"].items())} != mt["instructions"] \
            or task["packageRef"] != mt["packageRef"]:
        raise Refused("tampered_candidate", "task bytes")
    if [sha(x) for x in pl["imports"]] != [i["textSha256"] for i in manifest["optional"]["imports"]]:
        raise Refused("tampered_candidate", "import bytes")
    note = manifest["optional"]["note"]
    if (note is None) != (pl["note"] is None) or (note is not None and sha(pl["note"]) != note["sha256"]):
        raise Refused("tampered_candidate", "note bytes")
    transition = {k: v for k, v in manifest["transition"].items() if k != "status"}
    return manifest, transition, task


# --- the review-lead commit decision (runs INSIDE the route A transaction) -------------------------------

def commit_payload(rid: str, transition: dict[str, Any], lead: str, candidate_digest: str) -> dict[str, Any]:
    return {"reviewId": rid, "predecessorBindingId": transition["predecessorBindingId"], "linkId": transition["linkId"],
            "lead": lead, "leadSession": transition["targetSession"], "gate": transition["gate"], "gateRef": transition["gateRef"],
            "handoffId": transition["handoffId"], "candidateDigest": candidate_digest}


def decide_commit(view: A.View, rid: str, transition: dict[str, Any], candidate_digest: str,
                  existing: dict[str, Any] | None, fresh_now: dict[str, Any], fresh_prepare: dict[str, Any]) -> A.Decision:
    """032 §3a order: (a) retry check FIRST on the pre-assigned record id (`existing` = that row,
    read in this transaction): the exact stream, kind and payload => the committed record; anything
    else => conflict; (b) freshness: the semantic set re-read under the lock must equal prepare's;
    (c) predecessor CAS through the binding chain; (d) one review_rebind record."""
    chain = view.chain(rid)
    lead = chain.current.lead if chain.status == "ok" else None
    want = commit_payload(rid, transition, lead or "", candidate_digest)
    if existing is not None:
        same = (existing["kind"] == "review_rebind" and existing["root"] == transition["root"]
                and existing["claim_key"] == transition["claimKey"] and json.loads(existing["data"]) == want)
        if same:
            return A.Decision("committed", reason="exact retry", stream_key=rid)
        return A.Decision("conflict", counter="conflict", queue="conflict", reason="handoff identity reused", stream_key=rid)
    for r in view.links.get(rid, []):
        if r.payload.get("handoffId") == transition["handoffId"]:
            return A.Decision("conflict", counter="conflict", queue="conflict", reason="handoffId under another record", stream_key=rid)
    if fresh_now != fresh_prepare:
        diff = sorted(k for k in set(fresh_now) | set(fresh_prepare) if fresh_now.get(k) != fresh_prepare.get(k))
        return A.Decision("stale_candidate", reason=",".join(diff), stream_key=rid)
    if chain.status != "ok":
        return A.Decision(chain.status, queue=chain.status if chain.status != "unknown_key" else None, stream_key=rid)
    cur = chain.current
    if cur.link_id != transition["predecessorBindingId"] or cur.session != transition["fromSession"]:
        return A.Decision("stale_binding", reason="predecessor is not the current binding", stream_key=rid)
    m = A.SESSION.fullmatch(transition["targetSession"])
    if m is None or m.group(1) != cur.lead:
        return A.Decision("refused", reason="target must be the lead's hkw1 session", stream_key=rid)
    gate_ok = (transition["gate"] == "release" and transition["gateRef"] == cur.session) or \
              (transition["gate"] == "operator" and transition["gateRef"].strip())
    if cur.session is None or not gate_ok:
        return A.Decision("refused", reason="needs a bound predecessor and its release or an operator act", stream_key=rid)
    return A.Decision("accepted", append=("review_rebind", commit_payload(rid, transition, cur.lead, candidate_digest)), stream_key=rid)


# --- receipt ---------------------------------------------------------------------------------------------

def receipt_status(view: A.View, rid: str, receipt: dict[str, Any], row: dict[str, Any] | None) -> str:
    """current | superseded | mismatch, from ONE snapshot: the record exists under the pre-assigned
    id with exactly the receipt's handoffId/digest/link/seq/hash, and its link is (still) current."""
    if row is None:
        return "mismatch"
    d = json.loads(row["data"])
    if (row["kind"], row["seq"], row["record_hash"], d.get("handoffId"), d.get("candidateDigest"), d.get("linkId")) != (
            "review_rebind", receipt["seq"], receipt["recordHash"], receipt["handoffId"], receipt["candidateDigest"], receipt["bindingLinkId"]):
        return "mismatch"
    chain = view.chain(rid)
    if chain.status != "ok":
        return "mismatch"
    if chain.current.link_id == receipt["bindingLinkId"]:
        return "current"
    return "superseded" if any(b.link_id == receipt["bindingLinkId"] for b in chain.history) else "mismatch"
