# Installation

Complete setup guide for all Hekate components.

---

## System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| OS | Windows 10/11, Linux, macOS | Windows 11 (NSSM services) |
| RAM | 8 GB | 16+ GB (Ollama models) |
| CPU | 4 cores | 8+ cores |
| GPU | None (CPU inference) | NVIDIA GPU (Ollama acceleration) |
| Disk | 10 GB | 50+ GB (models + Docker volumes) |

## Runtime Dependencies

| Dependency | Version | Purpose |
|------------|---------|---------|
| **Python** | 3.11.x | Engine, Hades, LLM Gateway, MCP servers |
| **Node.js** | 24+ | Dashboard build, TS worker |
| **.NET SDK** | 8.0+ | Context Store API |
| **Docker** | Latest | PostgreSQL (AGE + pgvector) |
| **Git** | Any | Worktree management, staging |
| **Claude Code** | Latest | Primary task executor |
| **Ollama** | Latest | Embeddings, local inference (optional) |

!!! note "Python Versions"
    The Hekate Engine, LLM Gateway, and MCP servers require **Python 3.11**. The Hades admin service runs on Python 3.14. Do not mix them — 3.14 breaks FastAPI imports in the engine.

---

## Step-by-Step Setup

### 1. Install Python Dependencies

```bash
# Core engine
pip install -r orchestration/requirements.txt

# Gods pipeline
pip install -r Odin/requirements.txt

# Admin service
pip install -r hades/requirements.txt

# LLM Gateway
pip install -r llm-gateway/requirements.txt

# Agent Context MCP
pip install -r context-store/tools/agent-context-mcp/requirements.txt
```

### 2. Start PostgreSQL

```bash
cd context-store
docker compose up -d
```

This starts a custom PostgreSQL image with:

- **Apache AGE** — property graph layer
- **pgvector** — vector similarity search
- Port **5433** (not 5432, to avoid conflicts)

Verify:

```bash
docker exec code-storage-db pg_isready -U postgres
# /var/run/postgresql:5432 - accepting connections
```

### 3. Build Context Store API

```bash
dotnet publish context-store/Api/Api.csproj -c Release -o C:\Hekate\context-store\
```

### 4. Build Dashboard

```bash
cd orchestration/frontend
npm install
npm run build
cd ../..
```

The built dashboard is served by the engine at port 5200.

### 5. Configure

```bash
cp orchestration/config.example.json orchestration/config.json
```

Edit `orchestration/config.json` — see [Configuration Reference](../reference/configuration.md) for all options.

### 6. Set Up Ollama (Optional)

Ollama provides local embeddings and inference:

```bash
# Install Ollama, then pull the embedding model
ollama pull nomic-embed-text

# Optional: pull a general model for planning
ollama pull qwen3.5:latest
```

### 7. Verify

Start the engine and check health:

```bash
cd Odin
python run_hekate.py &

# Check health
curl http://localhost:5200/api/health
# {"status": "ok"}
```

---

## Component Startup Order

Start components in this order (dependencies first):

1. **Docker** (PostgreSQL) — required by Context Store
2. **Context Store API** (port 5102) — required by engine for context enrichment
3. **LLM Gateway** (port 5210) — required by engine for LLM calls
4. **Hekate Engine** (port 5200) — the core
5. **Hades Admin** (port 5201) — optional, for service management
6. **MCP Servers** (ports 5211-5213) — optional, for agent access

---

## Windows NSSM Services

For production deployment on Windows, see [Windows Services (NSSM)](../deployment/windows-nssm.md). NSSM wraps each component as a Windows service with automatic restart.

---

## Verify Full Stack

Once everything is running:

```bash
# Engine health
curl http://localhost:5200/api/health

# Context Store health
curl http://localhost:5102/api/health

# LLM Gateway health
curl http://localhost:5210/health

# Hades health
curl http://localhost:5201/health

# Ollama (if installed)
curl http://localhost:11434/
```

All should return HTTP 200.
