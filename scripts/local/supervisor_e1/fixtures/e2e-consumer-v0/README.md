# E2e golden consumer fixture bundle (`e2e-consumer-v0`)

An **offline test fixture** for the handoff consumer contract of plan 034 rev 3 (accepted E2e evidence: plan 035). It was produced by one real run of the accepted path and is meant to be consumed offline, for example by a ChatAgent-side consumer test.

**It grants no authority and invokes nothing.** No conversation, worker or model is launched or called. Nothing is authorized: the policy files are the fixture's default-deny **stub**, not production auth. There is no network transport; the "delivery" is a set of files.

## What produced it (pins)

| Item | Value |
|---|---|
| Contract | plan 034 rev 3, sha256 `17273a5194ccec681a7b9eca7089db84cf20fe3489e64f3729130059cd2ab9db` |
| Hekate | base `d0ed671` + the accepted E2c/E2d/E2e overlay (plans 031, 033, 035) |
| Consumer | `e1/consumer.py` (its sha256 is in `producer.json`) |
| H1 | ChatAgent **`5255daacfc670a4919f61439eb12adcb6a401920`**, Node **`24.21.0`**, via `e1/h1_bridge.py` on a detached pinned checkout (path in `producer.json`) |
| Estimator | `chatagent-utf8-conservative-v1@5255daa` (UTF-8 bytes; 32 per request, 16 per message) |
| Wrapper / codec | `handoff-delivery.v0` / `py-canon.v0` |

**The path:** a real claim on the disposable Api, then the **real H1** on its raw response, used as the task. Then finish, review requested, ACK and one progress checkpoint by the lead. Then E2d prepare (task = the real H1 output) with **two prior-conversation imports** (one `user-stated`, one `assistant-claimed`, from an explicitly **synthetic** prior source) and a **predecessor note**, then commit. Then the delivery from the stored candidate bytes, a one-snapshot `Fresh` proof, and three compositions: allow policy, default deny, and a wrong destination. The ids inside are random; regenerating gives a different, equally valid bundle. This bundle is one fixed snapshot.

## Files

| Path | Content |
|---|---|
| `delivery/manifest.bin`, `envelope.bin`, `task.bin` | the **exact** stored candidate bytes (`sha256(manifest.bin)` = the candidate digest) |
| `delivery/receipt.json` | the exact E2d commit receipt bytes |
| `delivery/h1-input.json` | the exact H1 options bytes (raw claim response, rules, instructions, budget, `capturedAtIso`) |
| `delivery/wrapper.json` | `{wrapper, codec, candidateDigest}` |
| `inputs/fresh.json` | the **as-of** revalidation proof (one combined snapshot at generation time); its `basis` names that snapshot. It is evidence of that moment, not of now |
| `inputs/policy-allow.json`, `inputs/policy-deny.json` | the policy-stub inputs (principal + rules) |
| `inputs/request.json` | the destination conversation and the requested evidence pointers |
| `inputs/request-wrong-destination.json` | the same request for another destination conversation |
| `inputs/prior-source-SYNTHETIC.json` | the **synthetic** prior conversation the imports came from (ChatAgent `SourceRef` shape: `conversationId`, `eventId`, `messageId`, `contentHash` = SHA-256 of the UTF-8 text, `provenance`). Fixture data, not a production source store |
| `recorded/retrieval.json` | each retrieval request (the full pointer + source stream) and the as-of result of the validated-chain retriever |
| `recorded/h1-calls.json` | every **real** H1 call the consumer made (the options with the reduced `windowTokens`, and the result) |
| `expected/h1-text.txt` | the exact H1 package text (the committed task) |
| `expected/valid/`, `expected/denied/`, `expected/wrong-destination/` | the exact emitted view part (`view-part.txt`), and `expected.json` with `viewDigest`, `reservationTokens`, `viewCost` and the H1 `suppliedSha256` |
| `variants.json` | 17 cases: `valid`, `denied`, `wrong-destination`, and mutation instructions with their expected refusal code (corruption incl. import and note text, malformed shapes, stale proofs, over budget, wrong codec) |
| `producer.json` | the pins above, machine-readable |
| `INDEX.sha256` | the SHA-256 of every other file |
| `generate.py`, `replay.py` | the producer and the offline verifier |

## Verify offline

```powershell
cd scripts/local/supervisor_e1
uv run python fixtures/e2e-consumer-v0/replay.py      # no database, no H1, no network
```

`replay.py` first prints the path and SHA-256 of the consumer implementation it actually imported and fails on any difference from `producer.json` `consumerSha256` (no silent revision drift). It checks every hash in `INDEX.sha256` (and rejects unlisted files). It then recomputes both compositions with the accepted consumer from the exact bytes, the as-of proof and the **recorded** H1 calls and retrieval results; an unrecorded call is an error. It requires the expected view-part bytes, `viewDigest`, reservation and H1 text byte for byte, and checks every variant's expected outcome. It prints `REPLAY PASS` and exits 0 only when everything matches.

## What the three compositions show (the host contract surfaces)

| Item | `valid` (allow, own destination) | `denied` (default deny) | `wrong-destination` (allow, other destination) |
|---|---|---|---|
| user-stated import | `included`, label `imported:user-stated`, original ref + `contentHash`, destination mapping, policy decisions with tokens, the text | `denied` (`policy`): ref, provenance, destination mapping and decisions kept; **no text** | `denied` (`destination_mismatch`): as for denied; **no text** |
| assistant-claimed import | `included`, label `imported:assistant-claimed` (it stays a claim) | `denied`, no text | `denied`, no text |
| predecessor note | `included`, label `claim`, author session, sha256, text | same (the note is part of the committed package, always a claim) | same |
| evidence retrievals | `included`, label `as-of`, with read basis | `denied` before any callback | `included` |

A `label` field appears only on included items; denied items carry the `provenance` field instead.

## Variants and mutation instructions

Each variant starts from the valid inputs and applies `mutations` in order:
- `replace` (first occurrence, on the UTF-8 text of a byte file);
- `raw` (replace a byte file entirely);
- `set` (a JSON path in `fresh`, `policy`, `request` or `h1Input`);
- `field` (a wrapper field);
- `policyFile` / `requestFile` (use another policy or request input).

The expected outcome is either `composed` (with the expected directory) or `refused` with an exact code: `digest_mismatch`, `delivery_mismatch`, `receipt_shape`, `strict_json`, `receipt_not_current`, `binding_moved`, `stale_content`, `review_not_candidate`, `fresh_mismatch`, `CONTEXT_TOO_LARGE` or `codec_unsupported`.

## Regenerate (needs the DB/API slot and the pinned H1)

```powershell
$env:HEKATE_E1_CHATAGENT_DIR = "<detached checkout at 5255daa>"
uv run python fixtures/e2e-consumer-v0/generate.py     # port 5108 free, owned hekate-local container up
```
