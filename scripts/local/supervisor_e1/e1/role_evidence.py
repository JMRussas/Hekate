"""Managed role evidence manifest (`hekate-role-evidence.v0`). Pure and offline: no file, network, DB or clock access.

build_manifest(identity, payloads, max_file_bytes=, max_total_bytes=) -> BuiltManifest (canonical body bytes + sha256)
verify_manifest(body, payloads, expected=, max_file_bytes=, max_total_bytes=, expected_digest=None) -> VerifyResult

The host supplies named exact bytes plus strict metadata. Nothing here reads, scrubs, executes or interprets payload
content; bytes are only measured and hashed. See ROLE-EVIDENCE.md for the limitations (same-machine hashes do not
authenticate an actor, linkage is host-asserted or unlinked, `review_export` is a label and not proof of sanitizing).

Encoding: json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False) as UTF-8. Versioned
and deterministic for this module only; no RFC 8785 or cross-language equivalence is claimed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA = "hekate-role-evidence.v0"
MAX_PAYLOADS = 32
MAX_NAME = 64
MAX_MANIFEST_BYTES = 64 * 1024
MAX_FILE_CEILING = 16 * 1024 * 1024
MAX_TOTAL_CEILING = 64 * 1024 * 1024
ACCESS = ("restricted_raw", "review_export")
LINKAGE = ("unlinked", "host_asserted")
_MAX_INT = 2**53 - 1  # largest safe JS integer
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}", re.ASCII)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,%d}" % (MAX_NAME - 1), re.ASCII)
_CTYPE = re.compile(r"[a-z0-9.+-]{1,32}/[a-z0-9.+-]{1,32}", re.ASCII)


class EvidenceRefused(Exception):
    """Typed refusal. `code` is a fixed token; the message never carries caller content."""

    def __init__(self, code: str, name: str | None = None):
        super().__init__(code)
        self.code = code
        self.name = name


@dataclass(frozen=True, slots=True)
class Payload:
    """One named exact payload. `data` must be `bytes`; classification is a label and never changes the bytes."""

    data: bytes
    content_type: str
    purpose: str
    access: str


@dataclass(frozen=True, slots=True)
class BuiltManifest:
    body: bytes  # canonical manifest bytes; `digest` is sha256 of exactly these bytes (no self-hash inside)
    digest: str

    def manifest(self) -> dict[str, Any]:
        """A fresh dict on every call, so callers cannot mutate the built artifact."""
        return json.loads(self.body)


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """Byte/identity consistency only. `code` is a fixed token ('ok' on success); never contains content."""

    ok: bool
    code: str
    name: str | None = None


def _fail(code: str) -> EvidenceRefused:
    return EvidenceRefused(code)


def _id(v: Any) -> str:
    if type(v) is not str or not _ID.fullmatch(v):
        raise _fail("identity")
    return v


def _uuid(v: Any) -> str:
    if type(v) is not str or not _UUID.fullmatch(v):
        raise _fail("identity")
    return v


def _sha(v: Any) -> str:
    if type(v) is not str or not _SHA.fullmatch(v):
        raise _fail("identity")
    return v


def _int(v: Any, lo: int = 1) -> int:
    if type(v) is not int or not lo <= v <= _MAX_INT:  # type() rejects bool
        raise _fail("identity")
    return v


def _opt_id(v: Any) -> str | None:
    return None if v is None else _id(v)


def _obj(v: Any, spec: Mapping[str, Any], *, partial: bool = False) -> dict[str, Any]:
    if not isinstance(v, Mapping) or not all(type(k) is str for k in v):
        raise _fail("schema")
    if (set(v) - set(spec)) or (not partial and set(spec) - set(v)):
        raise _fail("schema")  # unknown or missing field
    return {k: spec[k](v[k]) for k in v}


def _limits(v: Any) -> dict[str, int]:
    if not isinstance(v, Mapping) or len(v) > 16 or not all(type(k) is str for k in v):
        raise _fail("schema")
    return {_id(k): _int(x, 0) for k, x in v.items()}


def _refs(v: Any) -> list[str]:
    if type(v) not in (list, tuple) or len(v) > 8:
        raise _fail("schema")
    return [_id(x) for x in v]


def _linkage(v: Any) -> str:
    if v not in LINKAGE or type(v) is not str:
        raise _fail("identity")
    return v


# "unknown" is an explicit, allowed value wherever the host cannot attest binding/provider/model.
_IDENTITY: dict[str, Any] = {
    "planRoot": _uuid, "taskId": _uuid, "runId": _id, "attemptId": _id,
    "epoch": _int, "stateRevision": _int, "contentRevision": _int, "claimKey": _opt_id, "operationKey": _opt_id,
    "role": lambda v: _obj(v, {"id": _id, "version": _id, "definitionSha256": _sha}),
    "binding": lambda v: _obj(v, {"binding": _id, "provider": _id, "model": _id}),
    "limits": _limits,
    "source": lambda v: _obj(v, {"revision": _id, "snapshotSha256": _sha}),
    "reviewerRefs": _refs, "linkage": _linkage,
}
_REQUIRED_EXPECTED = ("planRoot", "taskId", "runId", "attemptId", "epoch", "stateRevision", "contentRevision")


def _identity(v: Any, *, partial: bool = False) -> dict[str, Any]:
    out = _obj(v, _IDENTITY, partial=partial)
    if out.get("linkage") == "unlinked" and out.get("claimKey") is not None:
        raise _fail("identity")  # an unlinked stream has no claim receipt; operationKey is only a correlation label
    return out


def _canonical(obj: Any) -> bytes:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise _fail("schema") from None


def _budgets(max_file: Any, max_total: Any) -> None:
    for v, ceiling in ((max_file, MAX_FILE_CEILING), (max_total, MAX_TOTAL_CEILING)):
        if type(v) is not int or not 1 <= v <= ceiling:
            raise _fail("budget")
    if max_file > max_total:
        raise _fail("budget")


def _name(v: Any) -> str:
    if type(v) is not str or not _NAME.fullmatch(v) or ".." in v or v.endswith("."):
        raise _fail("name")
    return v


def _check_sizes(sizes: Mapping[str, int], max_file: int, max_total: int) -> None:
    if len(sizes) > MAX_PAYLOADS:
        raise _fail("count")
    if len({n.lower() for n in sizes}) != len(sizes):
        raise _fail("name")  # case-fold collision (Windows hosts)
    if any(s > max_file for s in sizes.values()) or sum(sizes.values()) > max_total:
        raise _fail("budget")


def _payload_bytes(payloads: Any) -> dict[str, bytes]:
    if not isinstance(payloads, Mapping):
        raise _fail("schema")
    if len(payloads) > MAX_PAYLOADS:
        raise _fail("count")
    return {_name(n): p for n, p in payloads.items()}


def build_manifest(identity: Mapping[str, Any], payloads: Mapping[str, Payload], *, max_file_bytes: int,
                   max_total_bytes: int) -> BuiltManifest:
    """Validate everything and enforce byte budgets from lengths BEFORE hashing; raise EvidenceRefused otherwise."""
    _budgets(max_file_bytes, max_total_bytes)
    ident = _identity(identity)
    if set(ident) != set(_IDENTITY):
        raise _fail("schema")
    items = _payload_bytes(payloads)
    for p in items.values():
        if not isinstance(p, Payload) or type(p.data) is not bytes:
            raise _fail("schema")
        if p.access not in ACCESS or type(p.access) is not str:
            raise _fail("access")
        if type(p.content_type) is not str or not _CTYPE.fullmatch(p.content_type) or type(p.purpose) is not str \
                or not _ID.fullmatch(p.purpose):
            raise _fail("metadata")
    _check_sizes({n: len(p.data) for n, p in items.items()}, max_file_bytes, max_total_bytes)
    entries = [{"name": n, "contentType": p.content_type, "purpose": p.purpose, "access": p.access,
                "bytes": len(p.data), "sha256": hashlib.sha256(p.data).hexdigest()} for n, p in sorted(items.items())]
    body = _canonical({"schema": SCHEMA, "identity": ident, "payloads": entries, "authority": "none",
                       "integrity": "same_machine_sha256_unauthenticated"})
    if len(body) > MAX_MANIFEST_BYTES:
        raise _fail("metadata_size")
    return BuiltManifest(body, hashlib.sha256(body).hexdigest())


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    d = dict(pairs)
    if len(d) != len(pairs):
        raise ValueError("duplicate key")
    return d


def _bad_const(_: str) -> None:
    raise ValueError("non-finite")


def _parse(body: Any) -> dict[str, Any]:
    if type(body) is not bytes:
        raise _fail("schema")
    if len(body) > MAX_MANIFEST_BYTES:
        raise _fail("metadata_size")  # checked before parsing
    try:
        doc = json.loads(body.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_bad_const)
    except (ValueError, RecursionError):
        raise _fail("schema") from None
    entry = {"name": _name, "contentType": lambda v: v, "purpose": lambda v: v, "access": lambda v: v,
             "bytes": lambda v: _int(v, 0), "sha256": _sha}
    top = _obj(doc, {"schema": lambda v: v, "identity": lambda v: v, "payloads": lambda v: v,
                     "authority": lambda v: v, "integrity": lambda v: v})
    if top["schema"] != SCHEMA:
        raise _fail("schema")
    if top["authority"] != "none" or top["integrity"] != "same_machine_sha256_unauthenticated":
        raise _fail("schema")
    top["identity"] = _identity(top["identity"])
    if set(top["identity"]) != set(_IDENTITY):
        raise _fail("schema")
    if type(top["payloads"]) is not list or len(top["payloads"]) > MAX_PAYLOADS:
        raise _fail("count")
    top["payloads"] = [_obj(e, entry) for e in top["payloads"]]
    for e in top["payloads"]:
        if e["access"] not in ACCESS or not _CTYPE.fullmatch(str(e["contentType"])) \
                or not _ID.fullmatch(str(e["purpose"])) or type(e["contentType"]) is not str \
                or type(e["purpose"]) is not str or type(e["access"]) is not str:
            raise _fail("metadata")
    if _canonical(top) != body:
        raise _fail("canonical")  # non-canonical encodings are refused, not re-normalized
    return top


def verify_manifest(body: bytes, payloads: Mapping[str, bytes], *, expected: Mapping[str, Any], max_file_bytes: int,
                    max_total_bytes: int, expected_digest: str | None = None) -> VerifyResult:
    """Check schema, caller-expected identity, exact membership, lengths and hashes. Never raises on bad input.

    `expected` must name at least planRoot, taskId, runId, attemptId, epoch, stateRevision and contentRevision (other
    identity keys are compared when present; epoch is the attempt epoch, never a content revision), so the manifest is checked against what the CALLER expects and not against itself.
    """
    try:
        _budgets(max_file_bytes, max_total_bytes)
        exp = _identity(expected, partial=True)
        if any(k not in exp for k in _REQUIRED_EXPECTED):
            raise _fail("expected")
        if expected_digest is not None:
            _sha(expected_digest)
        data = _payload_bytes(payloads)
        if any(type(d) is not bytes for d in data.values()):
            raise _fail("schema")
        _check_sizes({n: len(d) for n, d in data.items()}, max_file_bytes, max_total_bytes)
        top = _parse(body)
        if expected_digest is not None and not hmac.compare_digest(hashlib.sha256(body).hexdigest(), expected_digest):
            raise _fail("digest")
        if any(top["identity"][k] != v for k, v in exp.items()):
            raise _fail("identity_mismatch")
        listed = {e["name"]: e for e in top["payloads"]}
        if len(listed) != len(top["payloads"]):
            raise _fail("name")
        _check_sizes({n: e["bytes"] for n, e in listed.items()}, max_file_bytes, max_total_bytes)
        for n in sorted(listed):
            if n not in data:
                raise EvidenceRefused("missing", n)
        for n in sorted(data):
            if n not in listed:
                raise EvidenceRefused("extra", n)
        for n, e in listed.items():  # lengths first for every file, then hashes
            if len(data[n]) != e["bytes"]:
                raise EvidenceRefused("length", n)
        for n, e in listed.items():
            if not hmac.compare_digest(hashlib.sha256(data[n]).hexdigest(), e["sha256"]):
                raise EvidenceRefused("hash", n)
    except EvidenceRefused as exc:
        return VerifyResult(False, exc.code, exc.name)
    return VerifyResult(True, "ok")
