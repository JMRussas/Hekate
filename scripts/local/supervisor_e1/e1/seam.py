"""Pure claim-envelope validation and the OPAQUE work package (plan 023 E1a; test-only).

No I/O of any kind (a structural test enforces the import list). This is NOT ChatAgent's
context seam: there is no context renderer and no package canonicalization here. The work
package is an explicitly opaque token plus the receipt identities verbatim; H1 fields,
rendering, the package digest and case-16 interop are pending H1's accepted checkpoint.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .exact import WireError, counter, loads_exact

CONTRACT_VERSION = "plan-contract/v1"
CLAIM_KEY = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CONTENT_DIGEST = re.compile(r"^[0-9A-F]{64}$")   # PlanStore structured-content digest: UPPERCASE, no domain tag
PREREQ_DIGEST = re.compile(r"^[0-9a-f]{64}$")    # hekate-prereq/v1 digest: lowercase
WORK = {"todo", "in_progress", "done", "cancelled"}
OPAQUE_PREFIX = "e1-opaque:"


def _bad(what: str) -> WireError:
    return WireError("unexpected_shape", what)


def _obj(v: Any, what: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise _bad(f"{what} must be an object")
    return v


def _str(o: dict[str, Any], k: str, what: str, *, nullable: bool = False, pattern: re.Pattern[str] | None = None) -> str | None:
    v = o.get(k, ...)
    if v is ...:
        raise _bad(f"{what}.{k} missing")
    if v is None and nullable:
        return None
    if not isinstance(v, str) or (pattern is not None and not pattern.fullmatch(v)):
        raise _bad(f"{what}.{k} invalid: {v!r}")
    return v


def _bool(o: dict[str, Any], k: str, what: str) -> bool:
    v = o.get(k, ...)
    if not isinstance(v, bool):
        raise _bad(f"{what}.{k} must be a boolean")
    return v


@dataclass(frozen=True)
class Receipt:
    root_id: str
    claim_key: str
    outcome: str
    node_id: str | None
    attempt_id: str | None
    attempt_epoch: int | None
    executor_ref: str | None
    content_revision: int | None
    content_digest: str | None
    prereq_digest: str | None
    event_seq: int | None
    actor: str
    created_at: str
    content_snapshot_norm: str | None   # sorted-key JSON, for semantic equality only (not a digest)
    prereq_snapshot_norm: str | None


@dataclass(frozen=True)
class Current:
    work: str
    attempt_id: str | None
    attempt_epoch: int


@dataclass(frozen=True)
class Envelope:
    raw: bytes
    replayed: bool
    still_current: bool
    current: Current | None
    receipt: Receipt


def parse_claim_envelope(raw: bytes, *, expected_root: str | None = None, expected_key: str | None = None) -> Envelope:
    """Validate a WHOLE raw claim response: version, flags, outcome-dependent nullability,
    identities, digest forms and counters. Raises WireError on anything unexpected."""
    doc = _obj(loads_exact(raw), "claim response")
    if doc.get("contractVersion") != CONTRACT_VERSION:
        raise WireError("unsupported_contract_version", f"contractVersion={doc.get('contractVersion')!r}")
    replayed = _bool(doc, "replayed", "claim response")
    still_current = _bool(doc, "stillCurrent", "claim response")
    r = _obj(doc.get("receipt"), "receipt")
    outcome = _str(r, "outcome", "receipt")
    if outcome not in ("claimed", "no_ready_work"):
        raise _bad(f"receipt.outcome {outcome!r}")
    root = _str(r, "rootId", "receipt", pattern=UUID)
    key = _str(r, "claimKey", "receipt", pattern=CLAIM_KEY)
    if key in (".", ".."):
        raise _bad("receipt.claimKey is a dot segment")
    if expected_root is not None and root != expected_root:
        raise WireError("correlation_mismatch", f"receipt.rootId {root} != requested {expected_root}")
    if expected_key is not None and key != expected_key:
        raise WireError("correlation_mismatch", f"receipt.claimKey {key} != requested {expected_key}")
    actor = _str(r, "actor", "receipt")
    created = _str(r, "createdAt", "receipt")
    claimed = outcome == "claimed"
    nullable = not claimed

    node = _str(r, "nodeId", "receipt", nullable=nullable, pattern=UUID)
    attempt = _str(r, "attemptId", "receipt", nullable=nullable)
    epoch = counter(r.get("attemptEpoch"), "receipt.attemptEpoch", minimum=1, nullable=nullable)
    executor = _str(r, "executorRef", "receipt", nullable=True)
    content_rev = counter(r.get("contentRevision"), "receipt.contentRevision", minimum=1, nullable=nullable)
    content_digest = _str(r, "contentDigest", "receipt", nullable=nullable, pattern=CONTENT_DIGEST)
    prereq_digest = _str(r, "prereqDigest", "receipt", nullable=nullable, pattern=PREREQ_DIGEST)
    event_seq = counter(r.get("eventSeq"), "receipt.eventSeq", minimum=1, nullable=nullable)
    cs, ps = r.get("contentSnapshot", ...), r.get("prereqSnapshot", ...)
    if cs is ... or ps is ...:
        raise _bad("receipt snapshots missing")

    if claimed:
        _check_content_snapshot(cs)
        _check_prereq_snapshot(ps, node, prereq_digest)  # type: ignore[arg-type]
    else:
        if any(v is not None for v in (node, attempt, epoch, executor, content_rev, content_digest, prereq_digest, event_seq, cs, ps)):
            raise _bad("no_ready_work receipt carries attempt/content fields")
        if still_current:
            raise _bad("no_ready_work cannot be stillCurrent")

    cur = doc.get("current", ...)
    if cur is ...:
        raise _bad("current missing")
    current = None
    if cur is not None:
        c = _obj(cur, "current")
        work = _str(c, "work", "current")
        if work not in WORK:
            raise _bad(f"current.work {work!r}")
        current = Current(work, _str(c, "attemptId", "current", nullable=True), counter(c.get("attemptEpoch"), "current.attemptEpoch"))
    if not claimed and current is not None:
        raise _bad("no_ready_work cannot have current state")
    if still_current and (current is None or current.work != "in_progress" or current.attempt_id != attempt or current.attempt_epoch != epoch):
        raise WireError("correlation_mismatch", "stillCurrent=true but current attempt differs from the receipt")

    receipt = Receipt(root, key, outcome, node, attempt, epoch, executor, content_rev, content_digest, prereq_digest,
                      event_seq, actor, created, _normalized(cs), _normalized(ps))
    return Envelope(raw, replayed, still_current, current, receipt)


def _normalized(v: Any) -> str | None:
    """Order-insensitive semantic form for EQUALITY only (sorted object keys; arrays keep the
    server's canonical order). Not a digest and never sent anywhere."""
    return None if v is None else json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


CONTENT_KEYS = {"description", "acceptance_criteria", "verification_criteria", "success_criteria",
                "scope", "affected_files", "requirement_ids"}
GATES = {"completed", "accepted"}
DECISIONS = {"accepted", "rejected"}


def _list(o: dict[str, Any], k: str, what: str) -> list[Any]:
    v = o.get(k, ...)
    if not isinstance(v, list):
        raise _bad(f"{what}.{k} must be a list")
    return v


def _check_content_snapshot(cs: Any) -> None:
    o = _obj(cs, "receipt.contentSnapshot")
    if set(o) != {"value", "attributes"}:
        raise _bad("receipt.contentSnapshot keys")
    _str(o, "value", "receipt.contentSnapshot", nullable=True)
    attrs = o["attributes"]
    if attrs is not None:
        a = _obj(attrs, "receipt.contentSnapshot.attributes")
        for k, v in a.items():
            if k not in CONTENT_KEYS or not isinstance(v, str):
                raise _bad(f"receipt.contentSnapshot.attributes[{k!r}]")


def _check_acceptance(v: Any, what: str) -> None:
    if v is None:
        return
    a = _obj(v, what)
    if _str(a, "decision", what) not in DECISIONS:
        raise _bad(f"{what}.decision")
    counter(a.get("contentRevision"), f"{what}.contentRevision", minimum=1)
    counter(a.get("attemptEpoch"), f"{what}.attemptEpoch")
    _str(a, "decidedBy", what)
    for k in ("artifactRef", "attemptId", "evidenceRef"):
        _str(a, k, what, nullable=True)


def _check_prereq_snapshot(ps: Any, node: str, prereq_digest: str) -> None:
    o = _obj(ps, "receipt.prereqSnapshot")
    if o.get("digest") != prereq_digest:
        raise WireError("correlation_mismatch", "prereqSnapshot.digest != prereqDigest")
    chain = _list(o, "chain", "prereqSnapshot")
    if not chain:
        raise _bad("prereqSnapshot.chain is empty")
    chain_ids = set()
    for i, raw_owner in enumerate(chain):
        w = f"prereqSnapshot.chain[{i}]"
        owner = _obj(raw_owner, w)
        chain_ids.add(_str(owner, "id", w, pattern=UUID))
        _str(owner, "nodeType", w)
        _str(owner, "parentId", w, nullable=True, pattern=UUID)
    if _obj(chain[0], "prereqSnapshot.chain[0]")["id"] != node:
        raise WireError("correlation_mismatch", "prereqSnapshot.chain must start at the claimed node")

    node_ids = set()
    for i, raw_node in enumerate(_list(o, "nodes", "prereqSnapshot")):
        w = f"prereqSnapshot.nodes[{i}]"
        n = _obj(raw_node, w)
        node_ids.add(_str(n, "id", w, pattern=UUID))
        kind = _str(n, "kind", w)
        _str(n, "nodeType", w)
        _str(n, "parentId", w, nullable=True, pattern=UUID)
        counter(n.get("contentRevision"), f"{w}.contentRevision", minimum=1)
        counter(n.get("attemptEpoch"), f"{w}.attemptEpoch")
        children = _list(n, "children", w)
        for j, c in enumerate(children):
            if not isinstance(c, str) or not UUID.fullmatch(c):
                raise _bad(f"{w}.children[{j}]")
        if kind == "leaf":
            if children or n.get("work") not in WORK:
                raise _bad(f"{w}: a leaf has no children and a known work status")
        elif kind == "container":
            if any(n.get(k) is not None for k in ("work", "attemptId", "artifactRef", "acceptance", "pinnedContentRevision", "pinnedPrereqDigest")):
                raise _bad(f"{w}: a container carries no state")
        else:
            raise _bad(f"{w}.kind {kind!r}")
        _str(n, "attemptId", w, nullable=True)
        _str(n, "artifactRef", w, nullable=True)
        if "acceptance" not in n:
            raise _bad(f"{w}.acceptance missing")
        _check_acceptance(n["acceptance"], f"{w}.acceptance")
        counter(n.get("pinnedContentRevision"), f"{w}.pinnedContentRevision", minimum=1, nullable=True)
        _str(n, "pinnedPrereqDigest", w, nullable=True, pattern=PREREQ_DIGEST)
    if node in node_ids:
        raise _bad("prereqSnapshot.nodes must not contain the claimed node itself")

    for i, raw_edge in enumerate(_list(o, "declared", "prereqSnapshot")):
        w = f"prereqSnapshot.declared[{i}]"
        e = _obj(raw_edge, w)
        if _str(e, "ownerId", w, pattern=UUID) not in chain_ids:
            raise _bad(f"{w}.ownerId is not in the chain")
        if _str(e, "predecessorId", w, pattern=UUID) not in node_ids:
            raise _bad(f"{w}.predecessorId is not a recorded node")
        if _str(e, "gate", w) not in GATES:
            raise _bad(f"{w}.gate")


@dataclass(frozen=True)
class OpaqueWorkPackage:
    """Explicitly opaque stand-in for H1's work package. NOT a rendered context, NOT a canonical digest."""
    token: str
    root_id: str
    claim_key: str
    node_id: str
    attempt_id: str
    attempt_epoch: int
    executor_ref: str | None
    event_seq: int
    content_revision: int
    content_digest: str
    prereq_digest: str


def opaque_package(env: Envelope) -> OpaqueWorkPackage:
    """Pure: receipt identities verbatim plus an opaque token. Refuses a no_ready_work receipt."""
    r = env.receipt
    if r.outcome != "claimed":
        raise WireError("no_package", "a no_ready_work receipt yields no work package")
    return OpaqueWorkPackage(OPAQUE_PREFIX + r.claim_key, r.root_id, r.claim_key, r.node_id, r.attempt_id,  # type: ignore[arg-type]
                             r.attempt_epoch, r.executor_ref, r.event_seq, r.content_revision,  # type: ignore[arg-type]
                             r.content_digest, r.prereq_digest)  # type: ignore[arg-type]
