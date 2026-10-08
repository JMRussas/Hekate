# Plan 043 — local development coordinator: persistence and operator acts (P2 DESIGN, revision 3)

**Status: revision 3, the design accepted for source implementation with disposable tests (2026-10-07; root msgs 1800, 1837, 1873, 1878).**
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
