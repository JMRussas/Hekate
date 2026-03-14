# Architecture

Read this when you need to understand the system design, component relationships, or data flow.

## Overview

Beethoven is a three-component AI agent platform:

- **Extension** (TypeScript) — VSCode fleet control center. Manages projects, monitors tasks, streams events.
- **Orchestration** (Python/FastAPI) — Task execution engine. Plans work via Claude, decomposes into dependency waves, dispatches to model tiers, verifies results.
- **Context Store** (C#/.NET) — Agent memory. Graph DB (AGE) + vector search (pgvector) for storing plans, conversations, and code indices.

## Data Flow

```
Extension (VSCode)
    │  fetch + Bearer token
    ▼
Orchestration API (:5200)
    ├── Projects, plans, task lifecycle (decompose → dispatch → execute → verify)
    ├── MCP server for external agents (Claude Code, Gemini, Codex)
    └── CLI executors for local task dispatch (tools/local_executor.py)
    │
    │  one-way seeding (tools/seed_hecate_roadmap.py)
    ▼
Context Store API (:5102)
    ├── PostgreSQL + AGE graph + pgvector embeddings
    ├── Plans, conversations, code indices
    └── Context router for agent memory retrieval
```

**Key:** Extension ↔ Orchestration are tightly coupled (REST + SSE). Context Store is decoupled — receives data via manual seeding, no runtime dependency from orchestration.

## Component Integration

### Extension → Orchestration (port 5200)

Client: `extension/src/api/client.ts` — native fetch with Bearer auth + auto-reconnect SSE.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/projects` | List projects |
| `GET /api/projects/{id}` | Project details + task summary |
| `POST /api/projects/{id}/start` | Start execution |
| `POST /api/projects/{id}/pause` | Pause execution |
| `GET/POST /api/tasks/{id}` | Task details, retry, cancel |
| `GET /api/services` | Service health status |
| `POST /api/internal/chat` | Chat messages |
| `POST /api/events/{id}/token` → `GET /api/events/{id}` | SSE event stream |

### MCP Server → Orchestration (port 5200)

Server: `orchestration/backend/mcp/server.py` — FastMCP stdio server, httpx client with Bearer auth.

Exposes tools for external agents: `create_project`, `plan_project`, `start_project`, `list_projects`, `project_status`, `list_tasks`, `next_task`, `claim_task`, `task_detail`, `submit_result`, `release_task`.

### Orchestration → Context Store (port 5102)

One-way via `orchestration/tools/seed_hecate_roadmap.py`. Creates projects and nodes in the graph DB. Not used during normal execution.

### CLI Executors (tools/)

Local task execution bypassing the REST API — direct SQLite access:

| Tool | Role |
|------|------|
| `tools/local_executor.py` | Claims tasks from DB, runs via claude/gemini/codex CLI or Ollama |
| `tools/supervisor.py` | Monitors tasks for failures (explicit + silent), re-queues or escalates |
| `tools/patch_hecate_plan.py` | One-off plan patches — reassign tiers, reset stuck tasks |

## Model Tiers

| Tier | Model | Cost | Use Case |
|------|-------|------|----------|
| `ollama` | qwen3.5:latest | Free (local) | Simple tasks |
| `haiku` | claude-haiku | $ | Medium tasks, verification, knowledge extraction |
| `sonnet` | claude-sonnet | $$ | Complex tasks |
| `opus` | claude-opus | $$$ | Critical tasks |
| `claude_code` | Claude Code CLI | Subscription | External execution (C#, complex multi-file) |
| `gemini_cli` | gemini-2.5-pro | Subscription | External execution (Python, RAG access) |
| `codex_cli` | gpt-5.4 | Subscription | External execution (web search) |

## Dependency Map

| Component | Depends On | Used By |
|-----------|-----------|---------|
| Extension | Orchestration API | User (VSCode) |
| Orchestration API | SQLite, Anthropic API, Ollama | Extension, MCP clients, CLI executors |
| CLI executors | SQLite (direct), claude/gemini/codex CLIs, Ollama | Manual / cron |
| MCP Server | Orchestration API (HTTP) | Claude Code sessions |
| Context Store API | PostgreSQL + AGE + pgvector | Seeding tools, direct queries |
| Supervisor | SQLite (direct) | Manual / cron |

## Gotchas & Pitfalls

- **DB path divergence**: Orchestration REST server creates DB at `orchestration/data/orchestration.db`, but CLI tools in `tools/` reference the same path via relative `Path(__file__).parent.parent / "data"`. If you run tools from a different CWD, the path resolves wrong.
- **CLI executor sandbox**: Claude Code runs in a sandbox — tasks that need file writes must have appropriate permissions configured in `config.json` executor settings.
- **Silent failures**: Tasks can report `status=completed` but actually produce no useful output (e.g., sandbox blocked writes). The supervisor tool detects these via pattern matching.
- **SSE token lifecycle**: SSE tokens are short-lived. The extension handles reconnection, but custom clients need to re-fetch tokens on disconnect.
