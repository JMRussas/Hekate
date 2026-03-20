# Hekate

Unified AI agent platform: gods pipeline (event-driven task execution) + orchestration backend (API + DB + dashboard) + context store (agent memory) + admin service (infra). Six components, one repo.

## Execution Architecture

**Gods pipeline is the active execution engine.** The monolith orchestration services (executor.py, task_lifecycle.py, sentinel/) are legacy — still deployed but being replaced.

```
Project created → Athena plans (Gemini, L1-L5) → Odin dispatches
  → Hermes executes (Claude Code CLI, async) → Mimir verifies (LLM Gateway)
  → Odin manages lifecycle (unblock deps → wave progression → complete)
```

Key files: `Odin/gods/pipeline.py` (event loop), `Odin/gods/handlers/registration.py` (handler wiring), `Odin/run_pipeline.py` (standalone runner).

## CRITICAL: Two Directories

| Directory | Purpose | What lives there |
|-----------|---------|-----------------|
| `C:\Users\jruss\Documents\GitHub\Hekate` | **Source repo** (git) | All source code, commits, pushes |
| `C:\Hekate` | **Deployment target** (NSSM services) | Published binaries + copied Python source |

**Never clone or init git in `C:\Hekate`.** It's a deployment directory. Deploy with `bash scripts/deploy.sh` from admin terminal.

## Deployment

```bash
# Full deploy — always use this, never partial copies
bash scripts/deploy.sh
```

The deploy script (`scripts/deploy.sh`):
1. Stops all NSSM services
2. `dotnet publish` context store → `C:\Hekate\context-store\` (binary)
3. `cp -r` orchestration source → `C:\Hekate\orchestration\` (Python)
4. Syncs migrations, DB, config
5. `npm run build` orchestration frontend
6. Python syntax check on all `.py` files
7. Starts all NSSM services
8. Health checks + verification

**After any code change**, run the full deploy. No shortcuts — partial deploys cause missing migrations, stale code, broken frontends.

## NSSM Services

All services run via NSSM from `C:\Hekate`, **not** from the source repo.

| Service | Binary/Script | Port | Notes |
|---------|--------------|------|-------|
| HekateOrchestration | Python 3.11 `run.py` | 5200 | Needs `HOME`, `APPDATA`, `USERPROFILE`, `GEMINI_FORCE_FILE_STORAGE` env vars |
| HekateContextStore | `Api.exe` (dotnet publish) | 5102 | Depends on Docker (Postgres) |
| HekateServer | `HekateMcp.Server.exe` | 5110 | hekate-mcp code analysis |
| HekatePythonWorker | `HekateMcp.Worker.Python.exe` | 9200 | |
| HekateTypeScriptWorker | similar | 9202 | |
| HekateCppWorker | similar | 9201 | |
| HekateAdmin | Python 3.14 `server.py` | 5201 | Hades — admin service. Needs `PATH` with nssm + python |
| HekateLLMGateway | Python 3.11 `server.py` | 5210 | LLM proxy — runs as user for CLI OAuth token access |

### NSSM Environment (HekateOrchestration)

The service runs as LocalSystem. These env vars are set via `nssm set AppEnvironmentExtra`:
- `PATH` — must include: `C:\Program Files\nodejs`, `C:\Users\jruss\AppData\Roaming\npm`, Python311
- `HOME=C:\Users\jruss` — CLI tools read auth from user home
- `USERPROFILE=C:\Users\jruss`
- `APPDATA=C:\Users\jruss\AppData\Roaming`
- `GEMINI_FORCE_FILE_STORAGE=true` — forces file-based OAuth instead of Windows Credential Manager

### Service Management

```bash
bash scripts/restart.sh status    # check all services
bash scripts/restart.sh restart   # restart core (Orchestration + Context Store)
bash scripts/restart.sh restart --all  # restart everything including MCP workers
```

## Known Fragility

### CLI Executor Auth Under NSSM
- **Claude Code**: Works. Auth stored in `~/.claude/.credentials.json` (flat file, readable by LocalSystem with HOME set).
- **Gemini CLI**: Works with `GEMINI_FORCE_FILE_STORAGE=true`. Token in `~/.gemini/oauth_creds.json`. Without the env var, uses Windows Credential Manager which LocalSystem can't access.
- **Codex CLI**: Disabled (low quota). Works with `gpt-5.2-codex` model. Auth in `~/.codex/auth.json`. Models `gpt-5.3-codex` and `gpt-5.4` are broken on ChatGPT subscription.

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
Hekate/                              # Source repo (C:\Users\jruss\Documents\GitHub\Hekate)
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
| `context-store/CLAUDE.md` | Working on agent memory, context router, node types, DB schema |
| `orchestration/CLAUDE.md` | Working on task execution, planning, wave dispatch, auth |
| `.claude/architecture.md` | Understanding cross-component design |

## Infrastructure

| Service | Port | Config |
|---------|------|--------|
| Postgres (AGE + pgvector) | 5433 | `context-store/docker-compose.yml` |
| Context Store API | 5102 | `CODESTORAGE_CONNSTR` env var |
| Context Store UI | 5179 | Vite dev server |
| Orchestration API + Dashboard | 5200 | `orchestration/config.json` |
| Hades (admin) | 5201 | `HEKATE_ROOT`, `HEKATE_SOURCE` env vars |
| Ollama | 11434 | `OLLAMA_URL` env var |
| hekate-mcp | 5110 | NSSM `HekateServer` |
| LLM Gateway | 5210 | NSSM `HekateLLMGateway`, Python 3.11 |

## Model Routing

| Tier | Provider | Used For | Status |
|------|----------|----------|--------|
| `claude_code` | Claude Code CLI | Medium/complex code, integration | Active |
| `gemini_cli` | Gemini CLI | Simple code, research, analysis | Active (needs `GEMINI_FORCE_FILE_STORAGE=true`) |
| `codex_cli` | Codex CLI | Simple code | Disabled (low quota, use `gpt-5.2-codex` to re-enable) |
| `ollama` | Ollama (local) | Analysis, documentation | Active |
| `haiku` | Claude Haiku (API) | Verification, knowledge extraction | Active (routes through CLI if no API key) |
| `sonnet` | Claude Sonnet (API) | Complex tasks | Active (routes through CLI if no API key) |
| `opus` | Claude Opus (API) | Critical tasks | Active (routes through CLI if no API key) |

## Environment

- **Runtime**: Node.js 24+, Python 3.11 (NOT 3.14), .NET 8 (SDK 10), Docker
- **Key env vars**: `ANTHROPIC_API_KEY` (optional), `CODESTORAGE_CONNSTR`, `OLLAMA_URL`
- **Platform**: Windows 11, bash shell (Git Bash)
- **Python**: Use `C:\Users\jruss\AppData\Local\Programs\Python\Python311\python.exe` — Python 3.14 has broken FastAPI imports

## Git Workflow

- workflow: direct
- base_branch: main
- Executor creates worktrees per project, auto-PRs on completion
- Always merge executor branches back to main promptly
