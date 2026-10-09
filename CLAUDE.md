# Hekate

Chat/context/provider work: read [CHAT-CONSOLIDATION.md](CHAT-CONSOLIDATION.md)
before adding overlapping functionality shared with Iris and ChatAgent.
For the current implementation contracts and acceptance matrix, follow
[CHATAGENT-INTEGRATION-HANDOFF.md](CHATAGENT-INTEGRATION-HANDOFF.md)
(ChatRuntime source checkpoint `4eb88fd`, updated 2026-09-28).

Unified AI agent platform: gods pipeline (event-driven task execution) + orchestration backend (API + DB + dashboard) + context store (agent memory) + admin service (infra). Six components, one repo.

## Execution Architecture

**Gods pipeline is the active execution engine.** The monolith orchestration services (executor.py, task_lifecycle.py, sentinel/) are legacy — still deployed but replaced.

```
Project created → Athena plans (Claude via LLM Gateway, L1) → Odin dispatches
  → Hermes executes (Claude Code CLI, async) → Mimir verifies (Claude agent or gateway)
  → Odin manages lifecycle (unblock deps → wave progression → complete)
```

Key files: `Odin/gods/pipeline.py` (event loop), `Odin/gods/handlers/registration.py` (handler wiring), `Odin/run_pipeline.py` (standalone runner).

## CRITICAL: Two Directories

| Directory | Purpose | What lives there |
|-----------|---------|-----------------|
| `<your Hekate checkout>` | **Source repo** (git) | All source code, commits, pushes |
| `C:\Hekate` | **Deployment target** (NSSM services) | Published binaries + copied Python source |

**Never clone or init git in `C:\Hekate`.** It's a deployment directory. Deploy with `bash scripts/deploy.sh` from admin terminal.

## Deployment

Deploy via Hades admin service (MCP tool `mcp__hades-admin__deploy` or HTTP `POST http://localhost:5201/deploy`):

The deploy handler (`hades/server.py`):
1. Stops all managed NSSM services
2. `dotnet publish` context store → `C:\Hekate\context-store\` (binary)
3. `cp -r` orchestration source → `C:\Hekate\orchestration\` (Python)
4. `cp -r` hades source → `C:\Hekate\hades\`
5. `cp -r` Odin gods + key files → `C:\Hekate\Odin\`
6. `cp -r` context-store/tools → `C:\Hekate\context-store\tools\`
7. Syncs migrations, DB, config
8. `npm run build` orchestration frontend
9. Python syntax check on all `.py` files
10. Starts all NSSM services + health checks

**After any code change**, run the full deploy. No shortcuts — partial deploys cause stale code.

## NSSM Services

All services run via NSSM from `C:\Hekate`, **not** from the source repo.

| Service | Binary/Script | Port | Notes |
|---------|--------------|------|-------|
| HekateEngine | Python 3.11 `run_hekate.py` | 5200 | Gods pipeline + API. Needs `HOME`, `APPDATA`, `USERPROFILE`, `GEMINI_FORCE_FILE_STORAGE` env vars |
| HekateContextStore | `Api.exe` (dotnet publish) | 5102 | Depends on Docker (Postgres) |
| HekateServer | `HekateMcp.Server.exe` | 5110 | Code analysis (Roslyn/Jedi/TS), HTTP MCP transport |
| HekatePythonWorker | `HekateMcp.Worker.Python.exe` | 9200 | Jedi worker (internal, not client-facing) |
| HekateTypeScriptWorker | similar | 9202 | TS worker (internal) |
| HekateCppWorker | similar | 9201 | C++ worker (internal) |
| HekateAdmin | Python 3.14 `server.py` | 5201 | Hades — admin service. Needs `PATH` with nssm + python |
| HekateLLMGateway | Python 3.11 `server.py` | 5210 | LLM proxy — LocalSystem with an explicitly configured CLI user profile |
| HekateHadesMcp | Python 3.11 `mcp_bridge.py` | 5211 | Hades MCP bridge (SSE transport) |
| HekatePrometheusMcp | Python 3.11 `prometheus_mcp.py` | 5212 | Project/task management MCP (SSE transport) |
| HekateAgentContextMcp | Python 3.11 `server.py` | 5213 | Agent context MCP (SSE transport) |

### NSSM Environment (HekateEngine)

The service runs as LocalSystem. These env vars are set via `nssm set AppEnvironmentExtra`:
- `PATH` — must include: `C:\Program Files\nodejs`, `<CLI user profile>\AppData\Roaming\npm`, Python311
- `HOME=<CLI user profile>` — CLI tools read auth from user home
- `USERPROFILE=<CLI user profile>`
- `APPDATA=<CLI user profile>\AppData\Roaming`
- `GEMINI_FORCE_FILE_STORAGE=true` — forces file-based OAuth instead of Windows Credential Manager

### Service Management

Use Hades MCP tools or HTTP API:
- `mcp__hades-admin__list_services` — check all services
- `mcp__hades-admin__restart_service(name)` — restart a service
- `mcp__hades-admin__restart_core` — restart Engine + Context Store
- `mcp__hades-admin__restart_all` — restart everything
- `mcp__hades-admin__deploy` — full deploy from source repo

## Known Fragility

### CLI Executor Auth Under NSSM
- **Claude Code**: Works. Auth stored in `~/.claude/.credentials.json` (flat file, readable by LocalSystem with HOME set). First call after restart is slow (60-120s cold start).
- **Gemini CLI**: Broken (exit code 1, auth issue). All tasks routed to claude_code instead. Token in `~/.gemini/oauth_creds.json`.
- **Codex CLI**: Disabled (low quota, ChatGPT subscription broken).

### Executor-Generated Code
- Executors (Claude/Gemini/Codex) sometimes write broken Python — raw newlines in strings instead of `\n`. The deploy script runs a syntax check before starting services.
- Executors generate Alembic migrations with wrong naming (`add_xyz.py` instead of `NNN_xyz.py`) and wrong `down_revision`. Validation exists in `task_lifecycle.py` but isn't bulletproof.
- Executors don't always `git add` their output files. File tracking in `task_lifecycle.py` catches this post-completion.

### Branch Switching
- The executor creates git worktrees at `.worktrees/{project-slug}/` for each project. This prevents branch switching on the main repo.
- If worktree creation fails, it falls back to `git checkout` which WILL switch the main repo's branch.
- After project completion, `_auto_merge_to_main()` creates a PR and optionally auto-merges.

### Database
- Source and deployment have SEPARATE SQLite databases. The deploy script syncs source → target if source is newer.
- Alembic migrations must follow `NNN` revision ID pattern (just the number, e.g., `018`).

## Components

| Component | Path | Language | Build | Run |
|-----------|------|----------|-------|-----|
| **Extension** | `extension/` | TypeScript | `cd extension && npm run compile` | F5 in VSCode |
| **Orchestration** | `orchestration/` | Python/FastAPI | `pip install -r orchestration/requirements.txt` | `cd orchestration && python run.py` (port 5200) |
| **Context Store** | `context-store/` | C#/.NET 8 | `dotnet publish context-store/Api/Api.csproj -o C:\Hekate\context-store\` | NSSM service (port 5102) |
| **Context Store UI** | `context-store/ui/` | React/Vite | `cd context-store/ui && npm install && npm run dev` | port 5179 |
| **Orchestration Dashboard** | `orchestration/frontend/` | React/Vite | `cd orchestration/frontend && npm run build` | Served by FastAPI from `dist/` |
| **Hades** | `hades/` | Python/FastAPI | `pip install -r hades/requirements.txt` | `python hades/server.py` (port 5201) |
| **LLM Gateway** | `llm-gateway/` | Python/FastMCP | `pip install -r llm-gateway/requirements.txt` | `python llm-gateway/server.py` (port 5210) |
| **Gods (Odin)** | `Odin/` | Python | `pip install -r Odin/requirements.txt` | Standalone god servers |

## Project Structure

```
Hekate/                              # Source repo (<your Hekate checkout>)
├── CLAUDE.md                        # This file
├── scripts/
│   ├── deploy.sh                    # Full deploy to C:\Hekate
│   └── restart.sh                   # NSSM service management
├── orchestration/
│   ├── backend/                     # FastAPI app (API + DB + dashboard)
│   │   ├── services/
│   │   │   ├── planner.py           # Claude-powered plan generation (L0-L3)
│   │   │   ├── decomposer.py       # Plan → task rows + dependency DAG
│   │   │   ├── plan_sync.py         # Sync plans to context store
│   │   │   ├── model_router.py      # Tier routing (claude_code, gemini_cli, ollama)
│   │   │   ├── odin.py             # LLM-driven overseer service
│   │   │   ├── llm_router.py       # Routes LLM calls through gateway (5210)
│   │   │   ├── chat_agent.py       # Multi-round streaming chat
│   │   │   ├── tree_runner.py      # Step-tree executor
│   │   │   ├── executor.py          # [LEGACY] Wave dispatch — being replaced by gods pipeline
│   │   │   ├── task_lifecycle.py    # [LEGACY] Task execution — being replaced by hermes
│   │   │   ├── sentinel/            # [LEGACY] Monitoring — being replaced by odin god
│   │   │   └── context_store_client.py # Circuit-breaker HTTP client
│   │   ├── routes/                  # REST API endpoints (projects, tasks, chat, odin, usage, events)
│   │   ├── migrations/versions/     # Alembic migrations (NNN_description.py)
│   │   ├── mcp/server.py            # MCP server for external executors
│   │   └── container.py             # DI container
│   ├── frontend/                    # React dashboard (build → dist/)
│   ├── tools/                       # CLI utilities
│   ├── config.json                  # Runtime config (gitignored)
│   ├── data/orchestration.db        # SQLite database (gitignored)
│   └── run.py                       # Entry point
├── context-store/
│   ├── Api/                         # .NET Minimal API
│   ├── ui/                          # React chat UI
│   ├── tools/                       # MCP servers (agent-context, skills, dev)
│   │   └── skills/skills.json       # Skill definitions (includes orchestrate bridge)
│   └── docker-compose.yml           # Postgres + AGE + pgvector
├── Odin/                            # Gods pipeline — ACTIVE execution engine
│   ├── gods/
│   │   ├── handlers/                # God implementations
│   │   │   ├── athena_leveled.py   # Planning (L1-L5 via Gemini)
│   │   │   ├── odin.py             # Dispatch, lifecycle, diagnosis
│   │   │   ├── hermes_async.py     # Async CLI execution (Claude Code)
│   │   │   ├── mimir.py            # Verification via LLM Gateway
│   │   │   ├── hephaestus.py       # Git staging
│   │   │   ├── tyche.py            # Budget tracking
│   │   │   └── registration.py     # Wires all handlers
│   │   ├── pipeline.py              # Event loop, cursor, gates
│   │   ├── relay.py                 # Event relay (god_relay_events table)
│   │   ├── plan_levels.py           # L1-L5 rule engine
│   │   ├── registry.py              # God registry
│   │   ├── odin/                    # Odin MCP server (dispatch, mcp_client)
│   │   └── providers/               # CLI provider abstraction (built, not wired)
│   ├── run_pipeline.py              # Standalone runner (--debug flag)
│   └── tests/                       # Gods test suite
├── llm-gateway/                     # LLM proxy for CLI OAuth tokens (port 5210)
├── Design/                          # Pencil design files
├── extension/                       # VSCode extension
└── .worktrees/                      # Git worktrees for parallel projects (gitignored)
```

## Deep-Dive Documentation

Read **on-demand** when working in the relevant area.

| Doc | When to read |
|-----|-------------|
| `PLAN_TO_CODE.md` | Working on the Planner, codegen (Generator/Lowerer/Emitter), Hermes execution, or any of the four open gap-fills. Explains the typed-plan + bounded-hole model the system is converging on. |
| `context-store/plans/011-typed-changes-hermes-refactor.md` | Status of the four Plan→Code gap-fills; pick up where the last person stopped |
| `context-store/plans/037-open-issues-register.md` | Canonical open-issue register (`HK-ISSUE-NNN`): check before starting or closing work; cite IDs in checkpoints |
| `context-store/CLAUDE.md` | Working on agent memory, context router, node types, DB schema |
| `orchestration/CLAUDE.md` | Working on task execution, planning, wave dispatch, auth |
| `.claude/architecture.md` | Understanding cross-component design |

## Infrastructure

| Service | Port | Config |
|---------|------|--------|
| Postgres (AGE + pgvector) | 5433 | `context-store/docker-compose.yml` |
| Context Store API | 5102 | `CODESTORAGE_CONNSTR` env var |
| Context Store UI | 5179 | Vite dev server |
| Hekate Engine (gods pipeline + API) | 5200 | SQLite DB |
| Hades (admin) | 5201 | `HEKATE_ROOT`, `HEKATE_SOURCE` env vars |
| hekate-mcp (code analysis) | 5110 | NSSM `HekateServer`, HTTP MCP transport |
| LLM Gateway | 5210 | NSSM `HekateLLMGateway` |
| Hades MCP | 5211 | SSE transport |
| Prometheus MCP | 5212 | SSE transport |
| Agent Context MCP | 5213 | SSE transport |
| Ollama | 11434 | `OLLAMA_URL` env var |

## Model Routing (Gods Pipeline)

All task types route to `claude_code` via LLM Gateway (port 5210). Gemini disabled.

| Tier | Provider | Used For | Status |
|------|----------|----------|--------|
| `claude_code` | Claude Code CLI | All code, research, analysis, integration, docs | Active — default for everything |
| `ollama` | Ollama (local) | Asset tasks only | Active |
| `gemini_cli` | Gemini CLI | — | Disabled (exit code 1, auth broken) |
| `codex_cli` | Codex CLI | — | Disabled (ChatGPT subscription broken) |

Tier map: `Odin/gods/handlers/odin.py` `_TIER_MAP`. Fallback chain: `["claude_code", "ollama"]`.

## Environment

- **Runtime**: Node.js 24+, Python 3.11 (NOT 3.14), .NET 8 (SDK 10), Docker
- **Key env vars**: `ANTHROPIC_API_KEY` (optional), `CODESTORAGE_CONNSTR`, `OLLAMA_URL`
- **Platform**: Windows 11, bash shell (Git Bash)
- **Python**: Use the `HEKATE_PYTHON` interpreter path configured for the deployment — Python 3.14 has broken FastAPI imports

## Git Workflow

- workflow: direct
- base_branch: main
- Executor creates worktrees per project, auto-PRs on completion
- Always merge executor branches back to main promptly
