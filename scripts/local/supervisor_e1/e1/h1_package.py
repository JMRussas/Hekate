"""ChatAgent H1 package as the supervisor's work package (plan 023 E1b; test-only).

Only H1's ACTUAL fields are used (no invented packageDigest). H1 output is checked as typed
provenance against the supervisor's own envelope parse of the SAME raw bytes, and
suppliedSha256 is recomputed independently from the exact text. Refusals map to
PackageRefused before any dispatch: CONTEXT_TOO_LARGE / PACKAGE_TOO_LARGE are overflow, every
other H1 code is a refusal.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .exact import WireError, counter
from .h1_bridge import build
from .seam import CONTENT_DIGEST, PREREQ_DIGEST, Envelope
from .supervisor import PackageRefused

OVERFLOW_CODES = frozenset({"CONTEXT_TOO_LARGE", "PACKAGE_TOO_LARGE"})
LOWER_SHA = PREREQ_DIGEST   # 64 lowercase hex


class H1ProvenanceError(WireError):
    def __init__(self, what: str):
        super().__init__("h1_provenance_mismatch", what)


@dataclass(frozen=True)
class H1WorkPackage:
    token: str                 # local label "h1:<claimKey>"; not a digest, never sent to PlanStore
    root_id: str
    claim_key: str
    node_id: str
    attempt_id: str
    attempt_epoch: int
    executor_ref: str | None
    event_seq: int
    content_revision: int
    content_digest: str        # Hekate structured digest, verbatim (uppercase)
    prereq_digest: str         # Hekate prerequisite digest, verbatim (lowercase)
    supplied_sha256: str       # H1: SHA-256 of the exact supplied text (lowercase); NOT a Hekate digest
    text: str                  # the exact mandatory task text H1 supplies
    system_instruction: str    # captured separately; not part of text or its hash
    role_fast: str
    role_deep: str
    supplied_json: str         # H1 source.supplied (requirement/prerequisites/rule hashes), sorted-key JSON
    replayed: bool             # historical fact at build time; excluded from semantic identity
    still_current: bool        # historical fact at build time; excluded from semantic identity
    runtime: str               # node path/version and pinned H1 commit, for evidence


def semantic_identity(pkg: H1WorkPackage) -> tuple[Any, ...]:
    """Everything that defines WHAT is supplied, including each separately supplied instruction
    (system, fast, deep), which the task-text hash does not cover. Excludes replayed/stillCurrent,
    snapshotId and runtime."""
    return (pkg.root_id, pkg.claim_key, pkg.node_id, pkg.attempt_id, pkg.attempt_epoch, pkg.executor_ref, pkg.event_seq,
            pkg.content_revision, pkg.content_digest, pkg.prereq_digest, pkg.supplied_sha256, pkg.text, pkg.supplied_json,
            pkg.system_instruction, pkg.role_fast, pkg.role_deep)


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def package_from(result: dict[str, Any], env: Envelope, *, system_instruction: str, role_fast: str, role_deep: str) -> H1WorkPackage:
    if result.get("ok") is not True:
        raise H1ProvenanceError("not an ok H1 result")
    text, supplied_sha = result.get("text"), result.get("suppliedSha256")
    if not isinstance(text, str) or not isinstance(supplied_sha, str) or not LOWER_SHA.fullmatch(supplied_sha):
        raise H1ProvenanceError("text/suppliedSha256 types")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != supplied_sha:
        raise H1ProvenanceError("suppliedSha256 is not SHA-256 of the exact text")
    src = result.get("source")
    if not isinstance(src, dict) or src.get("kind") != "hekate-plan-leaf" or src.get("contractVersion") != "plan-contract/v1":
        raise H1ProvenanceError("source kind/contractVersion")
    r = env.receipt
    pairs = {"rootId": r.root_id, "nodeId": r.node_id, "claimKey": r.claim_key, "attemptId": r.attempt_id,
             "attemptEpoch": r.attempt_epoch, "executorRef": r.executor_ref, "contentRevision": r.content_revision,
             "contentDigest": r.content_digest, "prereqDigest": r.prereq_digest, "eventSeq": r.event_seq,
             "actor": r.actor, "createdAt": r.created_at, "replayed": env.replayed, "stillCurrent": env.still_current}
    for k, expected in pairs.items():
        if not _same(src.get(k), expected):
            raise H1ProvenanceError(f"source.{k} differs from the envelope")
    for k in ("attemptEpoch", "contentRevision", "eventSeq"):
        counter(src[k], f"source.{k}", minimum=1)
    if not CONTENT_DIGEST.fullmatch(src["contentDigest"]) or not PREREQ_DIGEST.fullmatch(src["prereqDigest"]):
        raise H1ProvenanceError("Hekate digest forms")
    sup = src.get("supplied")
    if not isinstance(sup, dict) or set(sup) != {"requirementSha256", "prerequisitesSha256", "rules"}:
        raise H1ProvenanceError("source.supplied shape")
    for k in ("requirementSha256", "prerequisitesSha256"):
        if not isinstance(sup[k], str) or not LOWER_SHA.fullmatch(sup[k]):
            raise H1ProvenanceError(f"source.supplied.{k}")
    if not isinstance(sup["rules"], list) or any(
            not isinstance(x, dict) or set(x) != {"path", "revision", "sha256"} or not all(isinstance(v, str) for v in x.values())
            or not LOWER_SHA.fullmatch(x["sha256"]) for x in sup["rules"]):
        raise H1ProvenanceError("source.supplied.rules")
    ctx = result.get("context")
    if (not isinstance(ctx, dict) or ctx.get("systemInstruction") != system_instruction
            or ctx.get("roleInstructions") != {"fast": role_fast, "deep": role_deep}
            or ctx.get("messages") != [{"role": "user", "content": text}]):
        raise H1ProvenanceError("context must carry the given instructions and exactly the package text")
    return H1WorkPackage(
        token=f"h1:{r.claim_key}", root_id=r.root_id, claim_key=r.claim_key, node_id=r.node_id,  # type: ignore[arg-type]
        attempt_id=r.attempt_id, attempt_epoch=r.attempt_epoch, executor_ref=r.executor_ref,  # type: ignore[arg-type]
        event_seq=r.event_seq, content_revision=r.content_revision, content_digest=r.content_digest,  # type: ignore[arg-type]
        prereq_digest=r.prereq_digest, supplied_sha256=supplied_sha, text=text,  # type: ignore[arg-type]
        system_instruction=system_instruction, role_fast=role_fast, role_deep=role_deep,
        supplied_json=json.dumps(sup, sort_keys=True, separators=(",", ":")),
        replayed=env.replayed, still_current=env.still_current,
        runtime=json.dumps(result.get("_runtime"), sort_keys=True))


def h1_builder(*, rules: list[dict[str, str]], system_instruction: str, role_fast: str, role_deep: str,
               budget: dict[str, int], captured_at_iso: str, limits: dict[str, int] | None = None,
               repo: Path | None = None) -> Callable[[Envelope], H1WorkPackage]:
    def build_package(env: Envelope) -> H1WorkPackage:
        options: dict[str, Any] = {
            "response": env.raw.decode("utf-8"), "rules": rules, "systemInstruction": system_instruction,
            "roleInstructions": {"fast": role_fast, "deep": role_deep}, "budget": budget, "capturedAtIso": captured_at_iso,
        }
        if limits is not None:
            options["limits"] = limits
        result = build(options, repo=repo)
        if result.get("ok") is not True:
            code = str(result.get("code"))
            raise PackageRefused(code, overflow=code in OVERFLOW_CODES)
        return package_from(result, env, system_instruction=system_instruction, role_fast=role_fast, role_deep=role_deep)
    return build_package
