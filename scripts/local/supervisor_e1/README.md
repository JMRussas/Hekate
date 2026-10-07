# Supervisor E1a — test-only plan-contract seam experiment

Plan [023](../../../context-store/plans/023-coding-worker-adapter-proposal.md) §3, first slice (E1a). Evidence: [024](../../../context-store/plans/024-supervisor-e1a-validation.md).

**This is test code, not a worker.** Nothing here launches a process, CLI or model, or edits production code.

It checks one thing: a supervisor can drive a managed leaf against the **real** plan-contract API with every write behind explicit preconditions, and every uncertain or non-success case stops for a human.

## Layout

| File | Role |
|---|---|
| `e1/exact.py` | Exact JSON (pure). Float and exponent tokens and NaN/Infinity are rejected, every integer token must fit Int64, and duplicate keys are rejected. `counter()` checks semantic counter ranges and rejects bools. |
| `e1/seam.py` | Pure validation of a **whole** raw claim response: version, flags, outcome-dependent nullability, digest forms, nested snapshots, references and counters. Also the **opaque** work package. **There is no context renderer and no package canonicalization**: the package is the token `e1-opaque:<claimKey>` plus the receipt identities verbatim. ChatAgent's H1 fields, rendering, the digest and case 16 interop are pending H1's accepted checkpoint (E1b). |
| `e1/wire.py` | Loopback-only HTTP. `SupervisorClient` exposes claim, get claim, get plan and finish/release transitions, nothing else. `SetupClient` is the test fixture's surface (create, add, revise, decide) and is never given to the supervisor. |
| `e1/supervisor.py` | `FakeWorker` (in-process, scripted) and `Supervisor` (the only writer). |
| `e1/harness.py` | A disposable database plus an Api process (port 5108). See "Isolation". |
| `tests/test_fixture_correlation.py` | Offline tests over the real raw claim fixtures in `context-store/plans/fixtures/023/`, plus derived invalid documents. |
| `tests/test_supervisor_live.py` | Live cases against the real API. |

## Run

```powershell
# The owned hekate-local container must be running (scripts/local/hekate-local.ps1 start, or docker start).
cd scripts/local/supervisor_e1
uv run pytest -q
```

uv selects CPython ≥ 3.11 (validated with 3.13.13). The only dependency is the test runner `pytest`, locked in `uv.lock`; HTTP and JSON come from the standard library.

To run from a clean checkout elsewhere, the owned container must still be named explicitly. Set `HEKATE_E1_CONTAINER_WORKSPACE=D:\Git\Hekate`; any other value is refused, and arbitrary container discovery is never done.

## Isolation

The harness owns only what it creates:
- a new `hekate_plan_e1_<utc>_<hex>` database, with a name guard, in the owned `hekate-local` container (verified by its compose and workspace labels, published on loopback only);
- one Api process, built into a private `.run/<id>/` folder and started on `127.0.0.1:5108` with `HEKATE_PLAN_CONTRACT=1` and the dispatcher disabled. The run fails if the port is in use.

Cleanup kills the Api, drops the database and verifies the drop. Each step is independent and errors are aggregated, so an original error is never masked. The `.run/` work folder, including `api.log`, is deleted only after a confirmed Api exit and database drop. It is kept when startup or any test fails.

## Supervisor rules under test

- The supervisor dispatches only on a **fresh** claim (`replayed=false`) that is current at hand-off. A replayed receipt or an explicit `uncertain` operator state means needs-operator, whatever `stillCurrent` says.
- Success comes from typed outcome fields only, with runtime type checks: `exitCode` an int equal to 0, `timedOut` and `killed` the bool `false`, a structured result and a non-blank artifact. It also requires a strict-type match on every correlation field. Prose is never read.
- Before finishing:
  - the re-read receipt is unchanged and `stillCurrent`;
  - the node's current attempt matches;
  - the current `stateRevision` is used only for the compare-and-set.
- PlanStore is the final authority. A rejected finish becomes needs-operator, with no retry.
- Transition idempotency is last-operation only. The held finish is re-sent only while current state proves it is still the node's last operation; otherwise nothing is sent and the case goes to reconciliation.
- Release only on explicit operator instruction, with key `supervisor:<claimKey>:release`. The supervisor never calls `decide`.
- There is no launch journal in E1a, so restart durability is **not** claimed; the replay cases test policy only.

## E1b: ChatAgent H1 interop (opt-in)

Plan 023 §3.3a E1b. Evidence: [025](../../../context-store/plans/025-supervisor-e1b-validation.md). These suites are **not** part of the default run. Selecting them requires the pinned sibling ChatAgent checkout. When it is missing, dirty or at the wrong commit, they **error**; they never skip.

```powershell
uv run pytest interop        # pure: H1 over the real raw fixtures and derived inputs; no HTTP, no container
uv run pytest interop_live   # one live case: real claim bytes -> H1 -> fake worker -> finish (needs the container too)
```

| File | Role |
|---|---|
| `e1/h1_bridge.mjs` | Runs ChatAgent's **actual** `buildPlanTaskContext` (`node --import tsx`, with cwd set to the ChatAgent checkout). Options go in on stdin and one JSON line comes out. Refusals are typed and never echo content. Also runs frozen/copy probes. It renders nothing itself. |
| `e1/h1_bridge.py` | Before every call, checks the pin: HEAD is `5255daa…`; the H1 module, `src/app`, `src/domain`, `package.json`, `package-lock.json`, `tsconfig.json` and `.node-version` are tracked and clean; tsx is already installed (never installs); and the Node executable's `--version` equals `.node-version` exactly. The executable is `HEKATE_E1_NODE` or the known `node_modules/.cache/worker-diagnosis/new24/node.exe` (v24.21.0, recipe 812), never PATH. Output is parsed with `loads_exact`. |
| `e1/h1_package.py` | Turns the H1 result into the supervisor's work package using **only H1's actual fields** (there is no `packageDigest`). It checks typed provenance against the supervisor's own parse of the same raw bytes and recomputes `suppliedSha256` independently. `CONTEXT_TOO_LARGE` and `PACKAGE_TOO_LARGE` become `package_overflow`; other codes become `package_refused`. Both happen before dispatch. |

`ChatAgent dir`: `HEKATE_E1_CHATAGENT_DIR` (default `D:\Git\ChatAgent`).

`suppliedSha256` binds the mandatory task text **only**. The system, fast and deep instructions are captured and correlated separately, as their actual strings. Semantic identity includes them, and excludes `replayed`, `stillCurrent`, the random `snapshotId` and runtime details.

The test supervisor also handles faults (review msg 829). Lost claim or finish replies, malformed 5xx bodies, a worker that raises, failed reads and builder failures each become a structured needs-operator outcome. That outcome keeps the claim request, package, run, result and any held finish. Nothing is retried, released or relaunched automatically.

## E2a: launch and review evidence model (test-only)

Plan [026](../../../context-store/plans/026-launch-and-review-evidence-proposal.md) §8. Evidence: [027](../../../context-store/plans/027-supervisor-e2a-validation.md). Part of the default suite. **No durability, restart, fsync or wake claim; there is no real journal.**

| File | Role |
|---|---|
| `e1/evidence.py` | A pure in-memory model journal. Streams are keyed by `(rootId, claimKey)` with a single writer. Intents reserve worst-case terminal and fault capacity; payloads are typed and byte-capped; global caps use prospective eviction of resolved data only; resolution must be explicit and terminal. `classify()` returns operator-only resolutions for crash points C0–C11. `derive_review()` returns candidates and accept-eligibility separately. `ReviewWorkflow` runs on a fake clock with an explicit `wake(now, current)`. |
| `e1/coherent.py` | **Fixture-only** coherent read: node state plus all events in one repeatable-read SQL statement on the disposable database. `prove_finish()` returns `proved`, `superseded`, `unconfirmed`, `intervening` or `proof_missing`. Not a production mechanism. |
| `e1/supervisor.py` | An optional `journal(kind, data)` hook, a no-op by default. When an intent is refused, its effect does not happen. Every outcome record is required: if one fails or comes back `degraded`, the run stops with `outcome_unrecorded:<kind>` before any further effect. `launched` and `exited` are model-only facts. |

To run the H1 suites against the pinned commit while the sibling ChatAgent checkout has moved on, point `HEKATE_E1_CHATAGENT_DIR` at a detached `5255daa` clone whose `node_modules` is a shared directory junction to the original (not enforced read-only; the tests do not write to it). Never reset the original checkout.

## E2b-a: durable journal experiment (test-only, disposable database)

Plan [028](../../../context-store/plans/028-e2b-durable-journal-proposal.md) §9. Evidence: [029](../../../context-store/plans/029-supervisor-e2b-a-validation.md). Part of the default suite. The `supervisor_journal` schema exists **only** in the harness's disposable `hekate_plan_e1_*` database; nothing is added to `PlanStoreSchema` or any production migration. No worker, provider, service, wake or send.

| File | Role |
|---|---|
| `e1/journal_schema.sql` | Fixture-only tables: a singleton `global_usage` row (totals and shared caps), `writer_usage` (epoch, unresolved), `streams`, `records`, `summaries`, `takeovers`. Append-only triggers refuse UPDATE, TRUNCATE and any DELETE outside a compaction transaction. These are integrity checks, **not authorization**. |
| `e1/durable.py` | `DurableJournal`: the E2a hook contract over those tables, reusing `e1/evidence.py` for vocabulary, payload rules, reservations and resolution. One record per transaction, with locks in the order global, then writer, then stream. Idempotent client record ids. A lost COMMIT reply raises `CommitUnknown`, and `confirm()` is the only way to learn the outcome. Every append checks the epoch and that its own session holds the writer lock (append fencing only; effects are not fenced). The hash chain detects accidental corruption, not a credentialed writer. Corrupt streams and summaries are never compacted or evicted. |
| `e1/recovery.py` | The shared coherent-read adapter experiment (not a production proof path). Each stream is read in one REPEATABLE READ snapshot, bounded in SQL, and classified with the E2a classifier. `scan(now)` is bounded per call, uses a cursor, returns intended notifications only, and maintains a bounded operator-queue view with explicit overflow. It writes nothing. |
| `e1/journal_child.py` | A child process with its own database session, killed by tests at crash points. |

Dependency: `psycopg[binary]` (locked). The E2b-a tests need session advisory locks, explicit transactions and concurrent connections, which one-shot `psql` cannot provide.

## E2c: worker ACK, progress and review obligation (test-only, disposable database)

Plan [030](../../../context-store/plans/030-worker-ack-progress-review-boundary.md) rev 7 §12 (sha256 `52ef6784…ab3a`). Evidence: [031](../../../context-store/plans/031-supervisor-e2c-validation.md). Part of the default suite. **Fixture constants only** (`hkw1` sessions, the `packageRef` forms, G/V/K, windows): none of them is an accepted production identity, auth or default contract. No worker, provider, wake, send, bridge or ChatAgent change.

| File | Role |
|---|---|
| `e1/acts.py` | Pure. Exact act wire (030 §4/§4.1), `decide_act` / `decide_confirm` / `decide_request` / `decide_binding` over a validated record list plus PlanStore facts, derived bookkeeping, the binding chain (linkage + append seq, never `at`), deadline phases and the C-B read with per-field proof. `ActsModel` runs it over the E2a `ModelJournal`. |
| `e1/acts_durable.py` | `ActsJournal(DurableJournal)`: every same-session entry point is serialized by one re-entrant lock. Route A = one transaction that takes the PlanStore project advisory lock first, reads the PlanStore facts, then the E2b-a global → writer → stream locks, decides, and appends one record (or bumps one counter). `read_cb` is one REPEATABLE READ READ ONLY snapshot; a journal outage returns the PlanStore leaf with evidence unknown. |
| `e1/acts_schema.sql` | Fixture-only `e2c_counters` (saturating u32, known keys plus one shared bucket), `e2c_queue` (one entry per key per reason) and `e2c_runs` (a runId is dispatched once across all streams). Act records themselves are ordinary `supervisor_journal.records`. |
| `e1/evidence.py` | Vocabulary only: `dispatch_intent` (reserves `dispatch_outcome`) and the E2c ordinary kinds. The E2a classifier does not interpret them. |
| `tests/test_e2c_model.py`, `tests/test_e2c_live.py` | Model cases and live cases against real claims, transitions and decisions. |

E2c streams use `E2C_BOUNDS` (per stream 256); the E2a/E2b-a defaults are unchanged.

## E2d: selective-context handoff, review-lead rollover (test-only, disposable database)

Plan [032](../../../context-store/plans/032-selective-context-conversation-handoff.md) rev 6 §6 (sha256 `60a3bdfa…3899`). Evidence: [033](../../../context-store/plans/033-supervisor-e2d-validation.md). Part of the default suite. **Review-lead rollover and the package only**; worker rollover (R/S) is deferred. No conversation is launched, woken or invoked. The task part is **H1-shaped test data**, not a ChatAgent H1 rendering. Import authorization is an explicit two-sided **stub**. Fixture constants only.

| File | Role |
|---|---|
| `e1/handoff.py` | Pure. The `handoff-envelope.v0` + canonical manifest (`candidateDigest`), the per-role proof policy (liveness from the PlanStore class), the single byte/reference accounting rule, pending-effect listing, import verification, the semantic freshness set, the commit decision (retry check first, then freshness, then the predecessor CAS) and receipt status. |
| `e1/handoff_durable.py` | `prepare` (one snapshot, immutable candidate, no authority), `commit` (route A via `ActsJournal._route_a`; the stored candidate is reloaded and verified in the transaction), `verify_receipt`, `redeliver`. |
| `e1/handoff_schema.sql` | Fixture-only `e2d_candidates` (immutable; retains the exact Task bytes). |
| `tests/test_e2d_model.py`, `tests/test_e2d_live.py` | Model and live cases. |

Activation is a client obligation: binding validation accepts a successor's acts once the commit is durable, even before it fetches the receipt (tested and documented, no fence claimed).

## E2e: offline handoff consumer (test-only)

Plan [034](../../../context-store/plans/034-offline-handoff-consumer-contract.md) rev 3 (sha256 `17273a51…9db9`). Evidence: [035](../../../context-store/plans/035-supervisor-e2e-validation.md). Consumes the **exact accepted E2d v0 bytes** through a `handoff-delivery.v0` wrapper (`codec: py-canon.v0`), builds a separately identified **consumer view** (the committed candidate is never altered) and composes it with H1 **without changing H1**: H1 stays one message and is only called with `windowTokens` reduced by the view's cost. No invocation, launch, wake, write or production auth; the import/retrieval policy is a default-deny **stub**.

| File | Role |
|---|---|
| `e1/consumer.py` | Pure. Raw ingress caps (unread), strict decoding (duplicates, non-finite numbers, lone surrogates, invalid UTF-8 → typed refusal), closed receipt/H1-option shapes, digests over the exact bytes, E2d shape verification, delivered-content caps, the policy stub, bounded whitelisted retrieval (authorization before any callback; request bounded and deduped first; optional outages become `unavailable`), the view with a fixed-width reservation and `viewDigest` outside its preimage, H1 binding, and an **H1-shaped stub** for the default suite. |
| `e1/consumer_durable.py` | The delivery from stored bytes, the **one combined snapshot** revalidation bound to the verified candidate, and a retriever bound to the verified stream that validates the chain and the full pointer per call. |
| `e1/sha_check.mjs` | SHA-256 over exact bytes under the pinned Node (opt-in only). |
| `tests/test_e2e_model.py`, `tests/test_e2e_live.py` | Default suite (Python + Postgres). |
| `interop/test_e2e_node_sha.py`, `interop_live/test_e2e_h1.py` | Opt-in: pinned Node SHA over exact bytes; the **real** pinned H1 as the task at prepare, bound at the consumer, with the exact budget boundary. A missing pinned prerequisite errors the selected suite only. |
