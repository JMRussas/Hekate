# Orchestration Engine

> **Status: Prototype / Active Development**
> APIs, database schemas, and behavior may change without notice.

An AI task runner: you describe what you want, it generates a structured plan, you approve/edit it, then it executes the work as a dependency DAG with parallel workers, model-tier routing, budget enforcement, and real-time SSE progress.

It's built for workflows where "agent magic" isn't acceptable — you want the plan explicit, execution traceable, and spend controllable.

## What Makes It Different

Most AI coding tools behave like a single-threaded chat. This engine:

- **Decomposes requirements into a task graph** (with cycle detection)
- **Routes each task to the cheapest capable tier** (local Ollama → Haiku → Sonnet)
- **Enforces budget with reservation** (so concurrency doesn't accidentally overspend)
- **Streams a full event timeline over SSE** and persists the audit trail
- **Builds cumulative project knowledge** — tasks learn from what earlier tasks discovered

### Core Loop

1. **Plan** — Claude generates a plan JSON (tasks + deps + metadata)
2. **Review** — You edit priorities, model tiers, descriptions, then approve
3. **Execute** — Async executor runs tasks in parallel where the DAG allows
4. **Observe** — SSE emits task/budget events in real time

```
Requirements ──→ Claude Sonnet ──→ Plan JSON ──→ User Approval ──→ Task DAG
                  (planning)        (structured)   (review/edit)       │
                                                                      ▼
                                                    ┌─── Executor Loop ───┐
                                                    │ Check budget        │
                                                    │ Resolve dependencies│
                                                    │ Route to model tier │
                                                    │ Execute + tools     │
                                                    │ Verify output       │
                                                    │ Extract knowledge   │
                                                    │ Emit SSE events     │
                                                    └─────────────────────┘
```

### Local-First, Config-Driven

Everything is driven from `config.json` — hosts, budgets, model tiers, auth secret, timeouts. No cloud DB required; default persistence is SQLite (WAL mode).

## Features

### Knowledge Persistence

After each task completes, a lightweight model (Haiku) extracts reusable findings — constraints discovered, decisions made, gotchas encountered — into a `project_knowledge` store. These are automatically injected into the system prompts of all subsequent tasks in the same project.

The result: later tasks benefit from what earlier tasks learned. A task that discovers "the API requires ISO 8601 dates" saves that finding, and every downstream task sees it without needing to rediscover it. Most agent frameworks treat tasks as isolated; this engine builds cumulative project context.

### Model Routing

Each task routes to the cheapest model that can handle it:

| Complexity | Model | Cost |
|-----------|-------|------|
| Simple | Ollama (local) | Free |
| Medium | Haiku | $1 / $5 per MTok |
| Complex | Sonnet | $3 / $15 per MTok |
| Planning | Sonnet | $3 / $15 per MTok |

### Budget System

- **Reserve before execute** — TOCTOU-safe reservation prevents overspend under concurrency
- **Three-tier limits** — Daily, monthly, and per-project caps
- **Per-round checks** — Mid-tool-loop budget verification with graceful partial results
- **Full cost tracking** — Every API call recorded with provider, model, tokens, and USD cost

### Verification + Checkpoints

Optional post-completion verification via Haiku checks task output quality. Three outcomes: PASSED, GAPS_FOUND (auto-retry with feedback), or HUMAN_NEEDED (escalated for review). When a task exhausts retries, it creates a structured checkpoint for human resolution instead of silently failing.

### Planning Rigor

Three levels of planning depth, selected per project:

| Level | When | What you get |
|-------|------|-------------|
| **L1 — Quick** | Bug fixes, small tweaks | Flat task list, 4K tokens |
| **L2 — Standard** | Features, refactors | Phased plan, open questions, 6K tokens |
| **L3 — Thorough** | Security, migrations | L2 + risk assessment, test strategy, 8K tokens |

### Tool System

Tools are Python classes registered via an injectable `ToolRegistry`. Each task can request any combination:

| Tool | What it does |
|------|-------------|
| `search_knowledge` | Semantic RAG search (Ollama embeddings + cosine similarity) |
| `lookup_type` | Exact keyword/type lookup via FTS5 full-text search |
| `local_llm` | Free local inference via Ollama (drafts, summaries, sub-tasks) |
| `generate_image` | Image generation via ComfyUI workflow submission + polling |
| `read_file` / `write_file` | Sandboxed file I/O scoped to project workspace |

## Comparison

| | Orchestration Engine | CrewAI | AutoGen | LangGraph |
|---|---|---|---|---|
| **Plan review** | Explicit approve/edit before execution | No plan stage | No plan stage | User-defined graph, no approval step |
| **Execution model** | DAG with waves, parallel workers, retries | Sequential/hierarchical agents | Multi-agent conversation | State machine graph |
| **Cost control** | Per-task model routing, budget reservation, 3-tier limits | Manual per-agent | Manual per-agent | Manual |
| **Observability** | Real-time SSE, per-task cost, event replay | Callbacks | Print-based logging | LangSmith integration |
| **Knowledge sharing** | Auto-extracted findings injected into subsequent tasks | Shared memory (manual) | Shared context (manual) | State passing (manual) |
| **Infrastructure** | SQLite + Ollama, no cloud DB, self-hosted | Cloud APIs | Cloud APIs | Cloud APIs + LangSmith |

## Best Fit

**Good for:**
- Local or self-hosted agent workflows where cost control and human review matter
- Teams building internal automation that needs a traceable run history
- Projects where you want structured execution (DAG, retries, verification) instead of open-ended chat
- Workflows mixing free local models with paid cloud models based on task complexity

**Not designed for:**
- Real-time conversational agents
- High-throughput multi-user production (SQLite single-writer limits)
- Tasks that need a single long-running context window instead of decomposed subtasks

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- `ANTHROPIC_API_KEY` environment variable
- [Ollama](https://ollama.com) running locally (optional, for free-tier tasks)

### Backend

```bash
pip install -r requirements.txt
cp config.example.json config.json   # Edit with your settings — especially auth.secret_key
python run.py                        # http://localhost:5200
```

### Frontend (development)

```bash
cd frontend && npm install && npm run dev   # http://localhost:5173
```

### Frontend (production — served by FastAPI)

```bash
cd frontend && npm run build   # outputs to frontend/dist/, served automatically
```

### Docker

```bash
docker build -t orchestration .
docker run -p 5200:5200 \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -v ./config.json:/app/config.json \
  orchestration
```

### Tests

```bash
# Backend (443 tests, 86% coverage)
pip install -r requirements-dev.txt
python -m pytest tests/                     # all tests
python -m pytest tests/ --cov=backend       # with coverage

# Frontend (135 tests)
cd frontend && npm test
```

## Hello World — UI Walkthrough

This walkthrough demonstrates the core loop: plan approval, DAG execution, and live SSE progress.

### 0. Start the services

```bash
# Terminal 1 — backend
pip install -r requirements.txt
cp config.example.json config.json   # set auth.secret_key at minimum
export ANTHROPIC_API_KEY=...
python run.py                        # http://localhost:5200

# Terminal 2 — frontend
cd frontend && npm install && npm run dev   # http://localhost:5173
```

### 1. Register and log in

Open `http://localhost:5173`. Create an account — the first registered user becomes admin.

### 2. Create a project

Click **New Project** and enter:

- **Name:** `hello-world`
- **Requirements:**

  ```
  Create a hello-world deliverable:
  - Write hello_world.py that prints a greeting and today's date (ISO format).
  - Write RUN.md with exact steps to run it.
  - Summarize what you created in 2 bullets.
  ```

This naturally produces multiple tasks with dependencies (RUN.md depends on the script).

### 3. Generate a plan

Click **Plan**. When it appears:

- Verify tasks are discrete (code generation, run instructions, summary)
- Check that RUN.md depends on hello_world.py
- Optionally edit a task description (e.g., add "Use ISO 8601 date format")

### 4. Approve the plan

This is the "make it executable" step — approving decomposes the plan into concrete task rows with dependency edges.

Click **Approve**.

### 5. Execute and watch

Click **Execute** and watch the SSE timeline:

- Wave 0 tasks (no dependencies) start immediately in parallel
- Wave 1 tasks wait for their predecessors
- Each task shows: queued → running → completed with model used and cost

### 6. Inspect outputs

Click into each task to see its output. The knowledge persistence system will have extracted any reusable findings for future tasks in this project.

## Hello World — curl Walkthrough

The same flow via API, no frontend needed. Assumes the server is running on `http://localhost:5200`.

```bash
BASE=http://localhost:5200

# 1) Register (first user becomes admin)
curl -sS -X POST $BASE/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"hello@example.com","password":"password123","display_name":"Hello"}'

# 2) Login -> grab access token
TOKEN=$(curl -sS -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"hello@example.com","password":"password123"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 3) Create project
PROJECT_ID=$(curl -sS -X POST $BASE/api/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name":"hello-world",
    "requirements":"Create a hello-world deliverable:\n- Write hello_world.py that prints a greeting and today'\''s date in ISO format.\n- Write RUN.md with exact steps to run it.\n- Summarize what you created in 2 bullets.",
    "planning_rigor":"L2"
  }' | python -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Project: $PROJECT_ID"

# 4) Trigger plan
curl -sS -X POST $BASE/api/projects/$PROJECT_ID/plan \
  -H "Authorization: Bearer $TOKEN"

# 5) Get latest plan id
PLAN_ID=$(curl -sS $BASE/api/projects/$PROJECT_ID/plans \
  -H "Authorization: Bearer $TOKEN" \
  | python -c "import sys,json; print(json.load(sys.stdin)[0]['id'])")

# 6) Approve plan (decompose into tasks)
curl -sS -X POST $BASE/api/projects/$PROJECT_ID/plans/$PLAN_ID/approve \
  -H "Authorization: Bearer $TOKEN"

# 7) Start execution
curl -sS -X POST $BASE/api/projects/$PROJECT_ID/execute \
  -H "Authorization: Bearer $TOKEN"

# 8) SSE token + stream URL
SSE_TOKEN=$(curl -sS -X POST $BASE/api/events/$PROJECT_ID/token \
  -H "Authorization: Bearer $TOKEN" \
  | python -c "import sys,json; print(json.load(sys.stdin)['token'])")
echo "SSE stream: $BASE/api/events/$PROJECT_ID?token=$SSE_TOKEN"

# 9) List tasks (repeat until all completed)
curl -sS "$BASE/api/tasks/project/$PROJECT_ID" \
  -H "Authorization: Bearer $TOKEN" \
  | python -c "
import sys,json
for t in json.load(sys.stdin):
    print(f\"{t['status']:12s} {t['title']}\")
"
```

## Architecture

```
backend/
├── app.py              FastAPI app, lifespan, CORS, rate limiting
├── config.py           Config loader (all values from config.json, never hardcoded)
├── container.py        dependency-injector DI container
├── exceptions.py       Typed exception hierarchy
├── rate_limit.py       Shared slowapi limiter instance
├── db/
│   ├── connection.py   Async SQLite (aiosqlite, WAL mode, transactions)
│   ├── migrate.py      Programmatic Alembic runner
│   └── models_metadata.py  SQLAlchemy Table defs for Alembic autogenerate
├── migrations/         Alembic migration versions
├── middleware/
│   └── auth.py         JWT auth (Bearer tokens + short-lived SSE tokens)
├── models/             Pydantic schemas + status enums
├── routes/             REST endpoints
├── services/
│   ├── planner.py          Claude-powered plan generation
│   ├── decomposer.py       Plan → task rows + dependency DAG (cycle detection, wave computation)
│   ├── executor.py          Async worker pool with retry backoff and graceful shutdown
│   ├── claude_agent.py      Claude API runner with multi-turn tool loop
│   ├── ollama_agent.py      Ollama task runner
│   ├── task_lifecycle.py    Task execution, verification, checkpoints, context forwarding
│   ├── verifier.py          Post-completion output verification
│   ├── knowledge_extractor.py  Post-completion knowledge extraction
│   ├── budget.py            Spend tracking and limit enforcement
│   ├── model_router.py      Model tier selection and cost calculation
│   ├── progress.py          SSE broadcast and event persistence
│   └── resource_monitor.py  Background health checks (Ollama, ComfyUI, Claude)
└── tools/              Tool implementations (RAG, Ollama, ComfyUI, file I/O)

frontend/               React 19 + TypeScript + Vite
├── src/api/            REST client with auto token refresh and 401 retry
├── src/components/     AuthGuard, ErrorBoundary, Modal, CopyButton
├── src/hooks/          useAuth, useSSE, useFetch, useClipboard, useTheme
└── src/pages/          Dashboard, ProjectDetail, TaskDetail, Usage, Services, Admin
```

### Key Design Decisions

- **Config-driven everything.** All hosts, models, budgets, timeouts, and auth settings come from `config.json`. Zero hardcoded values.
- **Dependency injection.** `dependency-injector` wires all singletons. Routes use `@inject` + `Depends(Provide[...])`. Tests override providers cleanly.
- **Async SQLite.** aiosqlite with WAL mode. Explicit transaction support with same-task nesting detection. Alembic migrations in production, inline schema in tests.
- **Budget reservation.** `reserve_spend()` acquires budget atomically before execution. Prevents two concurrent tasks from both spending past the limit.
- **Partial tool registration.** A broken ComfyUI import doesn't take down RAG search. Failed tools are logged and skipped; the rest load normally.
- **SSE via query-param tokens.** EventSource can't send headers, so SSE auth uses short-lived project-scoped tokens (60s TTL) issued via a dedicated endpoint.

## Configuration

All settings live in `config.json` (gitignored). See [config.example.json](config.example.json) for the full template.

| Section | Key settings |
|---------|-------------|
| `server` | Host, port, CORS origins, rate limit, log format/level |
| `anthropic` | Planning model, tier models (haiku/sonnet/opus), timeout, max concurrent |
| `ollama` | Local + server hosts, default model, embed model, timeouts |
| `comfyui` | Hosts, default checkpoint, timeout, poll interval |
| `rag` | Database paths, embedding dimensions, top-k defaults |
| `budget` | Daily / monthly / per-project USD limits, warning threshold |
| `execution` | Concurrent tasks, tick interval, tool rounds, retries, verification, knowledge extraction |
| `model_pricing` | Per-model input/output cost per million tokens |
| `auth` | JWT secret (min 32 chars), token expiry, registration toggle, OIDC providers |
| `git` | Commit author, branch prefix, auto-PR, command timeout |

## API

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/health` | GET | No | Liveness probe |
| `/api/auth/register` | POST | No | Create account (first user becomes admin) |
| `/api/auth/login` | POST | No | Get access + refresh tokens |
| `/api/auth/refresh` | POST | No | Rotate tokens |
| `/api/auth/me` | GET | Bearer | Current user profile |
| `/api/auth/oidc/providers` | GET | No | List configured OIDC providers |
| `/api/auth/oidc/{provider}/login` | GET | No | Start OIDC login flow |
| `/api/auth/oidc/{provider}/callback` | POST | No | OIDC callback |
| `/api/auth/oidc/link/{provider}` | POST/DELETE | Bearer | Link/unlink OIDC identity |
| `/api/auth/oidc/identities` | GET | Bearer | List linked identities |
| `/api/projects` | CRUD | Bearer | Project management |
| `/api/projects/{id}/plan` | POST | Bearer | Generate plan via Claude |
| `/api/projects/{id}/plans` | GET | Bearer | List plan versions |
| `/api/projects/{id}/plans/{pid}/approve` | POST | Bearer | Approve plan, create tasks |
| `/api/projects/{id}/execute` | POST | Bearer | Start execution |
| `/api/projects/{id}/pause` | POST | Bearer | Pause execution |
| `/api/projects/{id}/cancel` | POST | Bearer | Cancel project |
| `/api/projects/{id}/clone` | POST | Bearer | Clone project with tasks |
| `/api/projects/{id}/export` | GET | Bearer | Export project as JSON |
| `/api/projects/{id}/coverage` | GET | Bearer | Requirement coverage report |
| `/api/projects/{id}/knowledge` | GET | Bearer | List extracted knowledge |
| `/api/projects/{id}/knowledge/{fid}` | DELETE | Bearer | Delete a knowledge finding |
| `/api/tasks/project/{id}` | GET | Bearer | List tasks (filterable, sortable) |
| `/api/tasks/{id}` | GET/PATCH | Bearer | Task detail / edit |
| `/api/tasks/{id}/retry` | POST | Bearer | Retry a failed task |
| `/api/tasks/{id}/cancel` | POST | Bearer | Cancel a running task |
| `/api/tasks/{id}/review` | POST | Bearer | Approve or retry a needs_review task |
| `/api/tasks/bulk` | POST | Bearer | Bulk retry/cancel |
| `/api/checkpoints/project/{id}` | GET | Bearer | List checkpoints |
| `/api/checkpoints/{id}` | GET | Bearer | Checkpoint detail |
| `/api/checkpoints/{id}/resolve` | POST | Bearer | Resolve checkpoint (retry/skip/fail) |
| `/api/usage/summary` | GET | Bearer | Usage summary |
| `/api/usage/budget` | GET | Admin | Budget status |
| `/api/usage/daily` | GET | Admin | Daily usage breakdown |
| `/api/usage/by-project` | GET | Admin | Per-project usage |
| `/api/services` | GET | Bearer | External service health |
| `/api/services/{id}` | GET | Bearer | Single service detail |
| `/api/events/{id}/token` | POST | Bearer | Get SSE token (60s TTL) |
| `/api/events/{id}?token=...` | GET | Token | Real-time SSE stream |
| `/api/rag/databases` | GET | Bearer | List RAG databases |
| `/api/rag/databases/{name}/sources` | GET | Bearer | List sources in a RAG DB |
| `/api/rag/databases/{name}/documents` | GET | Bearer | List documents in a RAG DB |
| `/api/admin/users` | GET | Admin | List all users |
| `/api/admin/users/{id}` | PATCH | Admin | Update user (role, active status) |
| `/api/admin/stats` | GET | Admin | System-wide statistics |

## Development

PR-based workflow with CI gate. See [.github/workflows/ci.yml](.github/workflows/ci.yml).

```bash
# Run backend tests
python -m pytest tests/ --cov=backend

# Run frontend tests
cd frontend && npm test

# Lint
ruff check backend/
```

## Limitations

- **SQLite single-writer** — WAL mode allows concurrent reads, but writes are serialized. Fine for small-to-medium workloads; not designed for high-throughput multi-user.
- **In-memory embeddings** — RAG tool loads embedding matrices into memory. Practical up to ~50k chunks.
- **No incremental re-indexing** — RAG databases are built externally (via [mcp-rag](https://github.com/JMRussas/mcp-rag)).
- **No persistent file storage** — Task outputs are stored in SQLite text columns, not as files. Large outputs should use the file tools.
- **Single Ollama instance per tier** — Falls back to first available host, no load balancing.

## Tech Stack

**Backend:** Python 3.11+ · FastAPI · aiosqlite · Alembic · dependency-injector · anthropic SDK · httpx · PyJWT · bcrypt · slowapi · authlib

**Frontend:** React 19 · TypeScript · Vite · react-router-dom · vitest

**Infrastructure:** SQLite (WAL mode) · [Ollama](https://ollama.com) · ComfyUI

## License

[AGPL-3.0-or-later](LICENSE)
