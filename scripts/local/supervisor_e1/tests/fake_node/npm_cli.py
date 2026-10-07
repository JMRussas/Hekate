"""A FAKE npm CLI for the operator task runner tests (no network, no packages). `ci` copies the fake
vitest and tsc entries into node_modules/ from this directory; anything else is refused. The lockfile
itself is hash-checked by the runner BEFORE this runs. Like real npm with the runner's scoped env: it
requires npm_config_userconfig/globalconfig to be empty files, and --offline needs npm_config_cache/_cacache."""

import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
args = sys.argv[1:]
if not args or args[0] != "ci" or "--ignore-scripts" not in args:
    sys.exit(9)
for var in ("npm_config_userconfig", "npm_config_globalconfig"):           # the runner's owned EMPTY config only
    cfg = os.environ.get(var)
    if not cfg or not Path(cfg).is_file() or Path(cfg).stat().st_size != 0:
        print(f"npm error fake: {var} is not an empty config file: {cfg!r}")
        sys.exit(7)
cache = os.environ.get("npm_config_cache")
if "--offline" in args and not (cache and (Path(cache) / "_cacache").is_dir()):
    print("npm error code ENOTCACHED")                                       # what a cold cache does to a real offline ci
    print("npm error request to https://registry.npmjs.org/fx/-/fx-1.0.0.tgz failed: cache mode is 'only-if-cached' "
          "but no cached response is available.")
    sys.exit(1)
if Path("node_modules").exists():
    sys.exit(8)                                   # real npm ci would remove it; the runner demands pristine anyway
for rel, src in (("node_modules/vitest/vitest.mjs", "vitest.py"), ("node_modules/typescript/bin/tsc", "tsc.py")):
    Path(rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / src, rel)
print("added 2 packages in 0s")
