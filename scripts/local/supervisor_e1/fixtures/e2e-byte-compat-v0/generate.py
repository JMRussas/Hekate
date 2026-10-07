"""Generate the E2e byte-compatibility supplement (offline; no database, no H1, no network).

  uv run python fixtures/e2e-byte-compat-v0/generate.py

Part 1, vectors/: GENERIC JSON byte vectors. They are NOT deliveries and NOT producer output; each
has its exact bytes, SHA-256, the accepted strict reader's typed outcome, and whether the v0 producer
can reach that value class at all.
Part 2, deliveries/: VALID, producer-reachable deliveries built by the ACCEPTED producer code (the
E2d model path + handoff.build) with a SYNTHETIC receipt and as-of proof consistent with the manifest
(not DB-committed), composed by the ACCEPTED consumer with the H1-SHAPED stub (not ChatAgent's real H1).
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path[:0] = [str(PROJECT), str(PROJECT / "tests"), str(HERE)]

from e1 import acts as A                       # noqa: E402
from e1 import consumer as C                   # noqa: E402
from e1 import handoff as H                    # noqa: E402

# ------------------------------------------------------------------------------------------------ vectors
# (name, raw bytes, producer-reachable?, note)
VECTORS = [
    ("utf8-nonascii-values", A.canonical({"t": "Résumé ﬁ مرحبا 中文"}).encode(), True,
     "combining marks, a ligature, RTL Arabic, CJK as raw UTF-8 values"),
    ("utf8-astral-and-separators", A.canonical({"t": "\U0001f600 \U0001d11e    "}).encode(), True,
     "astral (4-byte UTF-8) and U+2028/U+2029 raw (neither Python nor JS escapes them)"),
    ("controls-escaped", A.canonical({"t": "a\tb\nc\x00d\x1fe\"f\\g"}).encode(), True,
     "controls as short escapes or lowercase \\u00xx; quote and backslash escaped"),
    ("nonascii-keys-codepoint-order", A.canonical({"ﬁ": 2, "\U0001f600": 1}).encode(), False,
     "parses, but the v0 producer never emits non-ASCII keys; code-point order differs from JS UTF-16 order"),
    ("float-dot-zero", b'{"at":1000.0}', True, "Python renders 1000.0; JS JSON.stringify renders 1000"),
    ("float-negative-zero", b'{"at":-0.0}', True, "reachable fake-clock value; JS renders 0"),
    ("float-small-exponent", b'{"at":1e-05}', True, "reachable fake-clock value; JS renders 0.00001"),
    ("float-subnormal", b'{"at":5e-324}', True, "the smallest subnormal double; a reachable fake-clock value (0 <= v, repr fits)"),
    ("float-large-fraction", b'{"at":999999999999.5}', True, "reachable fake-clock value near MAX_AT"),
    # Valid JSON that is NOT py-canon.v0 canonical: the strict reader accepts it, but a manifest in this
    # form can never match its committed digest (verify_stored re-canonicalizes and compares).
    ("noncanonical-exponent-int-like", b'{"at":1e0}', False, "valid JSON; py-canon.v0 would write 1.0"),
    ("noncanonical-exponent-form", b'{"at":1.1e3}', False, "valid JSON; py-canon.v0 would write 1100.0"),
    ("noncanonical-trailing-zero", b'{"at":1100.00}', False, "valid JSON; py-canon.v0 would write 1100.0"),
    ("noncanonical-whitespace", b'{ "a": 1 }', False, "valid JSON; py-canon.v0 has no whitespace"),
    ("noncanonical-key-order", b'{"b":1,"a":2}', False, "valid JSON; py-canon.v0 sorts keys"),
    ("noncanonical-escaped-nonascii", b'{"t":"\\u00e9"}', False, "valid JSON; py-canon.v0 writes raw UTF-8, not a \\u00e9 escape"),
    ("noncanonical-escaped-slash", b'{"t":"a\\/b"}', False, "valid JSON; py-canon.v0 never escapes '/'"),
    ("noncanonical-negative-zero-int", b'{"a":-0}', False, "valid JSON; parses to the int 0, which py-canon.v0 writes as 0"),
    ("escaped-duplicate-key", b'{"a":1,"\\u0061":2}', False, "the same key written raw and escaped: a duplicate after decoding; refused"),
    ("nfc-precomposed", A.canonical({"t": "é"}).encode(), True, "U+00E9 (NFC); NO normalization: a different hash from the NFD vector"),
    ("nfd-decomposed", A.canonical({"t": "é"}).encode(), True, "e + U+0301 (NFD); NO normalization: a different hash from the NFC vector"),
    ("float-large-exponent", b'{"x":1e+21}', False, "exponent >= 1e16 is not reachable (clock <= 1e12)"),
    ("int-2^53-1", b'{"n":9007199254740991}', True, "the largest integer a JS Number holds exactly"),
    ("int-2^53+1", b'{"n":9007199254740993}', True,
     "reachable only through an unbounded PlanStore event_seq in the manifest basis; a JS Number parse rounds it"),
    ("int-2^63-1", b'{"n":9223372036854775807}', True, "Postgres bigint maximum; a JS Number parse rounds it"),
    ("duplicate-keys", b'{"a":1,"a":2}', False, "never produced by canonical output; refused by the strict reader"),
    ("nan", b'{"a":NaN}', False, "not JSON; refused"),
    ("infinity", b'{"a":Infinity}', False, "not JSON; refused"),
    ("overflowing-number", b'{"a":1e999}', False, "parses to infinity in Python; refused as non-finite"),
    ("lone-surrogate-escape", b'{"a":"\\ud800"}', False, "a lone high surrogate written as an escape; refused"),
    ("lone-surrogate-key", b'{"\\udc00":1}', False, "a lone low surrogate as a key; refused"),
    ("invalid-utf8-byte", b'{"a":"\xff"}', False, "not UTF-8; refused"),
    ("overlong-utf8", b'{"a":"\xc0\x80"}', False, "an overlong encoding of U+0000; refused as invalid UTF-8"),
    ("utf8-encoded-surrogate", b'{"a":"\xed\xa0\x80"}', False, "a surrogate encoded as UTF-8 (CESU-style); refused as invalid UTF-8"),
    ("bom-prefix", b'\xef\xbb\xbf{"a":1}', False, "a UTF-8 BOM before the JSON; refused (the producer never writes one)"),
]


def canonical_form(raw: bytes) -> bool | None:
    """True when the bytes are exactly py-canon.v0 output for their parsed value; None if not parseable."""
    try:
        return A.canonical(C.strict_loads(raw, "vector")).encode("utf-8") == raw
    except (C.Refused, UnicodeEncodeError):
        return None


def strict_outcome(raw: bytes) -> dict:
    try:
        C.strict_loads(raw, "vector")
        return {"strict": "ok"}
    except C.Refused as e:
        return {"strict": e.code}


# ------------------------------------------------------------------------------------------------ deliveries
def build_delivery(name: str, *, times: dict, task_text: str, instructions: dict, note: str | None, imports: list, event_seq: int | None):
    from test_e2c_model import LS1, LS2, ROOT, act, done, ev, package, rkey
    from test_e2d_model import DEST_CONV, PINS, evidence_of, pending_of, source_store
    m = A.ActsModel(ROOT, "ck-1", now=times["request"])
    basis = {"tx": "synthetic-route-a"} | ({"eventSeq": event_seq} if event_seq is not None else {})
    facts = A.PlanFacts("A", done(), revision=3, basis=basis)
    assert m.request_review(rkey(LS1), facts).outcome == "accepted"
    rid = A.review_id(rkey(LS1))
    m.now = times["ack"]
    assert m.intake(act("review_acknowledged", rkey(LS1), 1), facts).outcome == "accepted"
    m.now = times["progress"]
    assert m.intake(act("review_progress", rkey(LS1), 2, 1, ev(1)), facts).outcome == "accepted"
    req = {"attemptKey": {k: rkey(LS1)[k] for k in A.ATTEMPT_FIELDS}, "artifactRef": "sha-art"}
    cb = m.read(req, facts)
    pred = m.view().chain(rid).current
    prep_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "e2e-byte-compat-v0/" + name))
    t = dict(H.handoff_ids(ROOT, "ck-1", rid, pred.link_id, LS2, prep_id), prepareId=prep_id, root=ROOT, claimKey="ck-1",
             option="review_rebind", gate="operator", gateRef="recon/compat-slash", predecessorBindingId=pred.link_id, fromSession=LS1,
             targetSession=LS2, conversationRef="conv-compat-successor")
    store = source_store([text for text, _ in imports])
    # source_store gives alternating provenance; build our own to name each record's provenance explicitly
    store = {k: dict(v, provenance=imports[i][1]) for i, (k, v) in enumerate(sorted(store.items(), key=lambda kv: int(kv[0][1][1:])))}
    from test_e2d_model import SRC_CONV, imp
    verified = H.verify_imports([imp(i, text) for i, (text, _) in enumerate(imports)], store, "lead-1",
                                lambda p, k, tg: f"compat-stub:{k}:{p}", DEST_CONV) if imports else []
    task = {"text": task_text, "instructions": instructions, "packageRef": package()}
    pkg = H.build(role="lead", cb=cb, planstore_class="candidate", pins=PINS, basis_check=[cb["basis"]], pending=pending_of(m),
                  evidence=evidence_of(m, rid), task=task, transition=t, note={"text": note, "author": LS1} if note else None,
                  imports=verified)
    receipt = {"handoffId": t["handoffId"], "candidateDigest": pkg.candidate_digest, "bindingLinkId": t["linkId"],
               "recordId": t["recordId"], "seq": len(m.records()) + 1, "recordHash": H.sha("synthetic-record:" + name)}
    h1_in = {"response": "{\"synthetic\":true}", "rules": [], "systemInstruction": instructions["system"],
             "roleInstructions": {"fast": instructions["fast"], "deep": instructions["deep"]},
             "budget": {"windowTokens": 200000, "maxHistoryTurns": 0, "safetyTokens": 0, "fastOutputTokens": 1000, "deepOutputTokens": 1000},
             "capturedAtIso": "2026-10-07T12:00:00Z"}
    files = {"manifest.bin": A.canonical(pkg.manifest).encode("utf-8"), "envelope.bin": A.canonical(pkg.envelope).encode("utf-8"),
             "task.bin": A.canonical(task).encode("utf-8"), "receipt.json": json.dumps(receipt).encode("utf-8"),
             "h1-input.json": json.dumps(h1_in, ensure_ascii=False).encode("utf-8")}
    fresh = {"candidate_digest": pkg.candidate_digest, "record_id": t["recordId"], "review_identity": C.review_identity(pkg.manifest),
             "package_ref": task["packageRef"], "receipt_status": "current", "current_binding": t["linkId"], "review_class": "candidate",
             "pins_problem": None, "pending": pkg.manifest["authority"]["pending"], "queue": pkg.manifest["authority"]["queue"],
             "basis": {"synthetic": "as-of proof consistent with the manifest; not a database read"}}
    d = C.Delivery(C.WRAPPER, C.CODEC, pkg.candidate_digest, *(files[k] for k in ("manifest.bin", "envelope.bin", "task.bin", "receipt.json",
                                                                                  "h1-input.json")))
    policy = C.PolicyStub("lead-1", ({"id": "read-all", "action": "source_read", "match": {}, "allow": True},
                                     {"id": "use-all", "action": "destination_use", "match": {}, "allow": True}))
    c = C.compose(d, C.Fresh(**fresh), policy=policy, destination=DEST_CONV, h1=C.h1_stub(task_text))
    return files, fresh, {"destination": DEST_CONV, "wanted": []}, c


CASES = {
    "unicode-rich": dict(
        times={"request": 100.0, "ack": 200.0, "progress": 300.0},
        task_text="Review the résumé parser.\n\tInputs: ﬁle names, emoji \U0001f600, RTL مرحبا, "
                  "CJK 中文, combining é, separator   end.",
        instructions={"system": "SYSTEM ü", "fast": "FAST \U0001f680", "deep": "DEEP 中"},
        note="Handover \U0001f91d: the CJK import below is user-stated; the emoji one is only a claim.",
        imports=[("请保持公共 API 不变。", "user-stated"),
                 ("I fixed the \U0001f41b in retries ✅", "assistant-claimed")],
        event_seq=None),
    "clock-negative-zero-and-small-exponent": dict(
        times={"request": 5.0, "ack": -0.0, "progress": 1e-05},
        task_text="Clock edge case: the ACK is at -0.0 and the progress at 1e-05.", instructions={"system": "S", "fast": "F", "deep": "D"},
        note=None, imports=[], event_seq=None),
    "clock-subnormal": dict(
        times={"request": 5.0, "ack": 1.0, "progress": 5e-324},
        task_text="Clock edge case: progress at the smallest subnormal 5e-324.", instructions={"system": "S", "fast": "F", "deep": "D"},
        note=None, imports=[], event_seq=None),
    "clock-large-fraction": dict(
        times={"request": 5.0, "ack": 0.5, "progress": 999999999999.5},
        task_text="Clock edge case: progress at 999999999999.5.", instructions={"system": "S", "fast": "F", "deep": "D"},
        note=None, imports=[], event_seq=None),
    "eventseq-beyond-2^53": dict(
        times={"request": 100.0, "ack": 200.0, "progress": 300.0},
        task_text="Basis eventSeq beyond 2^53.", instructions={"system": "S", "fast": "F", "deep": "D"},
        note=None, imports=[], event_seq=2**53 + 1),
}


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def jbytes(obj) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def main() -> None:
    expected = {}
    for name, raw, reachable, note in VECTORS:
        write(HERE / "vectors" / f"{name}.bin", raw)
        expected[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), **strict_outcome(raw),
                          "pyCanonV0Canonical": canonical_form(raw), "producerReachable": reachable, "note": note}
    write(HERE / "vectors" / "expected.json", jbytes(expected))
    for name, spec in CASES.items():
        files, fresh, request, c = build_delivery(name, **spec)
        base = HERE / "deliveries" / name
        for fname, data in files.items():
            write(base / fname, data)
        write(base / "wrapper.json", jbytes({"wrapper": C.WRAPPER, "codec": C.CODEC, "candidateDigest": c.view["candidateDigest"]}))
        write(base / "fresh.json", jbytes(fresh))
        write(base / "request.json", jbytes(request))
        write(base / "expected" / "view-part.txt", c.part.encode("utf-8"))
        write(base / "expected" / "expected.json", jbytes({"viewDigest": c.view_digest, "reservationTokens": c.view["reservationTokens"],
                                                           "viewCost": c.view_cost, "h1SuppliedSha256": c.h1["suppliedSha256"]}))
        if name == "eventseq-beyond-2^53":
            # Budget arithmetic: each input is a safe integer, but window - reservation - outputs is
            # computed exactly here; a host must not do it in lossy floating point.
            big = json.loads(files["h1-input.json"])
            big["budget"] = dict(big["budget"], windowTokens=2**53 - 1)
            d2 = C.Delivery(C.WRAPPER, C.CODEC, c.view["candidateDigest"], files["manifest.bin"], files["envelope.bin"], files["task.bin"],
                            files["receipt.json"], json.dumps(big).encode("utf-8"))
            from test_e2d_model import DEST_CONV
            policy = C.PolicyStub("lead-1", ({"id": "read-all", "action": "source_read", "match": {}, "allow": True},
                                             {"id": "use-all", "action": "destination_use", "match": {}, "allow": True}))
            c2 = C.compose(d2, C.Fresh(**fresh), policy=policy, destination=DEST_CONV, h1=C.h1_stub(json.loads(files["task.bin"])["text"]))
            write(base / "expected-max-window" / "h1-input.json", json.dumps(big).encode("utf-8"))
            write(base / "expected-max-window" / "view-part.txt", c2.part.encode("utf-8"))
            write(base / "expected-max-window" / "expected.json", jbytes({"viewDigest": c2.view_digest,
                                                                       "reservationTokens": c2.view["reservationTokens"],
                                                                       "viewCost": c2.view_cost, "h1SuppliedSha256": c2.h1["suppliedSha256"]}))
    write(HERE / "producer.json", jbytes({
        "kind": "e2e-byte-compat-v0 (supplement to the accepted e2e-consumer-v0 golden bundle)",
        "plan": "034 rev 3 (17273a5194ccec681a7b9eca7089db84cf20fe3489e64f3729130059cd2ab9db)",
        "consumerSha256": hashlib.sha256((PROJECT / "e1" / "consumer.py").read_bytes()).hexdigest(),
        "handoffSha256": hashlib.sha256((PROJECT / "e1" / "handoff.py").read_bytes()).hexdigest(),
        "deliveries": "accepted producer code on the E2d MODEL path; synthetic receipt and as-of proof; H1-SHAPED stub; offline",
        "codec": C.CODEC, "wrapper": C.WRAPPER, "generatedAt": "2026-10-07"}))
    import replay                              # noqa: E402
    replay.write_index()
    sys.exit(replay.main([]))


if __name__ == "__main__":
    main()
