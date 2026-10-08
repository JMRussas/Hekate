"""A FAKE `tsc --noEmit`: exits 0; `dirty` in src/value.txt leaves a file behind, `hang` never returns,
`tserror` fails, `tsdetail` fails with its failure detail EARLY and then filler (the detail is not in a tail)."""

import sys
import time
from pathlib import Path

if sys.argv[1:] != ["--noEmit"]:
    sys.exit(9)
words = Path("src/value.txt").read_text(encoding="utf-8").split()
if "dirty" in words:
    Path("leftover.tsbuildinfo").write_text("x", encoding="utf-8")
if "hang" in words:
    time.sleep(300)
if "tsdetail" in words:
    print(" FAIL  tests/unit/value.test.ts > value > keeps the old contract")
    print("AssertionError: expected 'delivery_mismatch' to be 'fresh_mismatch'")
    print("." * 3000)
    print("Tests  1 failed | 9 passed (10)")
    sys.exit(2)
sys.exit(2 if "tserror" in words else 0)
