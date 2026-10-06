// H1 interop bridge (plan 023 E1b; test-only). Run with cwd = the ChatAgent checkout:
//   node --import tsx <this file>
// stdin:  {"options": <buildPlanTaskContext options>, "probe": {"getterRules": bool}?}
// stdout: exactly one JSON line. Never echoes received content in errors.
//
// Calls ChatAgent's ACTUAL buildPlanTaskContext; renders nothing itself.

import path from "node:path";
import { pathToFileURL } from "node:url";

const root = process.cwd();
const load = (rel) => import(pathToFileURL(path.join(root, rel)).href);
const out = (value) => process.stdout.write(JSON.stringify(value) + "\n");

let input = "";
for await (const chunk of process.stdin) input += chunk;

try {
  const planTask = await load("src/integrations/hekate/planTask.ts");
  const { ContextBudgetError } = await load("src/app/contextBuilder.ts");
  const { options, probe = {} } = JSON.parse(input);

  // Copy-semantics probe: each rule field is a getter that returns a different value on
  // its second read, so the package must reflect exactly one read per field.
  const reads = [];
  if (probe.getterRules) {
    options.rules = options.rules.map((rule, i) => {
      const o = {};
      for (const [k, v] of Object.entries(rule)) {
        let n = 0;
        Object.defineProperty(o, k, {
          enumerable: true,
          get() { n += 1; reads.push(`${i}.${k}`); return n === 1 ? v : `${v}-SECOND-READ`; }
        });
      }
      return o;
    });
  }

  let result;
  try {
    result = planTask.buildPlanTaskContext(options);
  } catch (e) {
    if (e instanceof planTask.PlanTaskError) out({ ok: false, kind: "PlanTaskError", code: e.code });
    else out({ ok: false, kind: "Error", code: e?.name ?? "Error" });
    process.exit(0);
  }
  if (result instanceof ContextBudgetError) {
    out({ ok: false, kind: "ContextBudgetError", code: result.code,
          estimatedInputTokens: result.estimatedInputTokens, availableInputTokens: result.availableInputTokens });
    process.exit(0);
  }

  // Frozen / copy semantics, observed on the actual result.
  const snapshot = () => JSON.stringify({ text: result.text, supplied: result.suppliedSha256, source: result.source });
  const before = snapshot();
  if (!probe.getterRules) {
    for (const rule of options.rules) { rule.text = "MUTATED"; rule.path = "MUTATED"; }
    options.rules.push({ path: "added", revision: "added", text: "added" });
  }
  let assignRejected = false;
  try { result.source.rootId = "MUTATED"; } catch { assignRejected = true; }
  const c = result.context;
  out({
    ok: true,
    text: result.text,
    suppliedSha256: result.suppliedSha256,
    source: result.source,
    context: {
      snapshotId: c.snapshotId,
      capturedAtIso: c.capturedAtIso,
      systemInstruction: c.systemInstruction,
      roleInstructions: c.roleInstructions,
      messages: c.messages.map((m) => ({ role: m.role, content: m.content })),
      memoryIsNull: c.memory === null,
      optionalCounts: [c.resolvedSources.length, c.unavailableSources.length, c.activeTasks.length,
                       c.omittedActiveTaskIds.length, c.includedTurnIds.length, c.omittedTurnIds.length],
      budgetMethod: c.budgetMethod
    },
    probes: {
      sourceFrozen: Object.isFrozen(result.source) && Object.isFrozen(result.source.supplied)
        && Object.isFrozen(result.source.supplied.rules) && result.source.supplied.rules.every(Object.isFrozen),
      assignmentRejectedOrIgnored: assignRejected || result.source.rootId !== "MUTATED",
      unchangedAfterInputMutation: before === snapshot(),
      ruleFieldReads: reads
    }
  });
} catch (e) {
  out({ ok: false, kind: "BridgeError", code: e?.name ?? "Error" });
  process.exit(3);
}
