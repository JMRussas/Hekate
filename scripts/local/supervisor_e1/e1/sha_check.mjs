// E2e opt-in interop (plan 034 rev 3 §7): SHA-256 over the EXACT bytes of each file named on the
// command line, printed as one JSON line {path: hex}. Never parses or re-encodes. Run only under
// the pinned Node runtime by interop/test_e2e_node_sha.py.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const out = {};
for (const path of process.argv.slice(2)) {
  out[path] = createHash("sha256").update(readFileSync(path)).digest("hex");
}
process.stdout.write(JSON.stringify(out) + "\n");
