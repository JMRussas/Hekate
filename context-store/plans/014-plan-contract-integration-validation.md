# Plan 014 — Plan contract store (increment 2b1): validation evidence

**Status: accepted by codex-hekate after independent contract, live-store, HTTP and launcher verification. Scope: increment 2b1 only.**

**Date:** 2026-10-06
**Host:** fenrir (Windows 11, Docker Desktop 29.8.1, Compose v5.5.1, PostgreSQL 16 + AGE 1.6.0 + pgvector 0.8.0 in the `hekate-local` container)
**Implementer:** claude-hekate
**Reviewer and acceptance owner:** codex-hekate

Contract and design: [012](012-plan-node-contracts-v1.md), section *Increment 2b1*.
Writer inventory: [013](013-plan-writer-inventory.md).

## What was validated

| Suite | Command | Result |
|---|---|---|
| Pure rules | `dotnet test context-store/PlanContracts.Tests` | **132 / 132** |
| Live store (PostgreSQL + AGE) | `HEKATE_PLAN_LIVE_CONNSTR=… dotnet test context-store/PlanStore.LiveTests` | **25 / 25** |
| Api process (HTTP, flag on/off/unsafe) | `pwsh scripts/local/PlanContractApi.Tests.ps1` | **28 / 28**, exit 0 |
| Launcher regression | Pester 3.4, `scripts/local/HekateLocal.Tests.ps1` | **50 / 50** |
| Api build | `dotnet build context-store/Api/Api.csproj` | 0 errors (pre-existing CA2024 only) |

codex-hekate's independent runs:
- The final pure and live suites (**132 / 25**, including the complete ancestor-walk regression) on a clean overlay: committed HEAD plus only the 2b1 files, with Program.cs's unrelated rebuild-edges hunk excluded. That confirmed there are no dependencies on unknown uncommitted work.
- The Api process check, reproduced at **28 / 28**.
- Launcher Pester, reproduced at **50 / 50**.

The final harness revision verifies its own cleanup: owned processes must have exited, and the database must really be gone (`dropdb` exit code plus a `pg_database` check). Any cleanup problem counts as a failure. That revision: 28 / 28, database confirmed dropped.

**Isolation:**
- Every live run created its own database: `hekate_plan_live_*` or `hekate_plan_api_*`, name-guarded, dropped afterwards.
- No existing database was modified.
- No production host, NSSM service or model was used.
- The Api check started and stopped only its own processes, tracked as process objects rather than PIDs.

## Acceptance criteria and evidence

| Criterion (codex-hekate GO, msg 446) | Evidence |
|---|---|
| Full lifecycle persists across a new connection | Live `Full_lifecycle_persists_across_a_new_store_and_connection`. Content is also read back after reconnecting: value and `acceptance_criteria` |
| Two compare-and-set writers: exactly one applies | Live `Two_CAS_writers_exactly_one_applies` (one Applied, one `stale_revision`) |
| Opposing dependencies: exactly one rejects the cycle | Live `Opposing_dependencies_exactly_one_rejects_the_cycle` |
| Reused key with a different payload is rejected | Live `Reused_key_with_different_content…`. Pure AddChild and ReviseContent tests also cover a changed project, value and attributes |
| Raw SQL and legacy API writes blocked atomically, including attribute moves and reparenting into a managed tree | Live `Legacy_and_raw_SQL_writers_are_fenced_atomically`. Live `Managed_tree_walk_is_complete…` covers a 12,000-node chain and a cycle. Api check `C legacy attribute/value write 409`, value unchanged |
| AGE failure rolls back with no partial SQL | Live `AGE_failure_rolls_back_the_whole_operation` |
| Replay, remove and reconcile produce no duplicate edges | Live `Replay_remove_and_reconcile_never_duplicate_projected_edges`, including repair of an injected duplicate |
| Endpoints are localhost-only; flag off means absent; unsafe config is refused | Api check A (404, no schema, an ambient parent flag does not leak), B (exit 78 before DDL), C (enabled); pure `PlanContractGateTests` (19 cases) |
| Responses are readable, with named enum states and a contract version | Api check `C view has named states and content`, `C create plan` (`contractVersion`, `defaultGate`) |

## Defects found by review and fixed before acceptance

| Defect | Fix |
|---|---|
| The AddChild fingerprint omitted the project | Project added to the fingerprint, with a regression test |
| The content-key set was mutable | `ImmutableHashSet` |
| The schema fence missed an unmanaged node becoming structural, and structural inserts via an unmanaged intermediary | `hekate_in_managed_tree` ancestor walk |
| The ancestor walk failed open beyond depth 10,000 | Complete walk, cycle-safe via `UNION` on id alone |
| The gate accepted a connection string with no Host or Database | Explicit Host and Database are required |
| Global id conflicts surfaced as raw 23505 errors (500) | Pre-check under the lock, plus a unique-violation catch, giving `node_exists` 409 |
| The snapshot and response omitted saved content | Value and content attributes are loaded atomically and returned |
| The root parent was normalised to null, hiding corruption | The stored parent is kept; `invalid_root_parent` is reported |
| Reconcile projected without validating | Graph and contract-version validation run before projecting |
| Missing preconditions defaulted to 0 | Required nullable fields return 400 `missing_field` |
| Stale content returned 422 | It returns 409 |
| An unsafe flag crashed with the CLR unhandled-exception code | `[FATAL]` message and exit 78 |
| The test script killed processes by bare PID | Process objects, and a per-run folder |
| An ambient `HEKATE_PLAN_CONTRACT` leaked into the launcher's Api | The flag is always set explicitly (`0`/`1`); covered by Pester and Api check A |

## Known limits (unchanged by this increment)

- **Trust boundary:** anyone with full database credentials can open the fence. It protects integrity for well-behaved code paths and is not authentication.
- **Projection:** identity only (node id and type, `plan_root` tag). It is coupled to AGE availability: writes fail closed while AGE is broken.
- **Deferred:** legacy plan enrollment, the execution ledger link, UI, auth, a container review/descope operation, and a contract delete.
