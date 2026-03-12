# Orchestration Engine

AI-powered task orchestration: takes requirements, generates plans via Claude, decomposes into prioritized tasks, and executes them in parallel with budget controls.

## Quick Start

```bash
# Backend
pip install -r requirements.txt
cp config.example.json config.json   # Edit with your settings
python run.py                        # http://localhost:5200

# Frontend (dev)
cd frontend && npm install && npm run dev   # http://localhost:5173

# Frontend (production) — served by FastAPI
cd frontend && npm run build                # builds to frontend/dist/

# Tests
pip install -r requirements-dev.txt
python -m pytest tests/                     # run all backend tests
python -m pytest tests/ --cov=backend       # with coverage report
cd frontend && npm test                     # run frontend tests

# Docker
docker build -t orchestration .
docker run -p 5200:5200 -v ./config.json:/app/config.json orchestration
```

## Project Structure

| Path | Role |
|------|------|
| `run.py` | Uvicorn launcher |
| `config.json` | All settings (gitignored) |
| `config.example.json` | Config template with all options |
| `backend/app.py` | FastAPI app, lifespan, CORS, rate limiting, routers |
| `backend/rate_limit.py` | Shared slowapi limiter instance |
| `backend/config.py` | Config loader, constants, `validate_config()` startup checks |
| `backend/container.py` | dependency-injector `DeclarativeContainer` |
| `backend/exceptions.py` | Typed exception hierarchy (`NotFoundError`, `BudgetExhaustedError`, `GitError`, etc.) |
| `backend/logging_config.py` | Structured logging setup |
| `backend/db/connection.py` | Async SQLite (aiosqlite, WAL mode) |
| `backend/db/migrate.py` | Programmatic Alembic migration runner |
| `backend/db/models_metadata.py` | SQLAlchemy Table definitions for Alembic |
| `backend/migrations/` | Alembic migration versions |
| `backend/mcp/server.py` | FastMCP stdio server for Claude Code integration |
| `backend/mcp/config.example.json` | MCP server config template |
| `backend/middleware/auth.py` | JWT + API key auth dependencies (Bearer, admin, SSE token) |
| `backend/models/` | Pydantic schemas, status enums |
| `backend/routes/auth.py` | Register, login, refresh, me endpoints |
| `backend/routes/checkpoints.py` | Checkpoint list, get, resolve endpoints |
| `backend/routes/admin.py` | Admin-only user management, system stats |
| `backend/routes/analytics.py` | Admin-only analytics (cost, outcomes, efficiency) |
| `backend/routes/external.py` | External task execution (claim, submit, release) |
| `backend/routes/rag.py` | Read-only RAG database inspection endpoints |
| `backend/routes/` | REST endpoints (projects, tasks, usage, services, events) |
| `backend/services/auth.py` | Password hashing, JWT encode/decode, SSE tokens, user management |
| `backend/services/planner.py` | Claude-powered plan generation |
| `backend/services/decomposer.py` | Plan → task rows + dependency DAG (wave computation, cycle detection) |
| `backend/services/executor.py` | Async worker pool, wave dispatch, recovery, tick loop |
| `backend/services/task_lifecycle.py` | Task execution, verification, checkpoints, context forwarding |
| `backend/services/claude_agent.py` | Claude API task runner with multi-turn tool support |
| `backend/services/ollama_agent.py` | Ollama task runner |
| `backend/services/verifier.py` | Post-completion output verification via Haiku |
| `backend/services/knowledge_extractor.py` | Post-completion knowledge extraction via Haiku |
| `backend/services/budget.py` | Spending tracking, limit enforcement |
| `backend/services/model_router.py` | Model tier selection, cost calculation |
| `backend/services/git_service.py` | Stateless git operations via subprocess + asyncio.to_thread |
| `backend/services/resource_monitor.py` | Health checks (Ollama, ComfyUI, Claude) |
| `backend/services/progress.py` | SSE broadcast, event persistence |
| `backend/tools/registry.py` | Injectable `ToolRegistry` class |
| `backend/tools/` | Tool implementations (RAG, Ollama, ComfyUI, file) |
| `frontend/` | React 19 + TypeScript + Vite UI (ErrorBoundary, 404 page) |
| `Dockerfile` | Multi-stage build (frontend + backend) |
| `.github/workflows/ci.yml` | GitHub Actions CI (tests, lint, frontend build+test, E2E) |
| `tests/` | pytest suite (unit, integration, E2E) |
| `data/orchestration.db` | SQLite database (auto-created) |

## Deep-Dive Docs

| Topic | Location |
|-------|----------|
| Architecture | [.claude/architecture.md](.claude/architecture.md) |
| Prompt Engineering | [.claude/prompt-engineering.md](.claude/prompt-engineering.md) |

## Key Conventions

- **Config**: all values in `config.json`, never hardcoded. `validate_config()` runs at startup.
- **DI Container**: `backend/container.py` wires all singletons; routes use `@inject` + `Depends(Provide[...])`
- **Database**: async SQLite via aiosqlite, WAL mode, all access via `Database` class
- **Migrations**: Alembic manages schema; `Database.init(run_migrations=True)` in production, inline schema in tests
- **Auth**: JWT Bearer tokens for REST, API keys (`orch_` prefix) for MCP/external executors, short-lived SSE tokens for EventSource. First registered user becomes admin.
- **Ownership**: projects have `owner_id`. Users see/modify only their own projects. Admins can access all.
- **Budget**: every API call recorded in `usage_log`, checked against limits before execution. Budget endpoints are admin-only.
- **Models**: Ollama (free) for simple tasks, Haiku ($) for medium, Sonnet ($$) for complex
- **Tools**: registered in `ToolRegistry` class, injected via DI container
- **SSE**: short-lived token via `POST /api/events/{project_id}/token`, then stream via `GET /api/events/{project_id}?token=...`
- **Health probe**: `GET /api/health` — unauthenticated, returns `{"status": "ok"}` for Docker/k8s liveness checks
- **Rate limiting**: slowapi (shared instance in `rate_limit.py`), default 60/minute, 5/minute on plan generation
- **Exceptions**: typed hierarchy in `backend/exceptions.py` — routes map specific exceptions to HTTP status codes
- **Validation**: Pydantic `Field` constraints on all mutable schemas (min/max length, ge/le bounds)
- **Waves**: tasks decomposed into waves by dependency depth; executor dispatches one wave at a time
- **Context forwarding**: completed task output injected into dependents' `context_json` automatically
- **Knowledge persistence**: post-completion Haiku extraction of reusable findings (constraints, decisions, gotchas) into `project_knowledge` table; injected into all subsequent tasks' system prompts
- **Verification**: optional post-completion check via Haiku (PASSED/GAPS_FOUND/HUMAN_NEEDED outcomes)
- **Checkpoints**: retry-exhausted tasks create structured checkpoints for human resolution
- **Traceability**: requirements numbered [R1], [R2], mapped to tasks; coverage endpoint shows gaps
- **External execution**: MCP server (`backend/mcp/server.py`) for Claude Code integration. Execution modes: auto (engine-only), hybrid (Ollama internal, Claude external), external (all external). Tasks claimed atomically via CAS, results submitted with cost tracking.
- **Git integration**: optional per-project (`repo_path` nullable). `GitService` wraps subprocess via `asyncio.to_thread()`. Config in `git.*` section. Phase 1 (foundation) complete; execution wiring (Phase 2+) pending.
- **Tests**: Backend: pytest-asyncio (auto mode), 797 tests. Frontend: vitest + @testing-library/react, 211 tests. Load tests: 7 (excluded from CI via `slow` marker)

## Git Workflow

| Setting | Value |
|---------|-------|
| **workflow** | `pr` |
| **base_branch** | `main` |
| **branch_protection** | `yes` |
| **ci_gate** | `required` |
| **squash_merge** | `yes` |

CI must pass (lint + tests + frontend) before the PR is ready. See `.github/workflows/ci.yml`.

## Dependencies

```
# Runtime
fastapi, uvicorn, httpx, anthropic, pydantic
aiosqlite, alembic, sqlalchemy
dependency-injector
PyJWT, bcrypt, email-validator
slowapi

# Dev
pytest, pytest-asyncio, pytest-cov
```

## Environment

- Python 3.11+, Node.js 18+
- ANTHROPIC_API_KEY env var required
- Ollama at localhost:11434 and 192.168.1.164:11434
- ComfyUI at localhost:8188 and 192.168.1.164:8188
- RAG DBs at noz-rag/data/ and verse-rag/data/
