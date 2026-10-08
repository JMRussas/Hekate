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

Second declared revision (root msgs 2299/2300; HK-ISSUE-017 integration): the supplement bundle ALSO pins the
handoff module that produced it (producer.json handoffSha256 1e9036f6...). handoff.py was revised by the accepted
pending-effects fix (7c8c562, cfa1a52a...) and then by HK-ISSUE-017 (274e71f, 43f332a8...); its strict replay
therefore reports a second implementation drift. For THAT bundle only, the accepted strict problems are exactly the
two declared drifts, in the replay's own order (consumer, then handoff), with the producer handoff pin EXACTLY the
declared old value and the imported handoff EXACTLY the declared new one. The golden bundle still accepts exactly the
one consumer drift. Any other handoff hash, an omitted, extra or reordered drift line rejects.
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
from e1 import handoff as H

PROJECT = Path(__file__).resolve().parents[1]

# The ONE declared revision (plan 041, HK-ISSUE-001/002): producer pin -> reviewed revised consumer.
REVISION_FROM = "30ca084a4712560aec2975b79e5d71d0febb107bc4868a154b0cbcc2c0e61a6e"
REVISION_TO = "aea15fa421b5a857d9393dbbdff5073a977de5e28bf4a0854b35c0d1983475ed"

# The declared handoff revision (supplement bundle only): producer handoff pin -> accepted handoff.py (274e71f).
# Provenance: 1e9036f6... (frozen, plan 033) -> cfa1a52a... (pending-effects fix 7c8c562) -> 43f332a8... (HK-ISSUE-017).
HANDOFF_FROM = "1e9036f6ed29983c1282dde0a3a256094984e2c6a9a3d8940a575beeea6faab2"
HANDOFF_TO = "43f332a85b27444868dd02df7010f6f2587933c59de22a2081aa8bbba99705d0"


@dataclass(frozen=True)
class Bundle:
    name: str
    cases: int                 # the frozen number of `ok` case lines its strict replay prints
    drift_line: str            # the bundle's exact problem wording for a consumer source drift ({new}, {old})
    handoff_drift_line: str | None = None   # set only for a bundle that also pins handoffSha256 ({new}, {old})


BUNDLES = {
    "e2e-consumer-v0": Bundle("e2e-consumer-v0", 17, "PROBLEM consumer drift: imported {new} != producer.json consumerSha256 {old}"),
    "e2e-byte-compat-v0": Bundle("e2e-byte-compat-v0", 52,
                                 "PROBLEM implementation drift: e1.consumer {new} != producer consumerSha256 {old}",
                                 "PROBLEM implementation drift: e1.handoff {new} != producer handoffSha256 {old}"),
}


def evaluate(bundle: Bundle, strict_stdout: str, *, imported: str, producer_pin: str,
             imported_handoff: str | None = None, producer_handoff: str | None = None) -> list[str]:
    """[] = accepted under the declared revision(s); otherwise the reasons it is rejected."""
    reasons = []
    if producer_pin != REVISION_FROM:
        reasons.append(f"producer pin {producer_pin} is not the declared old consumer {REVISION_FROM}")
    if imported != REVISION_TO:
        reasons.append(f"imported consumer {imported} is not the declared revised consumer {REVISION_TO}")
    lines = strict_stdout.splitlines()
    problems = [ln for ln in lines if ln.startswith("PROBLEM")]
    expected = [bundle.drift_line.format(new=REVISION_TO, old=REVISION_FROM)]
    if bundle.handoff_drift_line is not None:
        if producer_handoff != HANDOFF_FROM:
            reasons.append(f"producer handoff pin {producer_handoff} is not the declared old handoff {HANDOFF_FROM}")
        if imported_handoff != HANDOFF_TO:
            reasons.append(f"imported handoff {imported_handoff} is not the declared revised handoff {HANDOFF_TO}")
        expected.append(bundle.handoff_drift_line.format(new=HANDOFF_TO, old=HANDOFF_FROM))
    if problems != expected:
        reasons.append(f"strict problems must be exactly the declared drift(s), in order; got {problems}")
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
    producer = json.loads((here / "producer.json").read_bytes())
    producer_pin = producer["consumerSha256"]
    imported_handoff = hashlib.sha256(Path(H.__file__).read_bytes()).hexdigest() if b.handoff_drift_line else None
    producer_handoff = producer.get("handoffSha256") if b.handoff_drift_line else None
    p = subprocess.run([sys.executable, str(here / "replay.py")], cwd=PROJECT, capture_output=True, text=True, timeout=600,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    reasons = evaluate(b, p.stdout, imported=imported, producer_pin=producer_pin, imported_handoff=imported_handoff,
                       producer_handoff=producer_handoff)
    if p.returncode != 1:
        reasons.append(f"strict replay exit {p.returncode}, expected 1 (its truthful FAIL)")
    return {"mode": "revised-consumer-replay", "bundle": name,
            "revision": {"from": REVISION_FROM, "to": REVISION_TO, "importedConsumer": imported, "producerPin": producer_pin},
            **({"handoffRevision": {"from": HANDOFF_FROM, "to": HANDOFF_TO, "importedHandoff": imported_handoff,
                                    "producerHandoffPin": producer_handoff}} if b.handoff_drift_line else {}),
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
