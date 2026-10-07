"""Replay-verify the E2e golden consumer bundle OFFLINE (no database, no H1, no network).

  uv run python fixtures/e2e-consumer-v0/replay.py

1. Every file's SHA-256 matches INDEX.sha256 (and no unlisted file exists besides the scripts).
2. The valid and denied compositions are recomputed with the accepted consumer (e1/consumer.py) from
   the exact delivery bytes, the as-of Fresh proof, the policy/destination/wanted inputs and the
   RECORDED retrieval results and real-H1 calls (an unrecorded H1 call or retrieval is an error), and
   must reproduce the expected view-part bytes, viewDigest, reservation and H1 text byte for byte.
3. Each variant in variants.json applies its mutation and must give exactly its expected outcome.
Exit status 0 only when everything matches.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[1])]

from e1 import consumer as C            # noqa: E402
from e1.acts import canonical           # noqa: E402

SCRIPTS = {"generate.py", "replay.py", "INDEX.sha256"}


def rd(rel: str) -> bytes:
    return (HERE / rel).read_bytes()


def write_index() -> None:
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(HERE).as_posix()}"
             for p in sorted(HERE.rglob("*")) if p.is_file() and p.name != "INDEX.sha256" and "__pycache__" not in p.parts]
    (HERE / "INDEX.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def check_index() -> list[str]:
    problems, listed = [], set()
    for line in (HERE / "INDEX.sha256").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        listed.add(rel)
        if hashlib.sha256(rd(rel)).hexdigest() != digest:
            problems.append(f"hash mismatch: {rel}")
    actual = {p.relative_to(HERE).as_posix() for p in HERE.rglob("*") if p.is_file() and p.name != "INDEX.sha256"
              and "__pycache__" not in p.parts}
    problems += [f"unlisted file: {f}" for f in sorted(actual - listed)]
    return problems


def base_inputs() -> dict:
    w = json.loads(rd("delivery/wrapper.json"))
    return {"wrapper": w["wrapper"], "codec": w["codec"], "candidate_digest": w["candidateDigest"],
            "manifest": rd("delivery/manifest.bin"), "envelope": rd("delivery/envelope.bin"), "task": rd("delivery/task.bin"),
            "receipt": rd("delivery/receipt.json"), "h1Input": rd("delivery/h1-input.json"),
            "fresh": json.loads(rd("inputs/fresh.json")), "policy": json.loads(rd("inputs/policy-allow.json")),
            "request": json.loads(rd("inputs/request.json"))}


def mutate(inp: dict, m: dict) -> dict:
    """Mutation instructions: `replace` (first occurrence, on the UTF-8 text of a byte field), `raw`
    (replace the whole byte field), `set` (JSON path on fresh/policy/request/h1Input), `field` (a
    wrapper field), `policyFile` / `requestFile` (use another policy or request input)."""
    inp = copy.deepcopy(inp)
    op, target = m["op"], m.get("target")
    if op == "policyFile":
        inp["policy"] = json.loads(rd(m["file"]))
    elif op == "requestFile":
        inp["request"] = json.loads(rd(m["file"]))
    elif op == "replace":
        text = inp[target].decode("utf-8")
        assert m["find"] in text, f"mutation anchor not found in {target}: {m['find']!r}"
        inp[target] = text.replace(m["find"], m["with"], 1).encode("utf-8")
    elif op == "field":                                  # a wrapper field (wrapper / codec / candidate_digest)
        inp[target] = m["with"]
    elif op == "raw":
        inp[target] = m["with"].encode("utf-8")
    elif op == "set":
        doc = json.loads(inp[target]) if isinstance(inp[target], bytes) else inp[target]
        cur = doc
        for k in m["path"][:-1]:
            cur = cur[k]
        cur[m["path"][-1]] = m["value"]
        inp[target] = json.dumps(doc).encode("utf-8") if isinstance(inp[target], bytes) else doc
    else:
        raise ValueError(op)
    return inp


def run(inp: dict) -> C.Composition:
    retrieval = json.loads(rd("recorded/retrieval.json"))
    h1_calls = json.loads(rd("recorded/h1-calls.json"))

    def get(pointer):
        for r in retrieval:
            if r["request"] == pointer:
                return copy.deepcopy(r["result"])
        raise AssertionError(f"unrecorded retrieval {pointer}")

    def h1(options):
        for c in h1_calls:
            if canonical(c["options"]) == canonical(options):
                return copy.deepcopy(c["result"])
        raise AssertionError("unrecorded H1 call (options differ from every recorded real-H1 call)")
    d = C.Delivery(inp["wrapper"], inp["codec"], inp["candidate_digest"], inp["manifest"], inp["envelope"], inp["task"],
                   inp["receipt"], inp["h1Input"])
    p = inp["policy"]
    return C.compose(d, C.Fresh(**inp["fresh"]), policy=C.PolicyStub(p["principal"], tuple(p["rules"])),
                     destination=inp["request"]["destination"], h1=h1, wanted=inp["request"]["wanted"], retriever=get)


def check_expected(c: C.Composition, name: str) -> list[str]:
    exp = json.loads(rd(f"expected/{name}/expected.json"))
    out = []
    if c.part.encode("utf-8") != rd(f"expected/{name}/view-part.txt"):
        out.append(f"{name}: view-part bytes differ")
    got = {"viewDigest": c.view_digest, "reservationTokens": c.view["reservationTokens"], "viewCost": c.view_cost,
           "h1SuppliedSha256": c.h1["suppliedSha256"]}
    out += [f"{name}: {k} {got[k]!r} != {exp[k]!r}" for k in exp if got[k] != exp[k]]
    if c.h1["text"].encode("utf-8") != rd("expected/h1-text.txt"):
        out.append(f"{name}: H1 text differs")
    return out


def consumer_hash() -> str:
    """The sha256 of the consumer implementation ACTUALLY imported (no silent revision drift)."""
    return hashlib.sha256(Path(C.__file__).read_bytes()).hexdigest()


def main() -> int:
    problems = check_index()
    used, pinned = consumer_hash(), json.loads(rd("producer.json"))["consumerSha256"]
    print(f"consumer: {C.__file__} sha256 {used}")
    if used != pinned:
        problems.append(f"consumer drift: imported {used} != producer.json consumerSha256 {pinned}")
    for v in json.loads(rd("variants.json")):
        inp = base_inputs()
        for m in v.get("mutations", []):
            inp = mutate(inp, m)
        exp = v["expect"]
        try:
            c = run(inp)
            outcome = ("composed", None)
        except C.Refused as e:
            c, outcome = None, ("refused", e.code)
        if exp["outcome"] == "composed":
            if outcome[0] != "composed":
                problems.append(f"{v['name']}: expected composed, got refused {outcome[1]}")
            else:
                problems += check_expected(c, exp["expected"])
        elif outcome != ("refused", exp["code"]):
            problems.append(f"{v['name']}: expected refused {exp['code']}, got {outcome}")
        print(f"{'ok ' if not [p for p in problems if p.startswith(v['name'])] else 'BAD'} {v['name']}: {outcome[0]} {outcome[1] or ''}")
    for p in problems:
        print("PROBLEM", p)
    print("REPLAY", "PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
