# Development Setup

How to set up a local Hekate development environment.

---

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Node.js | 24+ | Required for frontend builds and extension |
| Python | 3.11 | Use `C:\Users\jruss\AppData\Local\Programs\Python\Python311\python.exe`. Do NOT use Python 3.14 (broken FastAPI imports). |
| .NET SDK | 8 (SDK 10) | For context store and code analysis server |
| Docker | Latest | For Postgres (AGE + pgvector) |
| Git | Latest | Git Bash recommended on Windows |

---

## Clone the Repository

```bash
git clone https://github.com/jruss/Hekate.git
cd Hekate
```

---

## Two Directories

Hekate uses two directories. Understanding this is critical.

| Directory | Purpose | What lives there |
|-----------|---------|------------------|
| `C:\Users\jruss\Documents\GitHub\Hekate` | **Source repo** (git) | All source code, commits, pushes |
| `C:\Hekate` | **Deployment target** (NSSM services) | Published binaries + copied Python source |

**Never clone or `git init` in `C:\Hekate`.** It is a deployment directory managed by the Hades deploy script. All code changes happen in the source repo.

---

## Install Dependencies

### Python (Orchestration + Odin + Hades + LLM Gateway)

```bash
pip install -r orchestration/requirements.txt
pip install -r Odin/requirements.txt
pip install -r hades/requirements.txt
pip install -r llm-gateway/requirements.txt
```

### .NET (Context Store + Code Analysis)

```bash
dotnet restore context-store/Api/Api.csproj
```

### Node.js (Dashboard + Context Store UI + Extension)

```bash
cd orchestration/frontend && npm install && cd ../..
cd context-store/ui && npm install && cd ../..
cd extension && npm install && cd ..
```

### Docker (Postgres)

```bash
cd context-store && docker compose up -d && cd ..
```

This starts Postgres with AGE (graph) and pgvector (embeddings) on port 5433.

---

## IDE Setup

### VS Code

The repository includes VS Code workspace settings. Open the root `Hekate` folder.

For the VS Code extension:
1. Open `extension/` in VS Code
2. Press F5 to launch the Extension Development Host

### Python

Set your Python interpreter to Python 3.11:
```
C:\Users\jruss\AppData\Local\Programs\Python\Python311\python.exe
```

---

## Running Services Locally

### Orchestration Engine (Port 5200)

```bash
cd orchestration && python run.py
```

### Context Store (Port 5102)

```bash
dotnet run --project context-store/Api/Api.csproj
```

Requires Docker Postgres running (port 5433).

### Hades Admin (Port 5201)

```bash
python hades/server.py
```

### LLM Gateway (Port 5210)

```bash
python llm-gateway/server.py
```

### Gods Pipeline (Standalone)

```bash
python Odin/run_pipeline.py --debug
```

### Dashboard (Development)

```bash
cd orchestration/frontend && npm run dev
```

Served at `http://localhost:5173`, proxies API calls to port 5200.

### Context Store UI (Development)

```bash
cd context-store/ui && npm run dev
```

Served at `http://localhost:5179`.

---

## Deployment

For deploying to the NSSM service directory (`C:\Hekate`), use the Hades admin service:

```bash
# Via HTTP API
curl -X POST http://localhost:5201/deploy

# Via MCP tool
# Use mcp__hades-admin__deploy from Claude Code
```

The deploy handler:
1. Stops all managed NSSM services
2. Publishes context store binaries (`dotnet publish`)
3. Copies orchestration, hades, Odin, and context store tools source
4. Syncs migrations and database
5. Builds the orchestration frontend
6. Runs Python syntax checks
7. Starts all services with health checks

**After any code change, run the full deploy.** Partial deploys cause stale code.

---

## Git Workflow

Hekate uses a direct-to-main workflow:

- **Branch:** `main` (single branch)
- **Commits:** Push directly to main
- **Executor branches:** The gods pipeline creates git worktrees at `.worktrees/{project-slug}/` for each project being executed
- **Auto-PR:** On project completion, the pipeline creates a PR and optionally auto-merges

There are no feature branches for human development -- commit and push directly to main.

---

## Environment Variables

Key environment variables for local development:

| Variable | Value | Purpose |
|----------|-------|---------|
| `ANTHROPIC_API_KEY` | (optional) | Direct API access (CLI uses OAuth) |
| `CODESTORAGE_CONNSTR` | (auto from docker-compose) | Context store DB connection |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama for local models |
| `HOME` | `C:\Users\jruss` | CLI tools read auth from home |

---

## Ports Reference

| Port | Service |
|------|---------|
| 5102 | Context Store API |
| 5110 | Hekate Code Analysis (MCP) |
| 5179 | Context Store UI (dev) |
| 5200 | Orchestration Engine |
| 5201 | Hades Admin |
| 5210 | LLM Gateway |
| 5211 | Hades MCP (SSE) |
| 5212 | Prometheus MCP (SSE) |
| 5213 | Agent Context MCP (SSE) |
| 5433 | Postgres (Docker) |
| 9200 | Python Worker (internal) |
| 9201 | C++ Worker (internal) |
| 9202 | TypeScript Worker (internal) |
| 11434 | Ollama |
