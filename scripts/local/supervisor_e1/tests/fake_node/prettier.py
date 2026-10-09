"""A FAKE `prettier` for the supervisor formatter tests (no network, no packages). Installed by the fake npm only when
package.json names prettier. "Formatting" = strip trailing blanks on every line and end with exactly one newline.
`--write <files>` rewrites them; `--check <files>` exits 1 when any would change. Required flags: --config <json> and
--no-editorconfig. The pinned config's "mode" picks a misbehaviour: ok | nonidem (every --write appends a line, so a
second run changes the bytes) | touch (also writes src/other.txt) | fail (exit 2) | hang (sleeps)."""

import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if "--no-editorconfig" not in args or "--config" not in args:
    sys.exit(9)
cfg = args[args.index("--config") + 1]
mode = json.loads(Path(cfg).read_text(encoding="utf-8")).get("mode", "ok")
check = "--check" in args
files = [a for i, a in enumerate(args) if not a.startswith("-") and (i == 0 or args[i - 1] != "--config")]
if mode == "fail":
    print("[error] fake prettier failed")
    sys.exit(2)
if mode == "hang":
    time.sleep(300)


def fmt(text: str) -> str:
    return "\n".join(ln.rstrip() for ln in text.splitlines()).rstrip("\n") + "\n"


dirty = False
for f in files:
    p = Path(f)
    raw = p.read_bytes().decode("utf-8")
    want = fmt(raw)
    if check:
        if raw != want:
            print(f"[warn] {f}")
            dirty = True
        continue
    if mode == "nonidem":
        want += "// again\n"
    p.write_bytes(want.encode("utf-8"))
    print(f)
if mode == "touch" and not check:
    Path("src/other.txt").write_text("formatted\n", encoding="utf-8")
sys.exit(1 if dirty else 0)
