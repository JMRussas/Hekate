# Plan 043 — local development coordinator: persistence and operator capability (P2 DESIGN ONLY)

**Status: proposal for root review (2026-10-07; direction from root msg 1800).**
- No code, no schema activation and no writes to any live database.
- **Scope: the LOCAL development coordinator only.** This records a local-only decision. The broader production questions stay open in 028 / HK-ISSUE-009 / 010 / 011 and are not decided here.

## 1. HK-ISSUE-009 (local): where the journal lives

**Decision proposed (root direction):** the `supervisor_journal` schema lives in the **same** database as PlanStore.
- `ActsJournal` Route A already takes the PlanStore project lock and reads PlanStore tables in one transaction (`acts_durable.py:60-80, 147`). Coupling keeps that coherent read and keeps PlanStore the sole task authority.

**Target:** a **new dedicated local development database and project**, never an existing populated database and never a production selection.

### Minimal implementation (one module plus a CLI, about 150 lines plus tests)

`e1/local_store.py`:
- **`target(dsn, expected_db, project_id)`** refuses unless:
  - the server is loopback;
  - the database name equals an explicit `--target-db` that the operator typed;
  - the database carries a marker table `hekate_local_coordinator(marker, created_at, version)` with one row whose marker names this purpose;
  - the project row exists.
  A populated database without the marker is refused, so there is no accidental adoption.
- **`install(dsn, ...)`:** explicit opt-in (`--install --target-db X --i-understand-local-only`).
  - It writes the marker plus the existing `durable.install` / `install_acts` / `install_handoff` in ONE transaction.
  - It records `schema_version` and the source sha256 of each schema file. Re-running with the same versions is a no-op; a version mismatch refuses (no migration in v0).
  - **Rollback:** `--uninstall` drops only `supervisor_journal.*` and the marker row, and only when no stream is open, that is, no node is in flight in PlanStore. Otherwise it refuses.
- **Clock:** `now = time.monotonic()` for intervals, plus the PlanStore event `createdAt` for the record time. The fixed `now=1000.0` stays test-only. A backwards wall-clock step is recorded, never trusted.
- **Restart classification:** on start, `recovery.scan` (the existing read-only path) plus the plan-run v0 in-flight rule. Any `uncertain` or in-flight stream stops for the operator, as today. There is no automatic resume in this increment.
- **Bounded evidence:** per-node run roots exactly as P1. The journal tables keep their existing caps (`global_usage.per_stream`).

**Threats and boundary:** the threats are wrong-database writes, which the explicit target name plus the marker plus loopback prevent, and a half-installed schema, which the single transaction prevents. It is not a multi-user store, has no network exposure and no production use.

## 2. HK-ISSUE-010 (local): an operator capability, not the test-fixture surface

**Problem:** `SetupClient` is the "test-fixture surface" (`wire.py:79-80`). Decisions, the HK-005 moves and `pin_spec` go through it with an `actor` string over loopback. **That is not authentication**, and it must not be labelled as such.

**Minimal option (recommended): a named local capability file.**
- `e1/operator.py` exposes ONLY the named coordinator and operator acts:
  - `decide`;
  - `fix_round_reset` (the HK-005 cancel→todo);
  - `pin_spec`;
  - `release_inflight`, after operator classification.
  Each method maps to exactly one existing route. There is no general HTTP passthrough.
- **The capability:** a random 256-bit secret in a file the operator creates (`--init-operator`), readable only by the local user, with its sha256 recorded in the marker table at install.
  - Every operator act carries `actor = "operator:<name>"` and an HMAC of the operation key under the secret. It is checked locally before the call, and the act plus its HMAC are appended to the journal (`operator_act` record).
  - What this proves: **possession of a local file**. What it does not prove: authentication to PlanStore, which still trusts loopback. It is described exactly that way in the evidence.
- **Alternative (smaller, weaker):** keep `SetupClient`, but restrict `run_plan` to the four named calls and record them as operator acts without any capability. This is only acceptable if root accepts "loopback plus local user" as the entire boundary.
- **Not proposed:** any PlanStore auth change. Production auth stays HK-ISSUE-010 / 028 OQ2.

## 3. Order of work after GO

1. `local_store` target guard and installer (disposable-database tests: refuse an unmarked database, refuse a version mismatch, rollback only when idle).
2. `operator.py` capability (tests: a missing or wrong secret is refused; only the named acts exist).
3. A plan-run v0 option `--store local`: the same driver, a real clock.
4. One local multi-task demonstration on the NEW dedicated database, under a separate GO.

**Estimate:** about 300–400 lines including tests, plus a review. No change to the PlanStore C# code.
