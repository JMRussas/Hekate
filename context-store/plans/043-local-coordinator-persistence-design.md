# Plan 043 — local development coordinator: persistence and operator acts (P2 DESIGN, revision 2)

**Status: revised design for root review (2026-10-07; root msgs 1800, 1837, 1873).**
- No code, no schema activation and no writes to any live database. Implementation needs a separate GO after D3.
- **Scope: the LOCAL development coordinator only.** This records a local decision. The production questions stay open in 028 / HK-ISSUE-009 / 010 / 011.

## 1. HK-ISSUE-009 (local): journal location and initialization

**Decision:** the journal lives in the `supervisor_journal` schema of the **same** PlanStore database.
- Route A already reads PlanStore tables under the project lock (`acts_durable.py:60-80, 147`).
- The target is a **new dedicated local development database and project**: never an existing populated database, never a production selection.

**Initialization (honest about transactions).** The existing installers each open their own connection and transaction (`durable.install` `durable.py:188`, `install_acts` `acts_durable.py:46`, `install_handoff` `handoff_durable.py:35`). So initialization is **not** one transaction, and v1 does not claim it is. Instead:
1. **Guard before any write:**
   - the server is loopback;
   - the database name equals an operator-typed `--target-db`;
   - the database has **no** `supervisor_journal` schema;
   - the PlanStore schema is present;
   - the configured project row exists.
   A populated or partially initialized database is refused.
2. **Run the three installers in order.** `journal_schema.sql` uses plain `CREATE SCHEMA`, so a re-run fails rather than silently half-applying.
3. **Verify complete before ANY dispatch.** On every coordinator start, the expected tables and functions are present and the recorded bounds equal the configured `E2C_BOUNDS`. Anything missing or mismatched is **refused** and named; there is no repair and no migration.
   - A failed initialization leaves a database that step 3 refuses. The documented recovery is to drop that dedicated database and create a new one. There is no uninstall command.

## 2. Time across process restart and reboot

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
