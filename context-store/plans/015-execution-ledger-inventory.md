# Plan 015 — Execution ledger inventory (source evidence)

**Status: read-only source inventory, 2026-10-06. Evidence only, not a design or approval.**
Accepted by codex-hekate as source evidence (msg 499).

**Method:** static reading of branch `feat/plan-nodes-migration` (after `9bc2cec`). Nothing was run and no database was queried.

**Labels:**
- **V** — verified in the cited code.
- **V★** — re-checked by claude-hekate.
- **I** — inferred.

Paths are relative to the repository root.

## 1. Which database the gods engine uses

- **V★ Selection order:**
  1. `--dsn`
  2. `ORCHESTRATION_DSN`
  3. `--db` or `ORCHESTRATION_DB`
  4. `orchestration/data/orchestration.db`

  Sources: `Odin/run_hekate.py:57-65`, `Odin/gods/api.py:74-81`. With a DSN the engine uses `asyncpg` and `PostgresDB`; otherwise `aiosqlite` and `SqliteDB` (`Odin/gods/engine.py:940-954`).
- **V** `Odin/run_pipeline.py` is SQLite only (`:30`, `:236`).
- **Unknown:** the DSN of the deployed `HekateEngine` service. `scripts/deploy_hekate.sh` sets none.
- **I:** the `--dsn` help example points at `localhost:5433/code_storage` (`run_hekate.py:32`), the same database context-store uses. Co-location is not confirmed.

## 2. Ledger tables and the Postgres name rewrite

- **V★** On Postgres, `_pg_rewrite` (`Odin/gods/engine.py:58-75`) regex-replaces the words `tasks`, `plans`, `projects` and `task_deps` with `engine_*` **anywhere in the SQL text**, string literals included.
- On Postgres the gods ledger is therefore `engine_tasks`, `engine_task_deps`, `engine_plans` and `engine_projects`. `god_relay_events` and `god_registry` keep their names and are shared.
- **V** The DDL is at `engine.py:335-457` (Postgres) and `:211-333` (SQLite).
- **V** `engine_tasks` is one **mutable** row per task:
  - `id`, `project_id`, `plan_id`, `status` (default `pending`), `context_json`
  - `output_text`, `output_artifacts_json`, `retry_count`, `max_retries` (3), `error`
  - `started_at`, `completed_at`, `wave`, `verification_status`, `verification_notes`
  - `git_branch`, `git_commit_sha`, `claimed_by`, `claimed_at`, `fork_group_id`, `branch_id`

  There are no foreign keys and no indexes beyond the primary keys.
- **V** The inspected gods task rows have no dedicated per-attempt history, and there is no attempt or run table in the gods DDL. Each retry overwrites `output_text` and `error`.
  - The monolith does have `checkpoints.attempts_json` (Alembic migration `005`). Its attempt-history semantics have **not** been fully audited here. The only per-attempt trace is relay idempotency keys such as `idem_key("hermes_complete", task_id, retry_count)` (`hermes_async.py:361`).
- **V** The monolith's Alembic schema (`orchestration/backend/migrations/versions/001-031`) defines a **different** `tasks` table. Its claim columns are at `014:45-46` and its git columns at `011:35-36`. `plan_nodes` (`030:23-38`) has a foreign key to `projects`, not `engine_projects`.

## 3. Task status and transitions

- **V** Status values (`Odin/gods/task_states.py:22-45`): `pending`, `blocked`, `queued`, `dispatched`, `running`, `completed`, `failed`, `cancelled`, `needs_review`.
- **V★** `transition_task` reads the current status, then runs an unconditional UPDATE. An invalid transition only logs a warning (`task_states.py:62-120`).
- **V** Flow:
  - Odin dispatch sets `model_tier` and emits `dispatch_command` (`handlers/odin.py:388-397`).
  - Hermes moves `pending`/`queued` to `running` with a read-then-write, not a compare-and-set (`hermes_async.py:136-143`), and sets `completed` or `failed` (`:323`, `:359-371`, `:383`, `:401`, `:419`).
  - Mimir sets `gaps_found` back to `pending` with `retry_count+1` (`mimir.py:500-506`, `:618-624`), or to `needs_review` (`:482`, `:599`, `:633`).
  - Startup recovery resets `running` to `pending` with `retry_count+1` (`engine.py:539-557`); so does `POST /api/tasks/{id}/retry` (`api.py:229-238`).
- **V** `git_commit_sha`, `claimed_by` and `output_artifacts_json` are never written in `Odin/gods`; they appear only in the DDL. The commit SHA travels only in the `project_committed` event payload (`hephaestus.py:247-268`).

## 4. Claims

- **V** Among the inspected paths, the only compare-and-set claim is the monolith's `/api/external` (`orchestration/backend/routes/external.py:171-176`, authenticated via `get_current_user`, `app.py:63`, `:334`). It acts on the monolith's `tasks` table, **not** `engine_tasks`.
- **V** The gods engine has no claim logic.

## 5. Existing links between plans and tasks (all weak)

| Link | Evidence | Weakness |
|---|---|---|
| `context_json.plan_node_id` | `Odin/gods/handlers/athena_complete.py:166-228` | Matched with `LIKE`, so prefixes can collide (`Odin/tests/test_failure_modes.py:641`). Reads SQLite `plan_nodes`. Probably fails on Postgres unless the monolith's migrations share the database (**I**). |
| context-store attribute `engine_task_id` | `context_bridge.py:171-181`, lookup `:221-242` | Unscoped scan. A replace-all attribute write wipes it, and since 2b1 it is fenced for managed nodes. |
| `context_json.plan_task_id` | `Odin/langgraph_engine/db.py:149` | LangGraph only, SQLite |

## 6. `Odin/langgraph_engine/`

- **V** Untracked. SQLite only. It writes the same `projects`, `plans`, `tasks` and `task_deps` tables in `orchestration.db`, tagged `{"engine":"langgraph"}`, using `INSERT OR REPLACE` (`db.py:34-38`, `:144-160`).
- **V** Its LangGraph checkpoints go to a separate `checkpoints.sqlite` (`run.py:50-52`). Nothing outside the folder imports it. Its README calls it a second, independent implementation; the gods pipeline is still the active engine (`README.md:3-6`).
- **Provenance: unknown.** No author or owner is recorded in headers or the README, and the folder is untracked, so there is no git history. It must not be treated as authoritative or adapted until its ownership is known.

## 7. Externally reachable APIs

| API | Location | Auth |
|---|---|---|
| Gods FastAPI, port 5200: projects, execute, decompose, `tasks/{id}/retry`, verify, review | `Odin/gods/api.py:123-308` | **None.** CORS `*` (`:103`). No claim or complete endpoint; unmatched paths return 501 (`:492`). |
| Prometheus MCP | `Odin/gods/prometheus_mcp.py:131-380` | Optional Bearer token. Wraps the gods API. `submit_plan_children` reaches a 501 route. |
| Monolith `/api/external/*`, `/api/tasks` | `orchestration/backend/routes/` | Authenticated. Writes the monolith's `tasks` table. |

## Risks for any link from plan nodes to execution

1. Two ledgers are named `tasks`. A reference must name its engine and table explicitly.
2. The inspected gods task rows have no dedicated per-attempt history. The monolith's `checkpoints.attempts_json` has not been audited.
3. Existing links use a JSON `LIKE`, or attributes that get clobbered.
4. The inspected gods engine paths have no durable compare-and-set claim.
5. `_pg_rewrite` can rewrite words inside any new gods SQL.
6. LangGraph would share SQLite tables if it were ever activated.
7. The gods API is unauthenticated.
8. The deployed engine's database is unverified.
