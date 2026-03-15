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
| `backend/services/claude_code_executor.py` | Claude Code CLI executor (stream-json, 10MB buffer) |
| `backend/services/generic_cli_executor.py` | Gemini CLI + Codex CLI executors (crash retry) |
| `backend/services/cli_common.py` | Shared prompt builder + cwd resolver for CLI executors |
| `backend/services/ollama_agent.py` | Ollama task runner |
| `backend/services/verifier.py` | Post-completion output verification via LLM |
| `backend/services/code_reviewer.py` | Senior-dev code review (review cycle) |
| `backend/services/knowledge_extractor.py` | Post-completion knowledge extraction |
| `backend/services/model_router.py` | Model tier selection, cost calculation, TIER_TO_PROVIDER |
| `backend/services/model_discovery.py` | Queries provider APIs at startup for available models |
| `backend/services/provider_quota.py` | Per-provider quota tracking and enforcement |
| `backend/services/context_store_client.py` | Shared httpx client for context store (circuit breaker) |
| `backend/services/enrichment_service.py` | Pre-dispatch context injection from context store |
| `backend/services/telemetry_feedback.py` | Post-completion execution outcome tracking |
| `backend/services/budget.py` | Spending tracking, limit enforcement |
| `backend/knowledge/` | Game dev knowledge base (platform docs, sprint patterns, code templates) |
| `backend/services/git_service.py` | Stateless git operations via subprocess + asyncio.to_thread |
| `backend/services/resource_monitor.py` | Health checks (Ollama, ComfyUI, Claude) |
| `backend/services/progress.py` | SSE broadcast, event persistence |
| `backend/tools/registry.py` | Injectable `ToolRegistry` class |
| `backend/tools/` | Tool implementations (RAG, Ollama, ComfyUI, file) |
| `frontend/` | React 19 + TypeScript + Vite UI (ErrorBoundary, 404 page) |
| `Dockerfile` | Multi-stage build (frontend + backend) |
| `.github/workflows/ci.yml` | GitHub Actions CI (tests, lint, frontend build+test, E2E) |
| `tests/` | pytest suite (unit, integration, E2E) |
| `data/orchestration.db` | SQLite database (auto-created, gitignored) |
| `tools/local_executor.py` | CLI task executor — claims tasks from DB, runs via claude/gemini/codex/ollama |
| `tools/supervisor.py` | Task monitor — detects failures (explicit + silent), auto-fixes or escalates |
| `tools/patch_hecate_plan.py` | One-off plan patcher — reassign tiers, reset stuck tasks, add review gates |

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
- **Models (CLI-first)**: CLI tiers are the default — `claude_code`, `gemini_cli`, `codex_cli` are subscription-billed ($0/call). Ollama for simple/free tasks. API tiers (haiku/sonnet/opus) available but route through Claude Code CLI when `ANTHROPIC_API_KEY` is not set. Model discovery queries all provider APIs at startup. Single source of truth: `TIER_TO_PROVIDER` in `model_router.py`.
- **Tools**: registered in `ToolRegistry` class, injected via DI container
- **SSE**: short-lived token via `POST /api/events/{project_id}/token`, then stream via `GET /api/events/{project_id}?token=...`
- **Health probe**: `GET /api/health` — unauthenticated, returns `{"status": "ok"}` for Docker/k8s liveness checks
- **Rate limiting**: slowapi (shared instance in `rate_limit.py`), default 60/minute, 5/minute on plan generation
- **Exceptions**: typed hierarchy in `backend/exceptions.py` — routes map specific exceptions to HTTP status codes
- **Validation**: Pydantic `Field` constraints on all mutable schemas (min/max length, ge/le bounds)
- **Waves**: tasks decomposed into waves by dependency depth; executor dispatches one wave at a time
- **Context forwarding**: completed task output injected into dependents' `context_json` automatically
- **Knowledge persistence**: post-completion Haiku extraction of reusable findings (constraints, decisions, gotchas) into `project_knowledge` table; injected into all subsequent tasks' system prompts
- **Verification**: optional post-completion check via LLM (PASSED/GAPS_FOUND/HUMAN_NEEDED outcomes). Uses `call_llm` (CLI/Ollama), not the Anthropic SDK directly.
- **Review cycle**: opt-in per-project (`config.review_cycle.enabled`). After verification passes: code reviewer analyzes scoped git diff → if bugs/security issues found, task re-executes with feedback (max 2 iterations) → if approved, auto-commits → at wave boundaries, creates PR. Config: `review_cycle.enabled`, `max_iterations`, `auto_commit`, `pr_on_wave_complete`.
- **Checkpoints**: retry-exhausted tasks create structured checkpoints for human resolution
- **Traceability**: requirements numbered [R1], [R2], mapped to tasks; coverage endpoint shows gaps
- **External execution**: MCP server (`backend/mcp/server.py`) for Claude Code integration. Execution modes: auto (engine-only), hybrid (Ollama internal, Claude external), external (all external). Tasks claimed atomically via CAS, results submitted with cost tracking.
- **Local executor**: `tools/local_executor.py` polls DB directly (no REST), claims and runs tasks via claude/gemini/codex CLI or Ollama. Designed for the 4090 dev machine.
- **Game dev projects**: Set `config.project_type = "game_dev"` and `config.platform` to activate game-aware planning. Supported platforms: `noz` (C#/NoZ engine), `highrise` (Lua/Highrise.game), `noz_continuing` (existing NoZ projects). Game dev adds task types: `game_design`, `game_content`, `game_code`, `game_ui`, `game_build_verify`. Platform knowledge docs in `backend/knowledge/platforms/` are injected into task context during decomposition and used for platform-aware verification. Sprint patterns in `backend/knowledge/sprint_recipes/`. Highrise code templates in `backend/knowledge/templates/highrise/`. Seed prior knowledge via `tools/seed_game_knowledge.py`.
- **Supervisor**: `tools/supervisor.py` monitors completed tasks for silent failures (sandbox blocked, no code written) and re-queues or escalates.
- **Git integration**: per-project via `repo_path` (the "working directory"). `GitService` wraps subprocess via `asyncio.to_thread()`. Review cycle auto-commits approved code and creates PRs at wave boundaries. The working directory is the primary anchor when creating a project.
- **Planning rigor**: L0 (roadmap — high-level epics), L1 (quick — flat task list), L2 (standard — phases + questions), L3 (thorough — phases + risk + test strategy). L0 epics decompose into research tasks; each can be expanded into a new L2 project via `POST /api/tasks/{id}/expand`.
- **Context store integration**: `context_store_client.py` provides a shared httpx client with circuit breaker (5 failures → 60s cooldown). Used by enrichment (pre-dispatch context injection) and telemetry (post-completion outcome tracking). Fails silently — never blocks task execution.
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
- ANTHROPIC_API_KEY env var optional — when not set, haiku/sonnet tasks route through Claude Code CLI
- CLI tools: `claude` (Claude Code), `gemini` (Gemini CLI), `codex` (Codex CLI) — all on PATH
- Ollama at localhost:11434 and 192.168.1.164:11434
- ComfyUI at localhost:8188 and 192.168.1.164:8188
- Context Store API at localhost:5102 (optional — enrichment/telemetry degrade gracefully)
- RAG DBs at noz-rag/data/ and verse-rag/data/
