"""E2e opt-in (plan 034 rev 3 §7): the PINNED Node runtime computes SHA-256 over the exact delivery
bytes and a py-canon.v0 corpus, and must equal Python's. Node never parses or re-encodes. Selected
with `uv run pytest interop`; the pinned checkout/runtime missing ERRORS this suite (never skips)
and never affects the default suite."""

import hashlib
import json
import subprocess
from pathlib import Path

from e1 import acts as A
from e1.h1_bridge import node_runtime

CHECK = Path(__file__).resolve().parents[1] / "e1" / "sha_check.mjs"
CORPUS = [
    A.canonical({"text": "Résumé ﬁ \U0001f600    "}).encode("utf-8"),   # combining, ligature, astral, separators
    A.canonical({"ﬁ": 2, "\U0001f600": 1}).encode("utf-8"),                                    # code-point key order (JS would differ)
    A.canonical({"c": "\x00\x1f\b\f\n\r\t\"\\"}).encode("utf-8"),                                  # controls and escapes
    A.canonical({"anchorAt": 1000.0, "neg": -0.0, "big": 2**60, "exp": 1e21}).encode("utf-8"),      # py-canon.v0 numbers (JS would re-render)
    b'{"a":1,"a":2}',                                                                               # duplicate keys: hashed as bytes, not parsed
    b"",
]


def node_sha(repo, paths):
    node, _ = node_runtime(repo)
    p = subprocess.run([str(node), str(CHECK), *map(str, paths)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_node_sha_equals_python_over_exact_bytes(h1_checkout, tmp_path):
    repo, _ = h1_checkout
    paths = []
    for i, b in enumerate(CORPUS):
        f = tmp_path / f"c{i}.bin"
        f.write_bytes(b)
        paths.append(f)
    got = node_sha(repo, paths)
    assert {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths} == got


def test_node_sha_over_a_real_e2d_delivery(h1_checkout, tmp_path):
    from test_e2d_model import make, setup, transition
    repo, _ = h1_checkout
    m, rid, _ = setup()
    t = transition(m, rid)
    p = make(m, rid, t=t, note={"text": "né \U0001f600", "author": t["fromSession"]})
    files = {"manifest": A.canonical(p.manifest).encode(), "envelope": A.canonical(p.envelope).encode()}
    paths = []
    for name, b in files.items():
        f = tmp_path / f"{name}.bin"
        f.write_bytes(b)
        paths.append(f)
    got = node_sha(repo, paths)
    assert got[str(paths[0])] == p.candidate_digest                    # Node verifies the committed digest over the same bytes
    assert got[str(paths[1])] == hashlib.sha256(files["envelope"]).hexdigest()
