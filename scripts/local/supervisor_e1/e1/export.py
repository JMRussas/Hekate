"""handoff-export.v0 producer (shared contract frozen in bridge msgs 1549/1550; plan 1532). TEST-SCOPED.

Writes ONE handoff export directory that ChatAgent's `handoff compose --export <dir>` reads (consumer at
ChatAgent 2383e85, src/integrations/hekate/handoffConsumer/cli.ts `readExport`):

  delivery/{wrapper.json, manifest.bin, envelope.bin, task.bin, receipt.json, h1-input.json}
  fresh.json  policy.json  request.json  retrieval.json  expected.json  view-part.txt  provenance.json
  INDEX.sha256   (written LAST; it excludes itself)

- EXCLUSIVE: the directory must not exist; it is created once and never written again after the index.
- The index is the consumer's canonical form: for every file, sorted by its fixed relative path,
  `<lowercase sha256>  <path>\\n`.
- Every file is checked against the consumer's read cap before anything is written.
- After writing, the whole directory is re-read and verified: exactly the fixed layout (no extra, missing
  or linked entry), and the index equals the one rebuilt from the bytes on disk.
- expected.json is `handoff-expectation.v0`. It is written with h1Builder "chatagent-h1" ONLY when the
  composition really used ChatAgent's H1 (the opt-in path). A stub composition can be exported only by
  tests, and then expected.json names a builder the consumer refuses, so it can never pass as real parity.
- provenance.json is PRODUCER-DECLARED metadata (the consumer reads only exportVersion). Nothing here
  authenticates it; it is recorded by hash on the consumer side.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from e1 import acts as A
from e1 import consumer as C

EXPORT_VERSION = "handoff-export.v0"
EXPECTATION_VERSION = "handoff-expectation.v0"
REAL_H1 = "chatagent-h1"
STUB_H1 = "hekate-h1-stub-NOT-chatagent"     # deliberately not accepted by the consumer
INDEX = "INDEX.sha256"
MIB = 1 << 20
JSON_INPUT_MAX = MIB
# The consumer's read caps (cli.ts 84-87, delivery.ts INGRESS_LIMITS), enforced here before writing.
CAPS: dict[str, int] = {
    "delivery/wrapper.json": 4096,
    "delivery/manifest.bin": MIB,
    "delivery/envelope.bin": MIB,
    "delivery/task.bin": MIB,
    "delivery/receipt.json": 4096,
    "delivery/h1-input.json": C.H1_RESPONSE_MAX + C.H1_RULE_BYTES_MAX + 4 * C.H1_INSTRUCTION_MAX,
    "fresh.json": JSON_INPUT_MAX,
    "policy.json": JSON_INPUT_MAX,
    "request.json": JSON_INPUT_MAX,
    "retrieval.json": JSON_INPUT_MAX,
    "expected.json": 4096,
    "view-part.txt": JSON_INPUT_MAX,
    "provenance.json": 64 << 10,
}
INDEX_MAX = 4096
FILES = tuple(sorted(CAPS))                    # exactly 13 fixed relative paths


class ExportRefused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def jbytes(value: Any) -> bytes:
    """py-canon.v0 bytes (sorted keys, no whitespace, raw UTF-8): deterministic and strictly readable."""
    return A.canonical(value).encode("utf-8")


def canonical_index(files: dict[str, bytes]) -> bytes:
    return "".join(f"{sha(files[p])}  {p}\n" for p in sorted(files)).encode("utf-8")


# --- the parts ------------------------------------------------------------------------------------------

def fresh_doc(fresh: C.Fresh) -> dict[str, Any]:
    """The snake_case as-of proof exactly as the consumer's toFresh reads it (basis optional)."""
    d = asdict(fresh)
    return {k: d[k] for k in ("candidate_digest", "record_id", "review_identity", "package_ref", "receipt_status",
                              "current_binding", "review_class", "pins_problem", "pending", "queue", "basis")}


def policy_doc(policy: C.PolicyStub) -> dict[str, Any]:
    return {"principal": policy.principal, "rules": [dict(r) for r in policy.rules]}


def expected_doc(comp: C.Composition, *, h1_builder: str) -> dict[str, Any]:
    """handoff-expectation.v0 (frozen in msg 1515/1524), from the producer's OWN composition."""
    return {"version": EXPECTATION_VERSION, "h1Builder": h1_builder, "viewDigest": comp.view_digest,
            "viewPartSha256": sha(comp.part.encode("utf-8")), "reservationTokens": comp.view["reservationTokens"],
            "viewCost": comp.view_cost, "h1SuppliedSha256": comp.h1["suppliedSha256"],
            "candidateDigest": comp.view["candidateDigest"]}


PROVENANCE_REQUIRED = ("hekateCommit", "hekateTreeClean", "runId", "planRoot", "nodeId", "attemptId", "attemptEpoch",
                       "candidateDigest", "recordId", "viewDigest", "h1Bridge", "worker", "reviewer", "synthetic")


def provenance_doc(declared: dict[str, Any]) -> dict[str, Any]:
    """exportVersion + the producer-declared fields proposed in msg 1577 (closed set; no renames)."""
    missing = [k for k in PROVENANCE_REQUIRED if k not in declared]
    extra = sorted(set(declared) - set(PROVENANCE_REQUIRED))
    if missing or extra:
        raise ExportRefused("provenance_fields", f"missing {missing} extra {extra}")
    w = declared["worker"]
    if not (isinstance(w, dict) and set(w) == {"kind", "requestedModel", "reportedModels", "reportedModelsAuthenticated"}
            and w["kind"] in ("claude-cli", "codex-cli", "fake") and w["reportedModelsAuthenticated"] is False):
        raise ExportRefused("provenance_worker")
    r = declared["reviewer"]
    if not (isinstance(r, dict) and set(r) == {"kind", "inputSha256"} and r["kind"] in ("deterministic-verifier", "model-session")):
        raise ExportRefused("provenance_reviewer")
    if not (isinstance(declared["h1Bridge"], dict) and set(declared["h1Bridge"]) == {"chatagentCommit", "nodeVersion"}):
        raise ExportRefused("provenance_h1_bridge")
    if not isinstance(declared["synthetic"], bool):
        raise ExportRefused("provenance_synthetic")
    return {"exportVersion": EXPORT_VERSION, **declared}


# --- the publisher --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ExportInputs:
    delivery: C.Delivery
    fresh: C.Fresh
    policy: C.PolicyStub
    destination: str
    composition: C.Composition
    h1_builder: str                     # REAL_H1 only when ChatAgent's real H1 composed this view
    provenance: dict[str, Any]
    wanted: tuple = ()                  # the first export: wanted = [] (root, msg 1549)
    retrieval: tuple = ()               # recorded {request, result} pairs; the first export: []


def export_files(inp: ExportInputs, *, allow_stub: bool = False) -> dict[str, bytes]:
    """The 13 file contents, validated. Nothing is written here."""
    if inp.h1_builder != REAL_H1 and not (allow_stub and inp.h1_builder == STUB_H1):
        raise ExportRefused("h1_builder", "only a real ChatAgent H1 composition is exportable outside tests")
    if inp.wanted or inp.retrieval:
        raise ExportRefused("first_export_scope", "the first export has wanted=[] and retrieval=[]")
    d = inp.delivery
    if (d.wrapper, d.codec) != (C.WRAPPER, C.CODEC) or d.candidate_digest != inp.composition.view["candidateDigest"]:
        raise ExportRefused("delivery_mismatch")
    if inp.fresh.candidate_digest != d.candidate_digest:
        raise ExportRefused("fresh_mismatch")
    if inp.composition.view["destination"] != inp.destination:
        raise ExportRefused("destination_mismatch")
    prov = provenance_doc(inp.provenance)
    if (prov["candidateDigest"], prov["viewDigest"]) != (d.candidate_digest, inp.composition.view_digest):
        raise ExportRefused("provenance_binding")
    files = {
        "delivery/wrapper.json": jbytes({"wrapper": d.wrapper, "codec": d.codec, "candidateDigest": d.candidate_digest}),
        "delivery/manifest.bin": d.manifest, "delivery/envelope.bin": d.envelope, "delivery/task.bin": d.task,
        "delivery/receipt.json": d.receipt, "delivery/h1-input.json": d.h1_input,
        "fresh.json": jbytes(fresh_doc(inp.fresh)),
        "policy.json": jbytes(policy_doc(inp.policy)),
        "request.json": jbytes({"destination": inp.destination, "wanted": list(inp.wanted)}),
        "retrieval.json": jbytes(list(inp.retrieval)),
        "expected.json": jbytes(expected_doc(inp.composition, h1_builder=inp.h1_builder)),
        "view-part.txt": inp.composition.part.encode("utf-8"),
        "provenance.json": jbytes(prov),
    }
    assert tuple(sorted(files)) == FILES

    def no_float(v: Any, where: str) -> None:
        """The consumer refuses JSON floats (codec_unsupported); refuse them before writing (review 1593 note)."""
        if isinstance(v, float):
            raise ExportRefused("float_value", where)
        if isinstance(v, dict):
            for k, x in v.items():
                no_float(x, f"{where}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                no_float(x, f"{where}[{i}]")
    for path in ("delivery/wrapper.json", "fresh.json", "policy.json", "request.json", "retrieval.json", "expected.json",
                 "provenance.json"):
        no_float(json.loads(files[path]), path)
    for path, data in files.items():
        if not isinstance(data, (bytes, bytearray)) or len(data) > CAPS[path]:
            raise ExportRefused("file_cap", path)
    if len(canonical_index(files)) > INDEX_MAX:
        raise ExportRefused("index_cap")
    return files


def publish(out: Path, files: dict[str, bytes]) -> dict[str, str]:
    """EXCLUSIVE publication: `out` must not exist. Files are written with O_EXCL; the index LAST.
    Returns {path: sha256} for all 13 files plus the index. Never overwrites anything."""
    out = Path(out)
    if out.exists() or out.is_symlink():
        raise ExportRefused("out_exists")
    if tuple(sorted(files)) != FILES:
        raise ExportRefused("layout")
    out.mkdir(parents=False)
    (out / "delivery").mkdir()

    def write_new(path: Path, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

    for rel in FILES:
        write_new(out.joinpath(*rel.split("/")), files[rel])
    index = canonical_index(files)
    write_new(out / INDEX, index)                                  # LAST: the export exists only once indexed
    verify(out)
    return {**{p: sha(files[p]) for p in FILES}, INDEX: sha(index)}


def verify(out: Path) -> None:
    """Re-read the published directory as the consumer will: exactly the fixed layout (regular files,
    no links, nothing extra or missing) and an index equal to the one rebuilt from the bytes on disk."""
    out = Path(out)

    def listing(d: Path) -> set[str]:
        return {e.name for e in os.scandir(d)}

    top = {p for p in FILES if "/" not in p} | {INDEX, "delivery"}
    nested = {p.split("/", 1)[1] for p in FILES if p.startswith("delivery/")}
    if listing(out) != top or listing(out / "delivery") != nested:
        raise ExportRefused("verify_layout")
    data: dict[str, bytes] = {}
    for rel in FILES + (INDEX,):
        p = out.joinpath(*rel.split("/"))
        st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode):
            raise ExportRefused("verify_not_regular", rel)
        data[rel] = p.read_bytes()
    if not stat.S_ISDIR(os.lstat(out / "delivery").st_mode):           # a real directory, not a link
        raise ExportRefused("verify_layout")
    if data.pop(INDEX) != canonical_index(data):
        raise ExportRefused("verify_index")
    prov = json.loads(data["provenance.json"])
    if prov.get("exportVersion") != EXPORT_VERSION:
        raise ExportRefused("verify_version")
