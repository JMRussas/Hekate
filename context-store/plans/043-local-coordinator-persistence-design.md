# Plan 043 — local development coordinator: persistence and operator acts (P2 DESIGN, revision 3)

**Status: revision 3; source implemented with disposable tests, pending independent review (2026-10-07; root msgs 1800, 1837, 1873, 1878, 1887). No dedicated coordinator database has been activated.**
- No code, no schema activation and no writes to any live database. Implementation needs a separate GO after D3.
- **Scope: the LOCAL development coordinator only.** This records a local decision. The production questions stay open in 028 / HK-ISSUE-009 / 010 / 011.

## 1. HK-ISSUE-009 (local): journal location and initialization

**Decision:** the journal lives in the `supervisor_journal` schema of the **same** PlanStore database.
- Route A already reads PlanStore tables under the project lock (`acts_durable.py:60-80, 147`).
- The target is a **new dedicated local development database and project**: never an existing populated database, never a production selection.

**Initialization: the coordinator CREATES its own database; it never adopts one (root msg 1878).** "No journal yet, and PlanStore and the project exist" does not prove that a database is new and dedicated, because an existing populated app database can satisfy it. So:

1. **Create, never adopt.** This reuses the harness pattern (`e1/harness.py`):
   - Use the single owned `hekate-local` container, selected by its compose and workspace labels and published on loopback.
   - `createdb` a NEW database under a guarded, purpose-specific name, `hekate_coord_<utcstamp>_<hex>`. Creation fails if the name exists.
   - In the SAME step, write a marker table `hekate_local_coordinator(marker, purpose, project_id, created_at)` with one row, where the marker is a fresh random token.
   - Write a **locator file** in the coordinator's local state directory: `{db, marker, projectId, apiPort}`, created exclusively.
2. **Initialize** that database: the PlanStore schema (via the Api, as the harness does), the project row, then the three journal installers in order.
   - The installers each own their connection (`durable.py:188`, `acts_durable.py:46`, `handoff_durable.py:35`), so this is **not** one transaction, and nothing claims it is.
3. **Open, on every start.** Read the locator, then verify before ANY dispatch:
   - the database exists, and its marker row equals the locator's marker and project;
   - the PlanStore and journal tables and functions are all present;
   - the recorded bounds equal `E2C_BOUNDS`.
   Anything else is **refused** and named. A database without that marker, any database not named by the locator, and partial or mismatched schemas are all refused. There is no repair and no migration.
4. **Recovery from a failed initialization:** the operator drops that coordinator-owned database and deletes its locator, then creates a new one. There is no uninstall command and no general installer.
5. **Unlike the harness, `stop()` never drops** the coordinator database. Tests use disposable coordinator databases (created and dropped by the test) until a separate GO activates a dedicated one.

## 2. Time across process restart and reboot

- **Scope of the claims (root msg 1878):** monotonic values are compared only within one process (one epoch). Across restarts, only recorded UTC values are compared, and only as a sanity stop, never to compute durations.
- **Recorded times are wall-clock UTC** (`datetime.now(timezone.utc)`), written into each journal record. Intervals inside ONE process (timeouts, heartbeats) use `time.monotonic()`.
- **Monotonic values are never persisted or compared across processes.** They reset on reboot and are meaningless across process lifetimes.
- **On start**, the coordinator reads the latest recorded UTC across streams. A current UTC earlier than it (a backwards clock), or later by an implausible gap (configured, e.g. more than 7 days), is a **stop for the operator**, never an automatic adjustment.
- The fixed `now=1000.0` stays test-only.
- **Restart classification:**
  - streams are classified by the existing read-only `recovery.scan`;
  - plan-run's in-flight rule applies (an open attempt, an existing run root);
  - an `uncertain` or in-flight stream is a stop.
  There is **no automatic resume** in this increment.

## 3. HK-ISSUE-010 (local): named operator acts under an explicit trust boundary

**Chosen (root msg 1837): the smaller alternative.**
- **Operator surface.** `e1/operator.py` exposes ONLY the named coordinator and operator acts that plan-run uses:
  - `decide`;
  - `fix_round_reset` (the HK-005 cancel→todo);
  - `pin_spec`;
  - `release_inflight`, after operator classification.
  Each maps to exactly one existing route, with no general passthrough. It replaces direct `SetupClient` use in run_plan and pilot.
- **Policy.** A configuration file names the operator `actor`, the allowed `rootId`s and the `projectId`. An act outside that policy is refused locally before the call, and each act is journaled as `operator_act`.
- **Boundary, stated plainly:**
  - **The boundary:** a trusted, single, local OS account on loopback.
  - **What this is NOT:** server authentication. PlanStore still trusts loopback, and the `actor` string is a label, not a credential. No local secret or HMAC is used, because a locally checked secret would not authorize anything at PlanStore.
  - **Where the real control lives:** production authentication stays HK-ISSUE-010 / 028 OQ2.

## 4. Order after GO (source plus disposable-database tests only; no live-DB writes until a separate GO)

1. The init guard and the verify-complete check. Tests: refuse an existing journal schema, refuse a partial schema, refuse mismatched bounds, refuse a non-loopback or unnamed target.
2. The clock rule. Tests: backwards time stops; an implausible gap stops; normal restart passes.
3. The `operator.py` policy surface. Tests: only the named acts exist; an act outside the policy is refused; acts are journaled.
4. `run_plan --store local`: the same driver, a real clock, the operator surface.
5. ONE local multi-task demonstration on a NEW dedicated database, under a separate GO.

**Estimate:** about 250–350 lines including tests. No PlanStore C# change.

## 5. Implemented (branch `feat/plan-run-local`; disposable coordinator databases only)

The main result (root msgs 1901/1904) is a **two-process CLI continuation** on the persistent store:
1. **Run 1:** a is accepted, then b, which is pending, stops `spec_pending`.
2. **The operator** forwards a's artifact and **pins** b's spec.
3. **Run 2**, with the SAME plan file in the SAME run root: it attaches, skips a, runs b and ends `all_done`.

Across that restart nothing is dispatched twice: in-flight or uncertain work stops.

**`e1/local_store.py`**
- **`LocalStore.create`:**
  1. a NEW guarded database;
  2. the marker row;
  3. an exclusive locator;
  4. the **single-instance lock**: a PostgreSQL session advisory lock keyed by the marker, held for the store's lifetime;
  5. PlanStore via the Api, the project row and the three journal installers;
  6. verify.
- **`LocalStore.open`:**
  1. the locator;
  2. the database exists;
  3. the marker matches;
  4. the lock is taken **before the Api starts** (a second coordinator gets `store_in_use`);
  5. the Api starts on the locator's own port (a busy port is `api_port_in_use`; another listener is never killed or adopted);
  6. verify (tables, functions, project, bounds);
  7. the clock sanity rule.
- **`stop()`** kills only this process's Api, releases the lock and **never drops** the database.
- **`session(actor, plan_bytes)`:**
  - the operator policy's **root allowlist is derived** from the validated plan file;
  - uncertain or unparseable operator acts stop the session **before any dispatch** (`uncertain_operator_acts`).
- **`LiveClockJournal`:** the journal's `now` is **UTC epoch seconds**, sampled at the pilot's ticks.
  - It is stable within an operation. The journal reads it for both a record's size and its `at`; a value read live on every access made the journal flag its own record as corrupt (found in testing).
  - It never goes backwards within the process. `time.monotonic()` is used only for in-process intervals.

**`e1/operator_acts.py`** (the module name avoids shadowing the standard-library `operator`)
- **The surface:** only the named acts, with no passthrough. These are plan, create_plan, add_child, add_dependency, decide, transition (only `cancelled`/`todo`, the HK-005 reset) and revise (pin).
- **The policy:** the actor label, the marker's project, and the derived root allowlist. Anything outside it is refused before any HTTP call.
- **D7:**
  - each act writes a durable **intent** (flush plus fsync) BEFORE the call and an **outcome** after it;
  - `uncertain_acts()` lists intents without an outcome and any partial or unparseable line, and these stop the next session;
  - nothing is retried automatically.
  The log is local JSONL because the journal's record kinds are closed; no new journal kind was added.
- **No `release_inflight`:** in-flight or uncertain work is classified by an operator, never released automatically.

**`e1/plan_import.py`**
- **The authorized pin (review 1899a):** a node *declared pending* may carry a valid spec/recipe ref at contentRevision > 1, in any work state. Re-importing the unchanged file after a pin is a no-op; every other difference is `import_conflict`.
- **`attach_plan`:** for a later run of the SAME file, it re-derives the identities and **verifies the plan with no write**. An absent plan is `plan_missing`, a missing node or edge is `plan_drift`, and any other unauthorized difference is `import_conflict`.

**`e1/plan_cli.py` `run --store local --state-dir D [--actor L]`**
- **Binding, checked before any effect** (no database access):
  - a NEW run root is a first run;
  - an existing run root must carry `plan.binding.json` = {marker, projectId, planRoot, importSha256} for THIS store and THIS file, plus the original bytes in `plan.import.json`;
  - an edited file is refused as `plan_changed`; another binding is `binding_mismatch`; a folder without a binding is `run_root_unbound`.
- **A first run** imports, then binds the run root exclusively. A binding is never written for a failed import.
- **A continuation attaches (no re-import)** and runs `run_plan` in the SAME run root: accepted nodes are skipped; in-flight or uncertain work stops.
- **A stop is `resumable: true`.** The database is kept.
- **Creating a coordinator database is not a CLI command.** It is `LocalStore.create`, and activating a dedicated one needs its own GO.

**`e1/plan_run.py`:** a recipe node whose earlier materialization stopped after creating `<key>.integration` now stops precisely with `integration_exists`, and the directory is **kept**. Automatic archive/retry is deferred (root msg 1901).

**Tests** (all on disposable coordinator databases)

`tests/test_local_store.py` (22):
- create, mark and verify; never adopt or recreate;
- open refuses a foreign marker, a missing database, a non-coordinator name or an unreadable locator;
- verify refuses a hidden journal table and mismatched bounds;
- a second coordinator gives `store_in_use`; a busy port is refused with the listener left alive;
- persistence across a process restart, with real UTC records;
- **in-flight work across a restart: no duplicate claim**;
- the clock rule; the sampled clock;
- operator policy refusals, intent/outcome logging, a crash between them giving an uncertain act, and the root allowlist (zero calls);
- uncertain or unparseable acts stop the session;
- the CLI `--store local`;
- **the two-process continuation with an operator pin** and `plan_changed`;
- an unbound run root is refused.

`tests/test_plan_run.py`:
- a pin of a pending node re-imports as a no-op;
- a declared node revised is a conflict;
- `attach_plan` gives plan_missing, attaches, allows the pin, and refuses unauthorized drift.

`tests/test_plan_run_d3.py`: a stopped integration gives `integration_exists`, with the directory kept.
