"""A FAKE `tsc --noEmit`: exits 0; `dirty` in src/value.txt leaves a file behind, `hang` never returns,
`tserror` fails."""

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
sys.exit(2 if "tserror" in words else 0)
