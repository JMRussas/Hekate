"""Replay-verify the E2e byte-compatibility supplement OFFLINE (no database, no H1, no network).

  uv run python fixtures/e2e-byte-compat-v0/replay.py                     # Python only
  uv run python fixtures/e2e-byte-compat-v0/replay.py --node <checkout>   # + optional pinned-Node SHA-256

1. INDEX.sha256 covers every file exactly; the imported consumer/handoff implementations match the
   producer hashes (no silent drift).
2. vectors/: each vector's SHA-256, the strict reader's TYPED outcome (stage 1), and whether the bytes
   are exactly py-canon.v0 canonical (stage 2). A byte hash alone never promotes a value.
3. deliveries/: each VALID delivery is recomposed (accepted consumer, H1-SHAPED stub, synthetic as-of
   proof) and must reproduce its expected view bytes, viewDigest and reservation; variants.json
   mutations must give exactly their expected typed outcome.
4. --node: the PINNED Node (the H1 checkout's runtime) recomputes SHA-256 over every file's exact
   bytes. This proves byte-hash parity only, NOT JavaScript semantic parity.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.path[:0] = [str(PROJECT)]

from e1 import acts as A                  # noqa: E402
from e1 import consumer as C              # noqa: E402
from e1 import handoff as H               # noqa: E402

ALLOW = C.PolicyStub("lead-1", ({"id": "read-all", "action": "source_read", "match": {}, "allow": True},
                                {"id": "use-all", "action": "destination_use", "match": {}, "allow": True}))


def rd(rel: str) -> bytes:
    return (HERE / rel).read_bytes()


def files() -> list[Path]:
    return [p for p in sorted(HERE.rglob("*")) if p.is_file() and p.name != "INDEX.sha256" and "__pycache__" not in p.parts]


def write_index() -> None:
    (HERE / "INDEX.sha256").write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(HERE).as_posix()}\n"
                                               for p in files()), encoding="utf-8", newline="\n")


def check_index() -> list[str]:
    problems, listed = [], set()
    for line in (HERE / "INDEX.sha256").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        listed.add(rel)
        if hashlib.sha256(rd(rel)).hexdigest() != digest:
            problems.append(f"hash mismatch: {rel}")
    problems += [f"unlisted file: {f}" for f in sorted({p.relative_to(HERE).as_posix() for p in files()} - listed)]
    return problems


def check_impl() -> list[str]:
    prod = json.loads(rd("producer.json"))
    out = []
    for mod, key in ((C, "consumerSha256"), (H, "handoffSha256")):
        got = hashlib.sha256(Path(mod.__file__).read_bytes()).hexdigest()
        print(f"implementation: {mod.__file__} sha256 {got}")
        if got != prod[key]:
            out.append(f"implementation drift: {mod.__name__} {got} != producer {key} {prod[key]}")
    return out


def check_vectors() -> list[str]:
    out = []
    for name, exp in json.loads(rd("vectors/expected.json")).items():
        raw = rd(f"vectors/{name}.bin")
        try:
            C.strict_loads(raw, "vector")
            strict = "ok"
        except C.Refused as e:
            strict = e.code
        try:
            canon = A.canonical(C.strict_loads(raw, "vector")).encode("utf-8") == raw
        except (C.Refused, UnicodeEncodeError):
            canon = None
        got = {"sha256": hashlib.sha256(raw).hexdigest(), "strict": strict, "pyCanonV0Canonical": canon}
        bad = [k for k in got if got[k] != exp[k]]
        print(f"{'ok ' if not bad else 'BAD'} vector {name}: strict={strict} canonical={canon}")
        out += [f"vector {name}: {k} {got[k]!r} != {exp[k]!r}" for k in bad]
    return out


def inputs(case: str) -> dict:
    base = f"deliveries/{case}"
    w = json.loads(rd(f"{base}/wrapper.json"))
    return {"wrapper": w["wrapper"], "codec": w["codec"], "candidate_digest": w["candidateDigest"],
            **{k: rd(f"{base}/{f}") for k, f in (("manifest", "manifest.bin"), ("envelope", "envelope.bin"), ("task", "task.bin"),
                                                  ("receipt", "receipt.json"), ("h1Input", "h1-input.json"))},
            "fresh": json.loads(rd(f"{base}/fresh.json")), "request": json.loads(rd(f"{base}/request.json"))}


def mutate(inp: dict, m: dict) -> dict:
    """`replace` (first occurrence in a byte field's UTF-8 text), `redigest` (recompute the candidate
    digest over the CURRENT manifest bytes in the wrapper, receipt and as-of proof, so the raw
    exact-byte digest stage passes; a later stage then refuses: the canonical-form check for a
    LEXICAL change, or a cross-field consistency check for a VALUE change), `file` (replace h1Input)."""
    inp = copy.deepcopy(inp)
    if m["op"] == "replace":
        text = inp[m["target"]].decode("utf-8")
        assert m["find"] in text, f"anchor not found in {m['target']}: {m['find']!r}"
        inp[m["target"]] = text.replace(m["find"], m["with"], 1).encode("utf-8")
    elif m["op"] == "redigest":
        dg = hashlib.sha256(inp["manifest"]).hexdigest()
        inp["candidate_digest"] = dg
        r = json.loads(inp["receipt"])
        r["candidateDigest"] = dg
        inp["receipt"] = json.dumps(r).encode("utf-8")
        inp["fresh"]["candidate_digest"] = dg
    elif m["op"] == "file":
        inp[m["target"]] = rd(m["path"])
    else:
        raise ValueError(m["op"])
    return inp


def compose(inp: dict) -> C.Composition:
    d = C.Delivery(inp["wrapper"], inp["codec"], inp["candidate_digest"], inp["manifest"], inp["envelope"], inp["task"],
                   inp["receipt"], inp["h1Input"])
    text = json.loads(inp["task"])["text"]
    return C.compose(d, C.Fresh(**inp["fresh"]), policy=ALLOW, destination=inp["request"]["destination"], h1=C.h1_stub(text),
                     wanted=inp["request"]["wanted"])


def check_expected(c: C.Composition, rel: str) -> list[str]:
    exp = json.loads(rd(f"{rel}/expected.json"))
    out = [] if c.part.encode("utf-8") == rd(f"{rel}/view-part.txt") else [f"{rel}: view-part bytes differ"]
    got = {"viewDigest": c.view_digest, "reservationTokens": c.view["reservationTokens"], "viewCost": c.view_cost,
           "h1SuppliedSha256": c.h1["suppliedSha256"]}
    return out + [f"{rel}: {k} {got[k]!r} != {exp[k]!r}" for k in exp if got[k] != exp[k]]


TARGET_FIELD = {"manifest": "manifest", "envelope": "envelope", "task": "task"}


def decoded_identity(raw: bytes) -> str:
    """The py-canon.v0 encoding of the DECODED value: equal iff the decoded values are identical,
    including the float/int distinction and -0.0 vs 0 (plain == would equate them)."""
    return A.canonical(C.strict_loads(raw, "variant"))


def check_class(v: dict, before: dict, after: dict) -> list[str]:
    """Independent assertion of the variant's class on its mutated field:
    - lexical: the decoded value is IDENTICAL and only the bytes are not canonical;
    - value-change: the decoded value differs (a re-encoding that changes the number or the text)."""
    cls = v.get("class")
    if cls is None:
        return []
    field = TARGET_FIELD[v["mutations"][0]["target"]]
    same = decoded_identity(before[field]) == decoded_identity(after[field])
    noncanonical = A.canonical(C.strict_loads(after[field], "variant")).encode("utf-8") != after[field]
    if cls == "lexical" and not (same and noncanonical):
        return [f"{v['name']}: not lexical (sameValue={same}, nonCanonicalBytes={noncanonical})"]
    if cls == "value-change" and same:
        return [f"{v['name']}: not a value change (decoded value identical)"]
    return []


def check_deliveries() -> list[str]:
    out = []
    for v in json.loads(rd("variants.json")):
        inp = inputs(v["delivery"])
        before = copy.deepcopy(inp)
        for m in v.get("mutations", []):
            inp = mutate(inp, m)
        out += check_class(v, before, inp)
        try:
            c, got = compose(inp), ("composed", None)
        except C.Refused as e:
            c, got = None, ("refused", e.code)
        exp = v["expect"]
        if exp["outcome"] == "composed":
            out += ([f"{v['name']}: expected composed, got {got}"] if c is None else check_expected(c, exp["expected"]))
        elif got != ("refused", exp["code"]):
            out.append(f"{v['name']}: expected refused {exp['code']}, got {got}")
        print(f"{'ok ' if not [p for p in out if p.startswith(v['name'])] else 'BAD'} {v['name']}: {got[0]} {got[1] or ''}")
    return out


def check_node(checkout: str) -> list[str]:
    sys.path.insert(0, str(PROJECT))
    from e1.h1_bridge import node_runtime   # the pinned runtime of the given checkout, never PATH
    node, version = node_runtime(Path(checkout))
    paths = files() + [HERE / "INDEX.sha256"]
    p = subprocess.run([str(node), str(PROJECT / "e1" / "sha_check.mjs"), *map(str, paths)], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        return [f"node failed: {p.stderr.strip()[-200:]}"]
    got = json.loads(p.stdout)
    bad = [str(x.relative_to(HERE)) for x in paths if got.get(str(x)) != hashlib.sha256(x.read_bytes()).hexdigest()]
    print(f"node {version}: sha256 over {len(paths)} files, {'all equal' if not bad else 'MISMATCH'} (byte-hash parity only)")
    return [f"node sha mismatch: {b}" for b in bad]


def main(argv: list[str]) -> int:
    problems = check_index() + check_impl() + check_vectors() + check_deliveries()
    if argv[:1] == ["--node"]:
        if len(argv) < 2:
            problems.append("--node needs the pinned checkout path")
        else:
            problems += check_node(argv[1])
    for p in problems:
        print("PROBLEM", p)
    print("REPLAY", "PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
