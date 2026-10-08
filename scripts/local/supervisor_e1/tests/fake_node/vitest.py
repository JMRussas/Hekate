"""A FAKE `vitest run --reporter=json`: every tests/**/*.test.ts is JSON {"cases": [{"name", "expect"}]};
a case passes when the first word of src/value.txt equals `expect` ("*" always passes). Writes a
vitest-shaped JSON report to stdout and exits 1 when any case fails. Like real Node it ALWAYS writes a
warning to stderr (msg 1659 F1). In src/value.txt: `crash` = no report, `garbage` = the report then junk on
stdout, `extra` = one UNDECLARED passing case. Plan-run v0 (plan 042): test-file operands after the reporter
flag restrict the run to those files (as real vitest), and a case may name its own `source` file (default
src/value.txt)."""

import json
import os
import sys
from pathlib import Path

if sys.argv[1:3] != ["run", "--reporter=json"]:
    sys.exit(9)
only = [Path(a) for a in sys.argv[3:]]
sys.stderr.write("(node:4242) ExperimentalWarning: a fake Node warning on stderr\n")
sys.stderr.flush()
value = Path("src/value.txt").read_text(encoding="utf-8").split()
first = value[0] if value else ""
if "crash" in value:
    print("Error: worker crashed")
    sys.exit(1)
results, failed, passed = [], 0, 0
def first_word(source: str) -> str:
    words = Path(source).read_text(encoding="utf-8").split() if Path(source).is_file() else []
    return words[0] if words else ""


for f in sorted(Path("tests").rglob("*.test.ts")):
    if only and f not in only:
        continue
    cases = json.loads(f.read_text(encoding="utf-8"))["cases"]
    ars = []
    for c in cases:
        got = first_word(c["source"]) if "source" in c else first
        ok = c["expect"] == "*" or c["expect"] == got
        failed, passed = failed + (not ok), passed + ok
        ars.append({"fullName": c["name"], "status": "passed" if ok else "failed",
                    "failureMessages": [] if ok else [f"AssertionError: expected '{got}' to be '{c['expect']}'\n    at {f.as_posix()}:1:1"]})
    if "extra" in value and f.name == "value.test.ts":
        passed += 1
        ars.append({"fullName": "an undeclared case", "status": "passed", "failureMessages": []})
    results.append({"name": os.path.abspath(f), "message": "", "assertionResults": ars})
print(json.dumps({"numFailedTests": failed, "numPassedTests": passed, "numTotalTests": failed + passed, "testResults": results}))
if "garbage" in value:
    print("garbage after the report")
sys.exit(1 if failed else 0)
