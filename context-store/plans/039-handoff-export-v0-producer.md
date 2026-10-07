# Plan 039 — handoff-export.v0 producer contract (Hekate side)

**Status: implemented (revision 1, 2026-10-07).** Producer source freeze 2 was accepted by independent review (msg 1600) and the root's focused tests (msg 1602). Committed as `1af9a9e8c20216c05ea43a521277b32cc5dff3d9` on `feat/handoff-export-v0`. Clean-checkout validation: full default suite 807 passed; both replays pass. **No real export has been produced yet**; the one opt-in real-CLI + real-H1 export run needs a separate root GO.

This note records, on the Hekate side, the export contract shared with ChatAgent. It was frozen in bridge msgs 1549/1550 (proposal 1532), with the expectation schema from msgs 1515/1524 and the provenance fields from msg 1577. The consumer is ChatAgent `2383e85` (`src/integrations/hekate/handoffConsumer/cli.ts`, `readExport`). No historical document is changed.

## 1. Layout (exactly 13 files plus the index)

```
delivery/wrapper.json  delivery/manifest.bin  delivery/envelope.bin  delivery/task.bin
delivery/receipt.json  delivery/h1-input.json
fresh.json  policy.json  request.json  retrieval.json  expected.json  view-part.txt  provenance.json
INDEX.sha256
```

- `INDEX.sha256` is written **last** and excludes itself. One line per file, sorted by its fixed relative path: `<lowercase sha256>␠␠<path>\n`.
- The output directory must not exist. Every file is created with `O_EXCL`; nothing is ever overwritten.
- Each file is checked against the consumer's read cap **before** anything is written: wrapper/receipt/expected 4 KiB, other JSON 1 MiB, provenance 64 KiB, h1-input as in `delivery.ts`, the index 4 KiB. Any JSON float is refused (the consumer refuses floats).
- After writing, `verify()` re-reads the directory as the consumer does. It requires the exact layout (regular files, `delivery` a real directory, nothing extra or missing) and an index equal to the one rebuilt from the bytes on disk.

## 2. Contents

| File | Content |
|---|---|
| `delivery/*` | The E2d delivery's exact stored bytes; `wrapper.json` is `{wrapper, codec, candidateDigest}` |
| `fresh.json` | The one-snapshot as-of proof in snake_case, as the consumer's `toFresh` reads it (`basis` optional) |
| `policy.json` | `{principal, rules}`, the plan 034 policy stub |
| `request.json` | `{destination, wanted}`; the first export has `wanted = []` |
| `retrieval.json` | Recorded `{request, result}` pairs; the first export has `[]` |
| `expected.json` | `handoff-expectation.v0`, from the producer's **own** composition |
| `view-part.txt` | The composition's emitted part (UTF-8) |
| `provenance.json` | `exportVersion` plus the producer-**declared** fields (§3) |

## 3. Truthfulness rules

- **H1 label by identity.** `expected.h1Builder` is `"chatagent-h1"` only when the review task source's builder **is** `e1.h1_bridge.build` **and** that bridge's verified runtime was captured. The bridge re-checks ChatAgent `5255daa` and Node `24.21.0` on every call. Any injected builder is labelled `injected-h1-NOT-chatagent`, and its export uses the stub builder name, which the consumer refuses. Such an export is possible only with `test_only`, so it can never pass as real parity.
- **Derived provenance.** Nothing is typed in:
  - `worker` comes from the round's journal `exited` record (`requestedModel`, `reportedModels`) plus the host-declared execution kind; `reportedModelsAuthenticated` is always `false`.
  - `h1Bridge` comes from the verified bridge runtime (nulls when not real).
  - `hekateCommit` and `hekateTreeClean` come from a controlled git query that counts untracked files and requires a 40-hex HEAD.
  - `reviewer` is `{kind: "deterministic-verifier", inputSha256: null}`: the request it implies is an **offline artifact only**, and no model session is claimed to have consumed it.
  - `synthetic` is true unless the composition was real.
- **One capture time.** `capturedAtIso` is the actual UTC time, taken once at prepare and reused unchanged by every composition H1 call.
- **Execution kind (HK-ISSUE-013).** Runs record a host-declared `executionKind`: `simulated` (default), `fake-cli` or `claude-cli`. `dryRun = executionKind != "claude-cli"`, never inferred from attestation.

## 4. Pilot integration (opt-in; the default is unchanged)

- `e1/pilot.py` retains the raw claim response bytes. It has an injectable `ReviewTaskSource` (the default is the unchanged stub) and a post-compose `exporter` hook that runs before the reviewer. An export failure is a typed `export_failed` stop; an H1 failure is `review_task_unavailable`.
- `e1/pilot_export.py` holds `RealH1ReviewSource` (the pinned bridge on the retained claim bytes) and the `Exporter`, which writes one export per round under `<run_root>/exports/export-r<n>`.
- Entry point: `uv run python -m e1.pilot_real … --launch-real-model --export --root-go <msg>`, from a clean checkout, with `HEKATE_E1_CHATAGENT_DIR` pointing at the pinned `5255daa` checkout.

## 5. Acceptance (ChatAgent side)

Capture and acceptance follow msg 1576, steps 0–5:
- `compose --export` with ChatAgent `2383e85` on the pinned Node;
- independent index, parity, binding, request and model-label checks;
- negative controls on copies;
- capture into ChatAgent's fixtures only after root acceptance.

A synthetic golden-derived export already round-trips through that consumer byte for byte (msgs 1593, 1600). That shows format parity only, not a real run.

## 6. Not included / open

- No real export exists yet (separate root GO).
- `wanted` and `retrieval` stay empty in v0.
- A model-session reviewer with a logged `inputSha256` is a later, separate criterion.
- Cost capture is deferred.
