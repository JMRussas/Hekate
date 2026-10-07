"""A FAKE npm CLI for the operator task runner tests (no network, no packages). `ci` copies the fake
vitest and tsc entries into node_modules/ from this directory; anything else is refused. The lockfile
itself is hash-checked by the runner BEFORE this runs."""

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
args = sys.argv[1:]
if not args or args[0] != "ci" or "--ignore-scripts" not in args:
    sys.exit(9)
if Path("node_modules").exists():
    sys.exit(8)                                   # real npm ci would remove it; the runner demands pristine anyway
for rel, src in (("node_modules/vitest/vitest.mjs", "vitest.py"), ("node_modules/typescript/bin/tsc", "tsc.py")):
    Path(rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / src, rel)
print("added 2 packages in 0s")
