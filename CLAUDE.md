# Hekate

Unified AI agent platform: VSCode extension (fleet control) + orchestration backend (task execution) + context store (agent memory). Three components, one repo.

## Components

| Component | Path | Language | Build | Run |
|-----------|------|----------|-------|-----|
| **Extension** | `extension/` | TypeScript | `cd extension && npm run compile` | F5 in VSCode |
| **Orchestration** | `orchestration/` | Python/FastAPI | `pip install -r orchestration/requirements.txt` | `cd orchestration && python run.py` (port 5200) |
| **Context Store** | `context-store/` | C#/.NET 8 | `dotnet build context-store/Api/Api.csproj` | `dotnet run --project context-store/Api/Api.csproj` (port 5102) |
| **Context Store UI** | `context-store/ui/` | React/Vite | `cd context-store/ui && npm install && npm run dev` | port 5179 |

## Project Structure

```
Hekate/
├── CLAUDE.md                    # This file
├── extension/                   # VSCode extension — fleet control center
│   ├── src/                     # TypeScript source (api/, views/, etc.)
│   ├── package.json             # Extension manifest
│   ├── esbuild.js               # Build script
│   └── tsconfig.json
├── orchestration/               # Task orchestration backend
│   ├── backend/                 # FastAPI app (routes/, services/, tools/, db/)
│   ├── tests/                   # pytest (unit/, integration/, e2e/, load/)
│   ├── frontend/                # Orchestration dashboard (React)
│   ├── tools/                   # CLI executors (local_executor, supervisor, plan patches)
│   ├── run.py                   # Entry point (uvicorn)
│   ├── Dockerfile               # Multi-stage build
│   ├── requirements.txt
│   └── CLAUDE.md                # Orchestration-specific docs
├── context-store/               # Agent memory (Postgres + AGE + pgvector)
│   ├── DbLayer/                 # Node CRUD, pgvector search
│   ├── GraphLayer/              # AGE Cypher queries
│   ├── ContextRouter/           # Intent-driven context assembly
│   ├── Api/                     # .NET Minimal API (port 5102)
│   ├── ui/                      # React frontend (port 5179)
│   ├── tools/                   # MCP servers, scripts
│   ├── docker-compose.yml       # Postgres + AGE + pgvector
│   └── CLAUDE.md                # Context store-specific docs
└── .claude/                     # Deep-dive docs
```

## Deep-Dive Documentation

Read these **on-demand** when working in the relevant area.

| Doc | When to read |
|-----|-------------|
| `context-store/CLAUDE.md` | Working on agent memory, context router, node types, DB schema |
| `orchestration/CLAUDE.md` | Working on task execution, planning, wave dispatch, auth |
| `extension/SETUP-BRYAN.md` | Installing/configuring the VSCode extension |
| `.claude/architecture.md` | Understanding cross-component design |

## Infrastructure

| Service | Port | Config |
|---------|------|--------|
| Postgres (AGE + pgvector) | 5433 | `context-store/docker-compose.yml` |
| Context Store API | 5102 | `CODESTORAGE_CONNSTR` env var |
| Context Store UI | 5179 | `context-store/ui/vite.config.ts` |
| Orchestration API | 5200 | `orchestration/config.json` |
| Ollama | 11434 | `OLLAMA_URL` env var |

## Quick Start

```bash
# 1. Start Postgres
cd context-store && docker compose up -d --build

# 2. Start context store API
dotnet run --project context-store/Api/Api.csproj

# 3. Start orchestration backend
cd orchestration && pip install -r requirements.txt && python run.py

# 4. Start context store UI
cd context-store/ui && npm install && npm run dev
```

## Environment

- **Runtime**: Node.js 20+, Python 3.11+, .NET 8, Docker
- **Key env vars**: `ANTHROPIC_API_KEY`, `CODESTORAGE_CONNSTR`, `OLLAMA_URL`
- **Platform**: Windows 11, bash shell

## Git Workflow

- workflow: direct
- base_branch: main
