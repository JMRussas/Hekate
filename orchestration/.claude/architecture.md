# Orchestration Engine - Architecture

## Core Workflow

```
Requirements → Claude Sonnet → Plan JSON → User Approval → Task Rows + DAG
                                                                  ↓
                                            ┌─────── Executor Loop ────────┐
                                            │ 1. Check budget limits       │
                                            │ 2. Find ready tasks (deps met)│
                                            │ 3. Check resource health     │
                                            │ 4. Route to model tier       │
                                            │ 5. Build small context       │
                                            │ 6. Call Claude/Ollama + tools│
                                            │ 7. Record usage + cost       │
                                            │ 8. Store output, emit SSE    │
                                            └──────────────────────────────┘
```

## Dependency Injection

All services wired via `dependency-injector` in `backend/container.py`:

```
Container
├── db: Database               (Singleton — async SQLite via aiosqlite)
├── http_client: httpx.AsyncClient (Singleton — shared HTTP client)
├── rag_cache: RAGIndexCache   (Singleton — lazy-loaded embedding indexes)
├── tool_registry: ToolRegistry (Singleton — injectable tool set)
├── auth: AuthService          (Singleton — JWT + bcrypt)
├── oidc: OIDCService          (Singleton — OIDC provider auth)
├── budget: BudgetManager      (Singleton — spend tracking)
├── progress: ProgressManager  (Singleton — SSE broadcast)
├── resource_monitor: ResourceMonitor (Singleton — health checks)
├── git_service: GitService    (Factory — stateless git operations)
├── planner: PlannerService    (Factory — Claude plan generation)
├── decomposer: DecomposerService (Factory — plan → tasks + DAG)
└── executor: Executor         (Singleton — async worker pool)
```

Routes use `@inject` + `Depends(Provide[Container.xxx])` to receive dependencies.
Tests override providers: `container.db.override(providers.Object(test_db))`.

## Database

- **Engine**: async SQLite via `aiosqlite` (WAL mode, foreign keys)
- **Schema management**: Alembic migrations in `backend/migrations/versions/`
- **Dual-mode init**: `Database.init(run_migrations=True)` for production (runs Alembic), `False` for tests (applies inline schema)
- **Pre-Alembic detection**: `migrate.py` checks for existing tables without `alembic_version` and stamps them

### Schema (10+ tables)

- **users**: id, email, password_hash, display_name, role, is_active
- **projects**: top-level container (owner_id → users, git columns: repo_path, git_base_branch, git_project_branch, git_worktree_path, git_state_json)
- **plans**: Claude-generated plan JSON (versioned)
- **tasks**: individual work units with model_tier, tools, context, output, git_branch, git_commit_sha
- **task_deps**: dependency DAG edges
- **usage_log**: every API call (provider, model, tokens, cost)
- **budget_periods**: aggregated daily/monthly spend
- **task_events**: fine-grained event log for SSE
- **checkpoints**: retry-exhausted task escalation for human resolution
- **user_identities**: OIDC provider links (multi-provider per user)
- **api_keys**: hashed API keys for programmatic access (models_metadata only — not yet in inline schema)

## Authentication

- **Passwords**: bcrypt hashing (direct, not passlib — passlib is incompatible with bcrypt 5.0)
- **Tokens**: PyJWT with HS256. Access tokens (30 min) + refresh tokens (7 days).
- **REST endpoints**: `Authorization: Bearer <token>` header via `HTTPBearer` scheme
- **SSE/EventSource**: token as query parameter (`?token=...`) since EventSource can't send headers
- **First user**: automatically gets `admin` role
- **Protected routes**: all routes except `/api/auth/*` require authentication via `get_current_user` dependency

### Auth Flow

```
Register → POST /api/auth/register → user row + 201
Login    → POST /api/auth/login    → {access_token, refresh_token, user}
Refresh  → POST /api/auth/refresh  → new {access_token, refresh_token}
Me       → GET  /api/auth/me       → user profile (protected)
```

### Frontend Auth

- Tokens in localStorage (`orch_access_token`, `orch_refresh_token`)
- `AuthProvider` context wraps app, auto-refreshes tokens every 25 min
- `AuthGuard` component redirects to `/login` if unauthenticated
- `authFetch` wrapper injects Bearer header, retries once on 401 after refresh

## Model Routing

| Task Type | Complexity | Model | Cost |
|-----------|-----------|-------|------|
| research | simple | Ollama | Free |
| analysis | simple | Ollama | Free |
| asset | any | Ollama + ComfyUI | Free |
| code | simple | Haiku | $1/5 per MTok |
| code | medium+ | Sonnet | $3/15 per MTok |
| integration | any | Haiku | $1/5 per MTok |
| planning | — | Sonnet | $3/15 per MTok |

## Tool System

Tools are Python classes in `backend/tools/`, registered via `ToolRegistry` (injectable DI singleton). Each has:
- `name`, `description`, `parameters` (JSON Schema)
- `async execute(params) -> str`

Available tools:
- `search_knowledge`: semantic RAG search (direct SQLite, Ollama embedding)
- `lookup_type`: FTS5 keyword RAG lookup
- `local_llm`: Ollama generate (free local inference)
- `generate_image`: ComfyUI workflow submission + polling
- `read_file` / `write_file`: sandboxed to `data/projects/{id}/`

## Budget System

- Pre-call check: `budget.can_spend(estimated_cost)` before every API call
- Post-call record: actual tokens → usage_log + budget_periods
- Limits: daily, monthly, per-project (configurable in config.json)
- Hard stop at 100%, warning at 80%

## Rate Limiting

- Shared `limiter` instance in `backend/rate_limit.py` (avoids circular imports between app and routes)
- Default: configurable via `server.rate_limit` (default `60/minute`)
- Plan generation endpoint: `5/minute` (expensive Claude call)

## Health Check

- `GET /api/health` — unauthenticated, lightweight liveness probe
- Returns `{"status": "ok"}` with 200 if the app is running
- Used by Docker `HEALTHCHECK` and k8s liveness probes
- Does NOT check external resources (Ollama, ComfyUI) — use `/api/services` for that

## Resource Monitoring

Background task (30s interval) health-checks:
- Ollama: GET /api/tags (verifies models loaded)
- ComfyUI: GET /system_stats
- Claude API: checks ANTHROPIC_API_KEY presence

Tasks that need unavailable resources stay in queue until next tick.

## Git Integration

Optional per-project feature. Projects with `repo_path` set produce git commits during execution.

**Status**: Phase 1 (foundation) complete. Phases 2-5 pending.

| Phase | What | Status |
|-------|------|--------|
| 1 | GitService + schema + migration | Done (PR #8) |
| 2 | Execution wiring (tasks → commits, branches, PR) | Pending |
| 3 | Git tools for agent awareness (status/diff/log) | Pending |
| 4 | REST API endpoints + frontend types | Pending |
| 5 | Worktree isolation (many projects → same repo) | Pending |

**GitService** (`backend/services/git_service.py`):
- Stateless — all git state on disk or in DB
- All methods: `asyncio.to_thread(subprocess.run(...))` with configurable timeout
- Factory provider in DI container (not singleton)
- Key methods: `validate_repo`, `create_branch`, `checkout`, `merge_branch`, `stage_and_commit`, `check_dirty`, `backup_dirty_state`, `create_worktree`, `push_branch`, `create_pr`

**Config** (`git.*` section):
- `enabled` (default true), `commit_author`, `branch_prefix` ("orch"), `non_code_output_path` (".orchestration"), `auto_pr`, `pr_remote`, `command_timeout` (30s)

**Schema additions** (migration 011):
- projects: `repo_path`, `git_base_branch`, `git_project_branch`, `git_worktree_path`, `git_state_json`
- tasks: `git_branch`, `git_commit_sha`

## Test Architecture

```
tests/
├── conftest.py                        # Shared fixtures (tmp_db, app_client, authed_client, mocks)
├── unit/
│   ├── test_model_router.py           (15 tests)
│   ├── test_budget.py                 (8 tests)
│   ├── test_decomposer.py            (8 tests)
│   ├── test_file_tool.py             (13 tests)
│   ├── test_auth.py                  (17 tests)
│   ├── test_progress.py              (9 tests)
│   ├── test_registry.py              (7 tests)
│   ├── test_executor_core.py         (31 tests)
│   ├── test_executor_hardening.py    (7 tests)
│   ├── test_rag_tools.py             (21 tests)
│   ├── test_ollama_tool.py           (8 tests)
│   ├── test_planner_service.py       (18 tests)
│   ├── test_progress_subscribe.py    (5 tests)
│   ├── test_resource_monitor.py      (15 tests)
│   ├── test_auth_middleware.py       (10 tests)
│   ├── test_app_lifespan.py          (4 tests)
│   ├── test_logging.py               (9 tests)
│   └── ...                           (remaining unit tests)
├── integration/
│   ├── test_auth_api.py              (12 tests)
│   ├── test_projects_api.py          (13 tests)
│   ├── test_tasks_api.py             (15 tests)
│   ├── test_usage_api.py             (5 tests)
│   ├── test_auth_gaps.py             (3 tests)
│   └── ...                           (remaining integration tests)
└── e2e/
    └── test_full_workflow.py          (2 tests)
```

**Backend (pytest):** 578 tests, 86% coverage (CI threshold: 80%)
- `tmp_db` fixture: fresh async Database with inline schema (no Alembic for speed)
- `app_client`: DI container overrides for db, auth, budget, progress, executor, resource_monitor
- `authed_client`: app_client + registered user + Bearer token header
- Claude/Ollama mocked in all tests (no real API calls)

**Frontend (vitest + @testing-library/react):** 137 tests across 21 files

```
frontend/src/
├── api/client.test.ts              (9 tests — authFetch, token injection, 401 retry)
├── api/auth.test.ts                (11 tests — login, register, refresh, dedup)
├── components/AuthGuard.test.tsx   (3 tests — loading, redirect, render)
├── components/ErrorBoundary.test.tsx (2 tests — fallback UI)
├── components/Layout.test.tsx      (4 tests — nav links, user info, logout)
├── hooks/useFetch.test.ts          (5 tests — success, error, refetch, deps)
├── hooks/useAuth.test.tsx          (6 tests — mount, login, logout, refresh)
├── hooks/useSSE.test.ts            (7 tests — connect, events, cleanup)
├── pages/Dashboard.test.tsx        (10 tests — projects, budget, create flow)
├── pages/ProjectDetail.test.tsx    (17 tests — status, actions, waves, coverage)
├── pages/TaskDetail.test.tsx       (14 tests — info, review, verification)
├── pages/Services.test.tsx         (6 tests — list, refresh, status badges)
├── pages/Usage.test.tsx            (9 tests — budget, tables, progress bars)
├── pages/Login.test.tsx            (5 tests — form, submit, error, link)
├── pages/Register.test.tsx         (5 tests — form, submit, error, link)
├── pages/NotFound.test.tsx         (2 tests — 404, dashboard link)
└── App.test.tsx                    (3 tests — routes, 404)
```

- jsdom environment via vitest
- Global fetch mocked per test
- localStorage mocked via jsdom
- Setup file loads @testing-library/jest-dom matchers

## Dependency Map

| File | Role | Depends On | Used By |
|------|------|-----------|---------|
| config.py | Config loader | config.json | Everything |
| rate_limit.py | Shared limiter | config | app.py, routes/projects |
| container.py | DI container | config, all services | app.py, routes |
| db/connection.py | Async SQLite | aiosqlite, config | All services, routes |
| db/migrate.py | Alembic runner | alembic, models_metadata | db/connection.py |
| middleware/auth.py | JWT auth deps | services/auth, container | app.py, routes |
| models/enums.py | Status enums | — | schemas, services, routes |
| models/schemas.py | Pydantic models | enums | Routes |
| services/auth.py | Auth service | db, config, bcrypt, PyJWT | middleware, routes/auth |
| services/planner.py | Plan generation | config, budget, model_router | routes/projects |
| services/decomposer.py | Plan → tasks | db, model_router | routes/projects |
| services/executor.py | Task execution | db, budget, model_router, tools, progress, resource_monitor | app.py |
| services/git_service.py | Git operations | config, exceptions, db | container, git_lifecycle (Phase 2) |
| services/budget.py | Cost tracking | db, config | executor, routes/usage |
| services/model_router.py | Model selection | config | planner, decomposer, executor |
| services/resource_monitor.py | Health checks | config | executor, routes/services |
| services/progress.py | SSE broadcast | db | executor, routes/events |
| tools/registry.py | Tool registry | tools/* | container, executor |
| tools/rag.py | RAG search | config, tools/base | registry |
| tools/ollama.py | Ollama client | config, tools/base | registry |
| tools/comfyui.py | ComfyUI client | config, tools/base | registry |
| tools/file.py | File I/O | config, tools/base | registry |

## Gotchas & Pitfalls

- Claude may return dependency indices as strings ("2") instead of ints. The decomposer handles both.
- Model IDs must match exactly what the Anthropic API accepts. Current valid IDs: `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-haiku-4-5-20251001`.
- **passlib 1.7.4 is incompatible with bcrypt 5.0.0**. Use `bcrypt.hashpw()`/`bcrypt.checkpw()` directly instead of `passlib.CryptContext`.
- aiosqlite runs SQLite on a background thread — no need for `threading.Lock` or `check_same_thread=False`.
- RAG tool SQLite connections still use sync sqlite3 directly (not via aiosqlite). These need `check_same_thread=False`.
- **Must use `AsyncAnthropic`** in all async functions (planner, executor). Using sync `Anthropic()` blocks the entire event loop and freezes SSE, health checks, and concurrent task dispatch.
- The executor marks tasks as QUEUED before dispatching and tracks dispatched task IDs in `_dispatched` set to prevent duplicate dispatch. Without this, the tick loop can re-dispatch the same PENDING task before the semaphore is acquired.
- Budget `record_spend()` uses `execute_many_write()` for atomic transactions — all three writes (usage_log, daily period, monthly period) commit together.
- RAG embedding matrices are loaded lazily on first use and cached in memory. First search will be slow.
- The `response.content` from Anthropic SDK (list of ContentBlock objects) can be passed directly back into messages — the SDK handles serialization.
- The executor uses a shared `AsyncAnthropic` client (created once at `start()`, closed at `stop()`). Do NOT create per-call clients — they leak connection pools.
- The executor tracks in-flight task handles in `_in_flight` set and cancels them on `stop()` for clean shutdown.
- Dead-project detection: if no tasks are PENDING/QUEUED/RUNNING and some are BLOCKED, the executor marks the project FAILED. This prevents projects stuck in EXECUTING forever.
- File tool sandbox uses `Path.is_relative_to()` for path traversal prevention — NOT `str.startswith()` which has prefix-collision vulnerabilities.
- **EventSource can't send headers** — SSE auth uses query-param token (`?token=...`) validated by `get_user_from_token_param`.
- **Alembic + SQLite**: Must use `render_as_batch=True` in `env.py` because SQLite doesn't support `ALTER TABLE DROP COLUMN` natively.
- **Test DB uses inline schema, not Alembic** — faster and avoids event loop issues. Keep inline `_SCHEMA` in `connection.py` in sync with migration files.
- `_load_config()` is private — module-level constants are snapshots. Do NOT call it after import; constants won't update.
- `validate_config()` checks bounds on port (1-65535), budget limits (>= 0), and timeouts (> 0) in addition to secret key length.
- `projects.owner_id` uses `ON DELETE SET NULL` — deleting a user nulls their projects (makes them admin-visible) instead of cascading delete.
- Rate limiter: shared instance in `backend/rate_limit.py` avoids circular imports between `app.py` ↔ `routes/projects.py`.
- Auth middleware: `_validate_token()` shared helper deduplicates JWT validation for Bearer and SSE token paths.
- Frontend refresh dedup: `_refreshPromise` module-level variable ensures concurrent 401s share one refresh call.
- **GitService uses subprocess, not gitpython** — all git ops via `subprocess.run` wrapped in `asyncio.to_thread()`. No git library dependency.
- **GitService is a Factory provider** (not Singleton) — stateless, safe to create multiple instances.
- **`subprocess.TimeoutExpired` does NOT inherit from `OSError`** — must catch separately in git subprocess calls.
- **Schema triple-sync**: git columns must be kept in sync across `connection.py` (inline `_SCHEMA`), `models_metadata.py` (Alembic), and migration files. Drift between these causes test failures or migration errors.
- **Migration revision IDs**: use short strings like `"011"`, not full descriptive names. `down_revision` must match the exact `revision` string of the parent migration.
- **`git status --short` unmerged codes**: 7 codes total (UU, AA, DD, AU, UA, DU, UD). Don't assume only UU/AA for conflict detection.
- **Multiple Claude sessions sharing a repo**: use git worktrees (`.worktrees/{feature}`) to isolate concurrent work. Without this, sessions change branches out from under each other.
