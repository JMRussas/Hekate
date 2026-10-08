"""E2e offline handoff consumer (plan 034 rev 3, sha256 17273a51...; TEST-ONLY, fixture constants). Pure.

Consumes the EXACT accepted E2d v0 bytes through a new `handoff-delivery.v0` wrapper, builds a
separately identified CONSUMER VIEW (the committed candidate is never altered), and composes it with
an H1 builder (the pinned real H1 in the opt-in suite; an H1-shaped stub in the default suite) WITHOUT
changing H1: H1 stays one message and is only called with a reduced `windowTokens`.

Nothing here invokes a model, launches or wakes anything, writes anywhere, or is production auth:
the import/retrieval policy is an explicit default-deny STUB. Digests are only ever computed over
received bytes; the Python canonical-SHAPE comparison (handoff.verify_stored) is part of verification.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable

from e1 import acts as A
from e1 import handoff as H

WRAPPER, CODEC, VIEW_VERSION = "handoff-delivery.v0", "py-canon.v0", "consumer-view.v0"
POLICY_VERSION = "import-policy-stub.v0"
ESTIMATOR_ID = "chatagent-utf8-conservative-v1@5255daa"
REQUEST_OVERHEAD_TOKENS, MESSAGE_OVERHEAD_TOKENS = 32, 16          # pinned contextBuilder.ts 36-37
# Ingress caps on RAW bytes, before any parsing (034 §2). Transport carries the manifest twice and
# escaped JSON, so these are larger than and separate from 032's delivered-content caps.
MANIFEST_MAX = ENVELOPE_MAX = TASK_MAX = 1 << 20
RECEIPT_MAX = 4096
H1_RESPONSE_MAX, H1_RULES_MAX, H1_RULE_BYTES_MAX, H1_INSTRUCTION_MAX = 1 << 20, 32, 256 * 1024, 64 * 1024
WRAPPER_MAX = 4_718_592                                              # 4.5 MiB, checked first
# Bounded retrieval (034 §5, C5) and the uncertainty list after the final re-read (§6).
RETRIEVAL_CALLS, RETRIEVAL_ITEMS, RETRIEVAL_BYTES = 16, 64, 32 * 1024
UNCERTAINTY_REFS_MAX = 256
RESERVATION_WIDTH = 10


class Refused(Exception):
    def __init__(self, code: str, detail: Any = None):
        super().__init__(f"{code}: {detail}" if detail is not None else code)
        self.code, self.detail = code, detail


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --- strict decoding (typed refusal BEFORE any effect) ---------------------------------------------------

def _no_dupes(pairs):
    seen: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise Refused("strict_json", f"duplicate key {k!r}")
        seen[k] = v
    return seen


def _finite(tok: str) -> float:
    v = float(tok)
    if not math.isfinite(v):
        raise Refused("strict_json", f"non-finite number {tok}")
    return v


def _reject_constant(tok: str):
    raise Refused("strict_json", f"non-JSON constant {tok}")


def _scalars_ok(v: Any) -> None:
    if isinstance(v, str):
        if any(0xD800 <= ord(c) <= 0xDFFF for c in v):
            raise Refused("strict_json", "lone surrogate")
    elif isinstance(v, dict):
        for k, x in v.items():
            _scalars_ok(k)
            _scalars_ok(x)
    elif isinstance(v, list):
        for x in v:
            _scalars_ok(x)


def strict_loads(raw: bytes, what: str) -> Any:
    """py-canon.v0 reader: valid UTF-8, no duplicate keys, no NaN/Infinity (incl. overflowing
    literals like 1e999), no lone surrogates (also when written as a \\ud800 escape)."""
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as e:
        raise Refused("strict_json", f"{what}: invalid UTF-8") from e
    try:
        doc = json.loads(text, object_pairs_hook=_no_dupes, parse_float=_finite, parse_constant=_reject_constant)
    except Refused as e:
        raise Refused(e.code, f"{what}: {e.detail}") from None
    except ValueError as e:
        raise Refused("strict_json", f"{what}: {e}") from None
    except RecursionError:              # HK-ISSUE-001: excessive nesting is a typed refusal, never an escape
        raise Refused("strict_json", f"{what}: nesting too deep") from None
    try:
        _scalars_ok(doc)
    except RecursionError:              # the scan recurses too; a document that just fit the parser can still overflow it
        raise Refused("strict_json", f"{what}: nesting too deep") from None
    return doc


# --- the delivery wrapper (034 §2) ---------------------------------------------------------------------

@dataclass(frozen=True)
class Delivery:
    """In-process offline transport: every payload field is the exact stored/received BYTES."""
    wrapper: str
    codec: str
    candidate_digest: str
    manifest: bytes
    envelope: bytes
    task: bytes
    receipt: bytes
    h1_input: bytes


@dataclass(frozen=True)
class Verified:
    manifest: dict[str, Any]
    envelope: dict[str, Any]
    task: dict[str, Any]
    receipt: dict[str, Any]
    h1_input: dict[str, Any]


def _ingress(d: Delivery) -> None:
    total = sum(len(x) for x in (d.manifest, d.envelope, d.task, d.receipt, d.h1_input)) + \
        len(d.wrapper.encode()) + len(d.codec.encode()) + len(d.candidate_digest.encode())
    if total > WRAPPER_MAX:
        raise Refused("ingress_too_large", {"wrapper": total})
    for name, b, cap in (("manifest", d.manifest, MANIFEST_MAX), ("envelope", d.envelope, ENVELOPE_MAX), ("task", d.task, TASK_MAX),
                         ("receipt", d.receipt, RECEIPT_MAX), ("h1Input", d.h1_input, H1_RESPONSE_MAX + H1_RULE_BYTES_MAX + 4 * H1_INSTRUCTION_MAX)):
        if len(b) > cap:
            raise Refused("ingress_too_large", {name: len(b)})


HEX64 = A.HEX64
BUDGET_KEYS = ("windowTokens", "maxHistoryTurns", "safetyTokens", "fastOutputTokens", "deepOutputTokens")


def _closed_receipt(r: Any) -> None:
    """Closed, typed receipt schema (msg 1312): exactly these fields, these types."""
    if (not isinstance(r, dict) or set(r) != {"handoffId", "candidateDigest", "bindingLinkId", "recordId", "seq", "recordHash"}
            or not all(isinstance(r[k], str) and A.UUID_RE.fullmatch(r[k]) for k in ("handoffId", "bindingLinkId", "recordId"))
            or not all(isinstance(r[k], str) and HEX64.fullmatch(r[k]) for k in ("candidateDigest", "recordHash"))
            or isinstance(r["seq"], bool) or not isinstance(r["seq"], int) or r["seq"] < 1):
        raise Refused("receipt_shape")


def _h1_input_caps(h: Any) -> None:
    allowed = {"response", "rules", "systemInstruction", "roleInstructions", "budget", "capturedAtIso", "limits"}
    if (not isinstance(h, dict) or not {"response", "rules", "systemInstruction", "roleInstructions", "budget", "capturedAtIso"} <= set(h)
            or set(h) - allowed):
        raise Refused("h1_input", "H1 options are not the closed option set")
    b = h["budget"]
    if (not isinstance(b, dict) or set(b) != set(BUDGET_KEYS)
            or any(isinstance(b[k], bool) or not isinstance(b[k], int) or not 0 <= b[k] <= A.INT_MAX for k in BUDGET_KEYS)):
        raise Refused("h1_input", "budget")
    if not isinstance(h["capturedAtIso"], str) or ("limits" in h and not isinstance(h["limits"], dict)):
        raise Refused("h1_input", "capturedAtIso/limits")
    if not isinstance(h["rules"], list) or not all(isinstance(r, dict) and all(isinstance(x, str) for x in r.values()) for r in h["rules"]):
        raise Refused("h1_input", "rules")
    if not isinstance(h["response"], str) or len(h["response"].encode()) > H1_RESPONSE_MAX:
        raise Refused("ingress_too_large", "h1Input.response")
    rules = h["rules"]
    if not isinstance(rules, list) or len(rules) > H1_RULES_MAX or len(A.canonical(rules).encode()) > H1_RULE_BYTES_MAX:
        raise Refused("ingress_too_large", "h1Input.rules")
    ri = h["roleInstructions"]
    if not isinstance(ri, dict) or set(ri) != {"fast", "deep"}:
        raise Refused("h1_input", "roleInstructions")
    for t in (h["systemInstruction"], ri["fast"], ri["deep"]):
        if not isinstance(t, str) or len(t.encode()) > H1_INSTRUCTION_MAX:
            raise Refused("ingress_too_large", "h1Input instruction")


def delivered_sizes(manifest: dict[str, Any], envelope: dict[str, Any], task: dict[str, Any]) -> tuple[int, int, int]:
    """032 delivered-content accounting on DECODED content: (required bytes, total bytes, required refs)."""
    tb = H.nbytes(task["text"]) + sum(H.nbytes(v) for v in task["instructions"].values())
    total = tb + H.nbytes(A.canonical(envelope))
    m2 = json.loads(json.dumps(manifest))
    m2["optional"] = {"diagnostics": None, "evidence": [], "imports": [], "note": None}
    req = tb + H.nbytes(A.canonical({"version": H.ENVELOPE, "manifest": m2, "payload": {"imports": [], "note": None}}))
    refs = len(manifest["evidenceIndex"]) + len(manifest["authority"]["pending"]) + len(manifest["authority"]["queue"])
    refs_opt = len(manifest["optional"]["evidence"]) + len(manifest["optional"]["imports"])
    return req, total, refs, refs_opt


def verify_delivery(d: Delivery) -> Verified:
    """Ingress caps (unread) -> wrapper/codec -> digest over the EXACT bytes -> strict decoding ->
    E2d canonical-shape verification -> receipt/manifest identity -> delivered-content caps.
    ANY failure refuses the whole delivery."""
    _ingress(d)
    if d.wrapper != WRAPPER or d.codec != CODEC:
        raise Refused("codec_unsupported", {"wrapper": d.wrapper, "codec": d.codec})
    receipt = strict_loads(d.receipt, "receipt")
    _closed_receipt(receipt)
    if not (sha(d.manifest) == d.candidate_digest == receipt["candidateDigest"]):
        raise Refused("digest_mismatch")
    manifest = strict_loads(d.manifest, "manifest")
    strict_loads(d.envelope, "envelope")
    strict_loads(d.task, "task")
    h1_input = strict_loads(d.h1_input, "h1Input")
    _h1_input_caps(h1_input)
    row = {"candidate_digest": d.candidate_digest, "manifest": d.manifest.decode("utf-8"),
           "envelope": d.envelope.decode("utf-8"), "task": d.task.decode("utf-8")}
    if not isinstance(manifest, dict):
        raise Refused("delivery_mismatch", "manifest shape")
    try:
        m2, t, task = H.verify_stored(row)
        req, total, refs, refs_opt = delivered_sizes(m2, json.loads(row["envelope"]), task)
        _ = (m2["transition"]["handoffId"], m2["transition"]["recordId"], m2["transition"]["linkId"], m2["state"]["mandatory"]["identity"],
             m2["authority"]["pending"], m2["authority"]["queue"], m2["evidenceIndex"], m2["optional"]["imports"], m2["optional"]["note"])
        review_identity(m2)             # HK-ISSUE-002: every identity field present, or a typed delivery_mismatch
    except H.Refused as e:
        raise Refused("delivery_mismatch", e.detail) from None
    except (KeyError, TypeError, AttributeError, IndexError, ValueError):
        raise Refused("delivery_mismatch", "manifest/envelope/task shape") from None
    if m2 != manifest:
        raise Refused("delivery_mismatch", "manifest")
    if (receipt.get("handoffId"), receipt.get("recordId"), receipt.get("bindingLinkId")) != (t["handoffId"], t["recordId"], t["linkId"]):
        raise Refused("receipt_mismatch")
    envelope = json.loads(row["envelope"])
    if (req > H.REQUIRED_MAX or refs > H.REQUIRED_REFS or total > H.TOTAL_MAX or total - req > H.OPTIONAL_MAX
            or refs_opt > H.OPTIONAL_REFS):
        raise Refused("delivery_overflow", {"required": req, "total": total, "refs": refs, "optionalRefs": refs_opt})
    return Verified(manifest, envelope, task, receipt, h1_input)


# --- policy stub (034 §5): default deny, two questions, recorded tokens --------------------------------

@dataclass(frozen=True)
class PolicyStub:
    """rules: ({"id", "action": "source_read"|"destination_use", "match": {...}, "allow": bool}, ...).
    A rule matches when every key in `match` equals the target's value. First match wins; no match = deny."""
    principal: str
    rules: tuple[dict[str, Any], ...] = ()

    def digest(self) -> str:
        return H.sha(A.canonical({"version": POLICY_VERSION, "principal": self.principal, "rules": list(self.rules)}))

    def decide(self, action: str, target: dict[str, Any]) -> dict[str, Any]:
        for r in self.rules:
            if r["action"] == action and all(target.get(k) == v for k, v in r["match"].items()):
                token = H.sha(A.canonical([POLICY_VERSION, self.principal, action, target, r["id"]]))[:32] if r["allow"] else None
                return {"action": action, "rule": r["id"], "allow": bool(r["allow"]), "token": token}
        return {"action": action, "rule": None, "allow": False, "token": None}


# --- revalidation facts (034 §6; read by the caller in ONE combined snapshot) ----------------------------

@dataclass(frozen=True)
class Fresh:
    candidate_digest: str               # the identity this proof was read FOR (bound, msg 1311)
    record_id: str
    review_identity: dict[str, Any]     # AttemptKey + artifactRef of the exact review
    package_ref: str
    receipt_status: str                 # current | superseded | mismatch (E2d receipt_status, same snapshot)
    current_binding: str | None
    review_class: str
    pins_problem: str | None
    pending: list[dict[str, Any]]
    queue: list[str]
    basis: dict[str, Any] = field(default_factory=dict)


FRESH_FIELDS = ("candidate_digest", "record_id", "review_identity", "package_ref", "receipt_status", "current_binding",
                "review_class", "pins_problem", "pending", "queue", "basis")


def review_identity(manifest: dict[str, Any]) -> dict[str, Any]:
    """The AttemptKey + artifactRef of the manifest. A missing field is a typed delivery_mismatch (HK-ISSUE-002)."""
    try:
        key = manifest["state"]["mandatory"]["identity"]
        return {k: key[k] for k in (*A.ATTEMPT_FIELDS, "artifactRef")}
    except (KeyError, TypeError):
        raise Refused("delivery_mismatch", "manifest identity") from None


def revalidate(v: Verified, fresh: Fresh) -> dict[str, Any]:
    # HK-ISSUE-002: a malformed proof or a Verified without its bound fields is a TYPED refusal, not an escape.
    missing = [f for f in FRESH_FIELDS if not hasattr(fresh, f)]
    if missing:
        raise Refused("fresh_mismatch", f"the revalidation proof is malformed: missing {missing}")
    if not isinstance(fresh.pending, list) or not isinstance(fresh.queue, list):
        raise Refused("fresh_mismatch", "the revalidation proof is malformed: pending/queue")
    try:
        t = v.manifest["transition"]
        bound = (v.receipt["candidateDigest"], v.receipt["recordId"], review_identity(v.manifest), v.task["packageRef"], t["linkId"])
    except (KeyError, TypeError):
        raise Refused("delivery_mismatch", "verified delivery fields") from None
    if (fresh.candidate_digest, fresh.record_id, fresh.review_identity, fresh.package_ref) != bound[:4]:
        raise Refused("fresh_mismatch", "the revalidation proof is not about this exact candidate/key/package")
    if fresh.receipt_status != "current":
        raise Refused("receipt_not_current", fresh.receipt_status)
    if fresh.current_binding != bound[4]:
        raise Refused("binding_moved")
    if fresh.review_class != "candidate":
        raise Refused("review_not_candidate", fresh.review_class)
    if fresh.pins_problem:
        raise Refused("stale_content", fresh.pins_problem)
    if len(fresh.pending) + len(fresh.queue) > UNCERTAINTY_REFS_MAX:
        raise Refused("uncertainty_overflow", len(fresh.pending) + len(fresh.queue))
    return {"asOf": "revalidation", "basis": fresh.basis, "pending": fresh.pending, "queue": fresh.queue}


# --- optional items: imports, note, retrieval ------------------------------------------------------------

Retriever = Callable[[dict[str, Any]], dict[str, Any] | None]    # FULL manifest pointer -> {"pointer", "bytes", "basis"} | None
POINTER_KEYS = {"seq", "kind", "checkpointId", "evidenceDigest", "linkId"}


def _imports(v: Verified, policy: PolicyStub, destination: str) -> list[dict[str, Any]]:
    out = []
    payload = v.envelope["payload"]["imports"]
    for i, imp in enumerate(v.manifest["optional"]["imports"]):
        ref, text = imp["ref"], payload[i]
        item = {"kind": "import", "ref": ref, "provenance": imp["provenance"], "destination": imp["destination"]}
        target = dict(ref, kind="import")
        dec = [policy.decide("source_read", target), policy.decide("destination_use", dict(target, destinationConversationId=destination))]
        item["decisions"] = dec
        if imp["destination"]["conversationId"] != destination:
            out.append(dict(item, status="denied", reason="destination_mismatch"))
        elif not all(d["allow"] for d in dec):
            out.append(dict(item, status="denied", reason="policy"))
        elif H.sha(text) != imp["textSha256"] or H.sha(text) != ref["contentHash"]:
            out.append(dict(item, status="unavailable", reason="hash_mismatch"))
        else:
            out.append(dict(item, status="included", label=f"imported:{imp['provenance']}", sha256=H.sha(text), text=text))
    return out


def _note(v: Verified) -> list[dict[str, Any]]:
    n = v.manifest["optional"]["note"]
    if n is None:
        return []
    return [{"kind": "note", "status": "included", "label": "claim", "author": n["author"], "sha256": n["sha256"],
             "text": v.envelope["payload"]["note"]}]


def _whitelist(v: Verified) -> dict[tuple[int, str], dict[str, Any]]:
    entries = list(v.manifest["evidenceIndex"]) + list(v.manifest["optional"]["evidence"])
    return {(e["seq"], e["kind"]): e for e in entries}


def _retrieve(v: Verified, policy: PolicyStub, destination: str, wanted: list[dict[str, Any]],
              retriever: Retriever | None) -> list[dict[str, Any]]:
    """Only manifest-selected pointers; authorization BEFORE the callback; at most RETRIEVAL_CALLS
    calls / RETRIEVAL_ITEMS items / RETRIEVAL_BYTES bytes; the callback can never broaden the
    whitelist (a result that does not echo the requested pointer is unavailable)."""
    wl = _whitelist(v)
    t = v.manifest["transition"]
    # Bound and dedupe the REQUEST before any policy check or callback (msg 1311): every wrapper,
    # denied/unavailable/included, counts against RETRIEVAL_ITEMS.
    if len(wanted) > RETRIEVAL_ITEMS:
        raise Refused("retrieval_request_too_large", len(wanted))
    keys: list[tuple[Any, Any]] = []
    for p in wanted:
        if (not isinstance(p, dict) or set(p) - {"seq", "kind"} or isinstance(p.get("seq"), bool) or not isinstance(p.get("seq"), int)
                or not isinstance(p.get("kind"), str) or len(p["kind"]) > 64):
            raise Refused("retrieval_request_malformed", p)
        k = (p["seq"], p["kind"])
        if k not in keys:
            keys.append(k)
    out, calls, items, used = [], 0, 0, 0
    for k in keys:
        target = {"kind": "journal", "root": t["root"], "claimKey": t["claimKey"], "seq": k[0], "recordKind": k[1]}
        item: dict[str, Any] = {"kind": "retrieval", "pointer": {"seq": k[0], "kind": k[1]}}
        if k not in wl:
            out.append(dict(item, status="denied", reason="not_selected"))
            continue
        # The FULL manifest pointer plus the SOURCE stream identity (msg 1314): a retriever bound to
        # another stream must refuse it, and its echo must name this exact source.
        full = dict(wl[k], root=t["root"], claimKey=t["claimKey"])
        item["pointer"] = full
        dec = [policy.decide("source_read", target), policy.decide("destination_use", dict(target, destinationConversationId=destination))]
        item["decisions"] = dec
        if not all(d["allow"] for d in dec):
            out.append(dict(item, status="denied", reason="policy"))
            continue
        if retriever is None or calls >= RETRIEVAL_CALLS or items >= RETRIEVAL_ITEMS:
            out.append(dict(item, status="unavailable", reason="cap" if retriever else "no_retriever"))
            continue
        calls += 1
        try:
            got = retriever(dict(full))
        except Exception:  # noqa: BLE001 -- an OPTIONAL source outage: unavailable, no exception or source text leaked
            out.append(dict(item, status="unavailable", reason="source_error"))
            continue
        if got is None:
            out.append(dict(item, status="unavailable", reason="not_found_or_invalid"))
            continue
        if not isinstance(got, dict) or got.get("pointer") != full or not isinstance(got.get("bytes"), str):
            out.append(dict(item, status="unavailable", reason="callback_mismatch"))   # never broadens the whitelist
            continue
        n = H.nbytes(got["bytes"])
        if used + n > RETRIEVAL_BYTES:
            out.append(dict(item, status="unavailable", reason="cap"))
            continue
        used += n
        items += 1
        out.append(dict(item, status="included", label="as-of", basis=got.get("basis"), sha256=H.sha(got["bytes"]), text=got["bytes"]))
    return out


# --- the view and its emitted part (034 §3, §4) ----------------------------------------------------------

def render(view: dict[str, Any]) -> tuple[str, str]:
    """(emitted part, viewDigest). viewDigest is over py-canon.v0(view) and is NOT in the view."""
    body = A.canonical(view)
    digest = H.sha(body)
    return f"<<handoff-view {VIEW_VERSION}>>\n{body}\n<<viewDigest {digest}>>\n", digest


def view_cost(view: dict[str, Any]) -> tuple[int, dict[str, Any], str, str]:
    """Fixed-width reservation fixed point: render with zeros, measure, write the value (same width)."""
    v = dict(view, reservationTokens="0" * RESERVATION_WIDTH)
    part, _ = render(v)
    cost = H.nbytes(part) + MESSAGE_OVERHEAD_TOKENS
    if len(str(cost)) > RESERVATION_WIDTH:
        raise Refused("CONTEXT_TOO_LARGE", "reservation exceeds its fixed width")
    v["reservationTokens"] = str(cost).zfill(RESERVATION_WIDTH)
    part, digest = render(v)
    assert H.nbytes(part) + MESSAGE_OVERHEAD_TOKENS == cost          # the fixed point holds by construction
    return cost, v, part, digest


H1Builder = Callable[[dict[str, Any]], dict[str, Any]]           # the pinned H1 options -> bridge result


@dataclass(frozen=True)
class Composition:
    view: dict[str, Any]
    view_digest: str
    part: str
    h1: dict[str, Any]
    view_cost: int


DROP_ORDER = ("retrieval", "import", "note")                     # optional items dropped first-to-last for budget


def compose(d: Delivery, fresh: Fresh, *, policy: PolicyStub, destination: str, h1: H1Builder,
            wanted: list[dict[str, Any]] = (), retriever: Retriever | None = None) -> Composition:
    """034 order: verify (refuse whole) -> revalidate (one snapshot, by the caller) -> policy and
    bounded retrieval (authorization before any callback) -> view -> measure (fixed point) -> H1 with
    the reduced window -> bind H1 -> final check. Writes nothing; caches nothing."""
    v = verify_delivery(d)
    reval = revalidate(v, fresh)
    m, task, h1_in = v.manifest, v.task, v.h1_input
    if (h1_in["systemInstruction"], h1_in["roleInstructions"]["fast"], h1_in["roleInstructions"]["deep"]) != (
            task["instructions"]["system"], task["instructions"]["fast"], task["instructions"]["deep"]):
        raise Refused("task_mismatch", "H1 instructions differ from the committed task")
    optional = _retrieve(v, policy, destination, list(wanted), retriever) + _imports(v, policy, destination) + _note(v)
    budget = h1_in["budget"]
    base = {
        "version": VIEW_VERSION, "codec": CODEC, "candidateDigest": d.candidate_digest,
        "receipt": {k: v.receipt[k] for k in ("handoffId", "recordId", "seq", "recordHash", "bindingLinkId")},
        "principal": policy.principal, "destination": destination,
        "policy": {"version": POLICY_VERSION, "digest": policy.digest()},
        "estimator": ESTIMATOR_ID, "budget": budget,
        "h1": {"suppliedSha256": m["task"]["suppliedSha256"], "instructions": m["task"]["instructions"]},
        "mandatory": {"authority": m["authority"], "state": m["state"], "obligations": m["obligations"],
                      "evidenceIndex": m["evidenceIndex"], "revalidation": reval, "transition": m["transition"]},
    }
    out_reserve = max(budget["fastOutputTokens"], budget["deepOutputTokens"]) + budget["safetyTokens"]
    while True:
        view = dict(base, optional=optional)
        cost, view, part, digest = view_cost(view)
        window = budget["windowTokens"] - cost
        if window - out_reserve < 0:
            res = None
        else:
            try:
                res = h1(dict(h1_in, budget=dict(budget, windowTokens=window)))
            except Exception:  # noqa: BLE001 -- the REQUIRED Task source failed: refuse, typed, no text leaked
                raise Refused("h1_unavailable") from None
            if not isinstance(res, dict) or not isinstance(res.get("ok"), bool):
                raise Refused("h1_unavailable", "result shape")
        if res is not None and res.get("ok"):
            break
        if res is not None and res.get("code") not in ("CONTEXT_TOO_LARGE",):
            raise Refused("h1_refused", res.get("code"))
        drop = next((i for kind in DROP_ORDER for i in range(len(optional) - 1, -1, -1)
                     if optional[i]["kind"] == kind and optional[i]["status"] == "included"), None)
        if drop is None:
            raise Refused("CONTEXT_TOO_LARGE", {"viewCost": cost, "window": budget["windowTokens"]})
        o = dict(optional[drop], status="omitted", reason="budget")
        o.pop("text", None)
        optional = optional[:drop] + [o] + optional[drop + 1:]
    # Bind H1 to the committed task: exactly one message, the same text and instruction digests.
    ctx = res.get("context") or {}
    if (res.get("text") != task["text"] or res.get("suppliedSha256") != m["task"]["suppliedSha256"]
            or ctx.get("messages") != [{"role": "user", "content": task["text"]}]):
        raise Refused("task_mismatch", "H1 output is not the committed task")
    if {k: H.sha(x) for k, x in (("system", ctx.get("systemInstruction", "")), ("fast", ctx.get("roleInstructions", {}).get("fast", "")),
                                  ("deep", ctx.get("roleInstructions", {}).get("deep", "")))} != m["task"]["instructions"]:
        raise Refused("task_mismatch", "H1 instructions are not the committed digests")
    return Composition(view, digest, part, res, cost)


# --- an H1-SHAPED stub for the default suite (NOT ChatAgent's H1) ----------------------------------------

def h1_stub(task_text: str, *, render_system: Callable[[str, str], str] = lambda s, r: f"{s}\n\n{r}") -> H1Builder:
    """Mimics the pinned contract's shape and budget arithmetic (bytes, 32/16 overheads, larger role,
    available = window - max(output) - safety) with an APPROXIMATE system rendering. The real H1 is
    exercised only in the opt-in interop_live suite."""
    def build(options: dict[str, Any]) -> dict[str, Any]:
        b = options["budget"]
        ri = options["roleInstructions"]
        role = ri["fast"] if H.nbytes(ri["fast"]) >= H.nbytes(ri["deep"]) else ri["deep"]
        fixed = REQUEST_OVERHEAD_TOKENS + H.nbytes(render_system(options["systemInstruction"], role)) + MESSAGE_OVERHEAD_TOKENS + \
            H.nbytes(task_text) + MESSAGE_OVERHEAD_TOKENS
        available = b["windowTokens"] - max(b["fastOutputTokens"], b["deepOutputTokens"]) - b["safetyTokens"]
        if fixed > available:
            return {"ok": False, "kind": "budget", "code": "CONTEXT_TOO_LARGE"}
        return {"ok": True, "text": task_text, "suppliedSha256": H.sha(task_text),
                "context": {"systemInstruction": options["systemInstruction"], "roleInstructions": dict(ri),
                            "messages": [{"role": "user", "content": task_text}]}}
    return build
