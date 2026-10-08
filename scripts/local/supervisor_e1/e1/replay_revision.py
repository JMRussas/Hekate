"""Revised-consumer replay check (plan 041 §4a, root msg 1748). OUT OF BUNDLE; offline; no model.

The frozen golden (`fixtures/e2e-consumer-v0`) and supplement (`fixtures/e2e-byte-compat-v0`) bundles pin the
consumer that PRODUCED them (`30ca084a…`) in their `producer.json`, and their own strict replays fail on any other
imported consumer. That strict result stays TRUTHFUL and is reported as is: this module never edits a bundle,
never relabels the strict FAIL as PASS, and has no generic "ignore drift" switch.

It runs each bundle's own strict `replay.py` unchanged and accepts its output ONLY when:
- the bundle's producer pin is EXACTLY the declared old consumer and the imported consumer is EXACTLY the declared
  new one (the one reviewed revision; full sha256 values);
- the strict output's ONLY problem is that one source drift, in the bundle's exact wording (so index, provenance,
  handoff and every case check still run and must be clean);
- every case is `ok`, the count equals the bundle's frozen case count, and nothing is `BAD`.
Anything else (another problem, an unexpected count, another hash) rejects.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from e1 import consumer as C

PROJECT = Path(__file__).resolve().parents[1]

# The ONE declared revision (plan 041, HK-ISSUE-001/002): producer pin -> reviewed revised consumer.
REVISION_FROM = "30ca084a4712560aec2975b79e5d71d0febb107bc4868a154b0cbcc2c0e61a6e"
REVISION_TO = "aea15fa421b5a857d9393dbbdff5073a977de5e28bf4a0854b35c0d1983475ed"


@dataclass(frozen=True)
class Bundle:
    name: str
    cases: int                 # the frozen number of `ok` case lines its strict replay prints
    drift_line: str            # the bundle's exact problem wording for a consumer source drift ({new}, {old})


BUNDLES = {
    "e2e-consumer-v0": Bundle("e2e-consumer-v0", 17, "PROBLEM consumer drift: imported {new} != producer.json consumerSha256 {old}"),
    "e2e-byte-compat-v0": Bundle("e2e-byte-compat-v0", 52,
                                 "PROBLEM implementation drift: e1.consumer {new} != producer consumerSha256 {old}"),
}


def evaluate(bundle: Bundle, strict_stdout: str, *, imported: str, producer_pin: str) -> list[str]:
    """[] = accepted under the declared revision; otherwise the reasons it is rejected."""
    reasons = []
    if producer_pin != REVISION_FROM:
        reasons.append(f"producer pin {producer_pin} is not the declared old consumer {REVISION_FROM}")
    if imported != REVISION_TO:
        reasons.append(f"imported consumer {imported} is not the declared revised consumer {REVISION_TO}")
    lines = strict_stdout.splitlines()
    problems = [ln for ln in lines if ln.startswith("PROBLEM")]
    expected = bundle.drift_line.format(new=REVISION_TO, old=REVISION_FROM)
    if problems != [expected]:
        reasons.append(f"strict problems must be exactly the one declared drift; got {problems}")
    ok = sum(1 for ln in lines if ln.startswith("ok "))
    bad = sum(1 for ln in lines if ln.startswith("BAD"))
    if ok != bundle.cases or bad:
        reasons.append(f"cases: {ok} ok / {bad} BAD, expected {bundle.cases} ok / 0 BAD")
    if [ln for ln in lines if ln.startswith("REPLAY")] != ["REPLAY FAIL"]:
        reasons.append("the strict replay's own verdict must be its truthful REPLAY FAIL (the declared drift)")
    return reasons


def run(name: str) -> dict:
    """Run the bundle's strict replay UNCHANGED and evaluate it. Never writes into the bundle."""
    b = BUNDLES[name]
    here = PROJECT / "fixtures" / name
    imported = hashlib.sha256(Path(C.__file__).read_bytes()).hexdigest()
    producer_pin = json.loads((here / "producer.json").read_bytes())["consumerSha256"]
    p = subprocess.run([sys.executable, str(here / "replay.py")], cwd=PROJECT, capture_output=True, text=True, timeout=600,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    reasons = evaluate(b, p.stdout, imported=imported, producer_pin=producer_pin)
    if p.returncode != 1:
        reasons.append(f"strict replay exit {p.returncode}, expected 1 (its truthful FAIL)")
    return {"mode": "revised-consumer-replay", "bundle": name,
            "revision": {"from": REVISION_FROM, "to": REVISION_TO, "importedConsumer": imported, "producerPin": producer_pin},
            "strict": {"exit": p.returncode, "verdict": "FAIL" if "REPLAY FAIL" in p.stdout else "PASS" if "REPLAY PASS" in p.stdout else "?",
                       "stdoutSha256": hashlib.sha256(p.stdout.encode("utf-8")).hexdigest(),
                       "problems": [ln for ln in p.stdout.splitlines() if ln.startswith("PROBLEM")]},
            "cases": {"ok": sum(1 for ln in p.stdout.splitlines() if ln.startswith("ok ")), "expected": b.cases},
            "revisedResult": "PASS" if not reasons else "FAIL", "reasons": reasons}


def main(argv: list[str]) -> int:
    names = argv or list(BUNDLES)
    results = [run(n) for n in names]
    for r in results:
        print(json.dumps(r, indent=1))
    ok = all(r["revisedResult"] == "PASS" for r in results)
    print("REVISED-CONSUMER REPLAY", "PASS" if ok else "FAIL", "(strict replays: FAIL as expected, the declared source drift only)"
          if ok else "")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
