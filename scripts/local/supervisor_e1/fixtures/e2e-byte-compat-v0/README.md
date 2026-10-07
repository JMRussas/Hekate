# E2e byte-compatibility supplement (`e2e-byte-compat-v0`)

A **supplement** to the accepted golden bundle `e2e-consumer-v0` (unchanged). It covers the Python/JavaScript byte and number differences that the golden bundle (ASCII-only, whole-number floats) does not. It is an **offline test fixture**: it grants no authority, invokes nothing, uses no network, and assigns **no new codec**. The v0 producer's codec is `py-canon.v0` (Python `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`), which is **not** RFC 8785.

## Two kinds of content (never mix them up)

| Part | What it is | What it is not |
|---|---|---|
| `vectors/` | **Generic JSON byte vectors**, each with exact bytes, SHA-256, the strict reader's typed outcome and the canonical-form check | **Not deliveries** and not producer output. Valid JSON is not a valid producer value. `producerReachable` says whether the v0 producer can emit that value class at all |
| `deliveries/` | **Valid, producer-reachable deliveries** built by the accepted producer code (the E2d **model** path + `handoff.build`), composed by the accepted consumer | **Not DB-committed**: the receipt and the as-of proof are **synthetic** (consistent with the manifest). The H1 builder is the **H1-shaped stub**, not ChatAgent's real H1 |

## Two verification stages (a byte hash alone never promotes a value)

1. **Strict reading** (`consumer.strict_loads`): valid UTF-8; no duplicate keys, including an escaped duplicate (the key `a` written once raw and once as its six-character backslash-u escape, vector `escaped-duplicate-key`); no NaN or Infinity, including overflowing literals like `1e999`; no lone surrogates, raw or escaped. It **accepts** valid but non-canonical JSON such as `1e0`, `1.1e3`, `1100.00`, `-0`, whitespace, unsorted keys, backslash-u escapes of non-ASCII characters (vector `noncanonical-escaped-nonascii`) and an escaped slash (vector `noncanonical-escaped-slash`).
2. **Canonical manifest verification and cross-field consistency** (`handoff.verify_stored`): the manifest must be exactly the `py-canon.v0` bytes of its value (Python forms: `1.0`, `-0.0`, `1e-05`, `5e-324`, raw UTF-8, raw `/`, sorted keys, no whitespace). It must hash to the committed `candidateDigest`, and the envelope and task must be exactly consistent with it.

Without a recomputed digest, any change to the manifest bytes is refused `digest_mismatch` at the exact-byte stage. With the digest recomputed (the `*-redigested` variants), two different checks refuse it, both reporting `delivery_mismatch`. Each refusal variant states its `class`, and `replay.py` asserts that class independently:
- **lexical** (the decoded value is identical; only the bytes are not canonical): `1e-05` to `0.00001`, `999999999999.5` to `999999999999.50`, a real `/` escaped as backslash-slash, a CJK character written as its backslash-u escape. The **canonical-form** check refuses them.
- **value-change** (the decoded value differs; the new bytes may themselves be canonical): `-0.0` to `0` (float to int), `1.0` to `1`, `9007199254740993` to `9007199254740992` (JS Number rounding), NFC to NFD, a corrupted note character. A **cross-field consistency** check refuses them: the envelope and the reconstructed manifest, or the task/import/note hashes, no longer agree.

"Identical decoded value" is compared by the `py-canon.v0` encoding of the decoded value, so `-0.0` and `0`, or `1.0` and `1`, count as different (plain `==` would equate them).

No Unicode normalization is applied anywhere: NFC `é` and NFD `e + U+0301` are different bytes and different hashes (vectors `nfc-precomposed` / `nfd-decomposed`; variant `nfc-to-nfd-in-task-text`).

## Reachable values and ranges (v0 producer)

| Value | Reachable? | Where | JS note |
|---|---|---|---|
| Non-ASCII text (BMP, astral, combining, RTL, CJK, U+2028/2029), controls in text | yes | task text (PlanStore content via H1), note, imports, `attemptId` | bytes are identical; never re-encode |
| Non-ASCII **object keys** | **no** (every key is a fixed ASCII field name) | — | code-point vs UTF-16 key order would differ; not reachable |
| Fake-clock floats: `1100.0`-style, `-0.0`, `1e-05`, `5e-324` (subnormal), `999999999999.5`, `1000000000000.0` | yes: `_valid_clock` allows finite `0 ≤ v ≤ 1e12` whose repr fits 23 characters, and `-0.0 ≥ 0` | manifest diagnostics `ack.at`, `deadlinePhase.anchorAt`/`deadline` | JS renders `1100`, `0`, `0.00001`: different bytes. Clock values are **carried**, not used in consumer arithmetic |
| Floats ≥ 1e16 / positive exponents | **no** (clock ≤ 1e12) | — | — |
| NaN, Infinity, lone surrogates, duplicate keys | **no** in valid producer bytes | — | refused by the strict reader |
| Integers: counters ≤ 2^32−1; `actSeq`/`checkpointId`/`attemptEpoch` ≤ 2^53−1 (wire limits); budgets ≤ 2^53−1 each (closed shape) | yes | manifest, view | safe in a JS Number |
| Journal `seq` (records, evidence pointers, receipt `seq`) | **schema** allows up to 2^63−1 (`MAX_SEQ`); **current fixtures** stay small (the per-stream cap is 256) | evidence index, receipt | equality of pointers/receipt `seq` must be **lossless** (not a JS Number above 2^53) |
| PlanStore `event_seq` in the manifest `state.basis.eventSeq` | **schema** bigint up to 2^63−1 and **not bounded** by v0 `facts_problem`; the `eventseq-beyond-2^53` delivery uses an accepted **synthetic** basis value 2^53+1 (not evidence that a live database reached it) | manifest basis | a JS Number parse rounds it (`…993` → `…992`): variants `js-number-rounded-eventseq*` |
| Budget arithmetic | inputs are each ≤ 2^53−1, but `windowTokens − reservation − max(outputs) − safety` is computed **exactly** (Python ints) | consumer | a host must use exact or overflow-safe integer comparison (case `eventseq-max-window-budget` uses `windowTokens = 2^53−1`) |

## Files

| Path | Content |
|---|---|
| `vectors/<name>.bin`, `vectors/expected.json` | 34 vectors: each vector's exact bytes; expected `sha256`, `strict` outcome, `pyCanonV0Canonical` (`true`/`false`/`null` when unreadable), `producerReachable` and a note |
| `deliveries/<case>/` | `manifest.bin`, `envelope.bin`, `task.bin`, `receipt.json` (synthetic), `h1-input.json`, `wrapper.json`, `fresh.json` (synthetic as-of), `request.json`, `expected/{view-part.txt, expected.json}`; `eventseq-beyond-2^53` also has `expected-max-window/` |
| `variants.json` | 18 cases: 6 compositions that must reproduce byte for byte, and 12 typed refusals, each with its `class` (5 lexical, 7 value-change): JS-style re-encodings with and without a recomputed digest, NFC→NFD, an escaped non-ASCII character, a corrupted astral note, an escaped real slash |
| `producer.json` | the plan pin, the SHA-256 of the accepted `consumer.py` and `handoff.py` that produced it, and the labels above |
| `INDEX.sha256` | the SHA-256 of every other file |
| `generate.py`, `replay.py` | producer and offline verifier |

## Verify

```powershell
cd scripts/local/supervisor_e1
uv run python fixtures/e2e-byte-compat-v0/replay.py                       # Python only, offline
uv run python fixtures/e2e-byte-compat-v0/replay.py --node <pinned 5255daa checkout>   # optional
```

`replay.py` checks the index, then that the **imported** `consumer.py` and `handoff.py` match `producer.json` (no silent drift). It checks every vector's hash and both stages, recomposes every delivery, and checks every variant's outcome and its lexical/value-change class. The optional `--node` step runs the **pinned** Node (`e1/sha_check.mjs`) over every file's exact bytes; it proves **byte-hash parity only, not JavaScript semantic parity**.
