# Environment Variables

Master reference for all environment variables used across Hekate services. Variables are organized by scope: global (used by multiple services), per-service, and NSSM-specific.

## Global Variables

These variables affect multiple services or the platform as a whole.

| Variable | Example Value | Description |
|----------|--------------|-------------|
| `ORCHESTRATION_DSN` | `postgresql+aiosqlite://postgres:postgres@localhost:5433/orchestration` | Postgres connection URI. When set, the orchestration backend uses Postgres instead of SQLite. Omit for SQLite mode. |
| `CODESTORAGE_CONNSTR` | `Host=localhost;Port=5433;Database=code_storage;Username=postgres;Password=postgres` | .NET connection string for the Context Store database (PostgreSQL with AGE + pgvector). |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API endpoint for local model inference. Used by the LLM Gateway for asset-tier tasks. |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Anthropic API key. Optional -- the LLM Gateway primarily uses Claude Code CLI (OAuth-based), not the API directly. Only needed if using the Anthropic SDK directly. |
| `HEKATE_ROOT` | `C:/Hekate` | Deployment target directory. Used by Hades admin service. Defaults to `C:/Hekate`. |
| `HEKATE_SOURCE` | `C:/Users/jruss/Documents/GitHub/Hekate` | Source repository path. Used by Hades admin service for deploy operations. |

## NSSM Service Variables (HekateOrchestration)

These variables are set via `nssm set AppEnvironmentExtra` and are critical for services running under LocalSystem.

| Variable | Value | Required By | Purpose |
|----------|-------|-------------|---------|
| `HOME` | `C:\Users\jruss` | Claude Code CLI | CLI reads auth from `~/.claude/.credentials.json`. LocalSystem's default home (`C:\Windows\System32\config\systemprofile`) has no credentials. |
| `USERPROFILE` | `C:\Users\jruss` | .NET, npm, various tools | Many Windows tools use USERPROFILE for config file discovery. Must match HOME. |
| `APPDATA` | `C:\Users\jruss\AppData\Roaming` | Node.js, npm, VS Code | Node.js and npm store global config and packages under APPDATA. |
| `GEMINI_FORCE_FILE_STORAGE` | `true` | Gemini CLI | Forces file-based OAuth token storage (`~/.gemini/oauth_creds.json`) instead of Windows Credential Manager, which LocalSystem cannot access. |
| `PATH` | See below | All subprocesses | LocalSystem has a minimal PATH. Must be extended to include required tools. |

### PATH Requirements

The PATH for HekateOrchestration must include:

```
C:\Program Files\nodejs              # Node.js runtime
C:\Users\jruss\AppData\Roaming\npm   # npm global packages (CLI tools)
C:\Users\jruss\AppData\Local\Programs\Python\Python311  # Python 3.11
C:\Program Files\NSSM                # NSSM binary (for service management)
C:\Windows\System32                  # Standard Windows tools
```

Set via NSSM:

```bash
nssm set HekateOrchestration AppEnvironmentExtra ^
    "PATH=C:\Program Files\nodejs;C:\Users\jruss\AppData\Roaming\npm;C:\Users\jruss\AppData\Local\Programs\Python\Python311;C:\Program Files\NSSM;C:\Windows\System32" ^
    "HOME=C:\Users\jruss" ^
    "USERPROFILE=C:\Users\jruss" ^
    "APPDATA=C:\Users\jruss\AppData\Roaming" ^
    "GEMINI_FORCE_FILE_STORAGE=true"
```

## Per-Service Variables

### HekateOrchestration (port 5200)

| Variable | Required | Description |
|----------|----------|-------------|
| `ORCHESTRATION_DSN` | No | Postgres connection string. Omit for SQLite. |
| `HOME` | Yes (NSSM) | User home for CLI auth |
| `USERPROFILE` | Yes (NSSM) | Same as HOME |
| `APPDATA` | Yes (NSSM) | For Node.js/npm config |
| `GEMINI_FORCE_FILE_STORAGE` | Yes (NSSM) | File-based Gemini OAuth |
| `PATH` | Yes (NSSM) | Extended PATH (see above) |

### HekateContextStore (port 5102)

| Variable | Required | Description |
|----------|----------|-------------|
| `CODESTORAGE_CONNSTR` | Yes | PostgreSQL connection string for AGE + pgvector database |
| `ASPNETCORE_URLS` | No | Override default listen URL (defaults to `http://+:5102`) |

### HekateLLMGateway (port 5210)

| Variable | Required | Description |
|----------|----------|-------------|
| `HOME` | Yes (NSSM) | Claude Code CLI reads OAuth credentials from `~/.claude/` |
| `OLLAMA_URL` | No | Ollama endpoint. Defaults to `http://localhost:11434`. |

### HekateAdmin / Hades (port 5201)

| Variable | Required | Description |
|----------|----------|-------------|
| `HEKATE_ROOT` | No | Deployment directory. Defaults to `C:/Hekate`. |
| `HEKATE_SOURCE` | No | Source repo path. Defaults to `C:/Users/jruss/Documents/GitHub/Hekate`. |
| `ADMIN_MCP_PORT` | No | Port override. Defaults to `5201`. |
| `PATH` | Yes (NSSM) | Must include nssm and Python on PATH for service management and deploy |

### MCP Bridge Services (ports 5211-5213)

The MCP bridge services (HekateHadesMcp, HekatePrometheusMcp, HekateAgentContextMcp) generally inherit their configuration from the services they bridge to. No additional environment variables are required beyond standard PATH and Python availability.

### External Services

| Service | Variable | Description |
|---------|----------|-------------|
| Ollama (11434) | `OLLAMA_MODELS` | Directory for model storage (Ollama's own config) |
| ComfyUI (8188) | Various | ComfyUI has its own configuration; not managed by Hekate |

## LocalSystem Considerations

All NSSM services run under the LocalSystem account. This affects environment variables in several ways:

**No user profile by default.** LocalSystem's home directory is `C:\Windows\System32\config\systemprofile`. This directory exists but contains no user-specific configuration. Setting `HOME` and `USERPROFILE` redirects tools to the real user's profile.

**No credential manager access.** The Windows Credential Manager is per-user and per-session. LocalSystem has its own credential store, which is typically empty. Tools that use the credential manager (Gemini CLI) must be forced to use file-based storage instead.

**Minimal PATH.** LocalSystem's PATH contains only system directories. Any tool not in `C:\Windows\System32` must be explicitly added to PATH via NSSM's `AppEnvironmentExtra`.

**Registry hive differences.** HKEY_CURRENT_USER for LocalSystem is different from your user account's HKCU. Tools that read registry settings may behave differently.

## Checking Current Values

### From an NSSM service

```bash
# View all environment extras set on a service
nssm get HekateOrchestration AppEnvironmentExtra
```

### From the running service

The Hades admin service exposes system info:

```bash
curl http://localhost:5201/system-info
```

### From the Hades MCP

```
mcp__hades-admin__system_info
```

## Troubleshooting

### "Claude Code CLI auth failed" in execution logs

The CLI cannot find credentials. Verify:

```bash
nssm get HekateOrchestration AppEnvironmentExtra
```

Ensure `HOME=C:\Users\jruss` is set and that `C:\Users\jruss\.claude\.credentials.json` exists and is readable by LocalSystem.

### "Gemini CLI exit code 1"

Gemini CLI OAuth is broken under LocalSystem even with `GEMINI_FORCE_FILE_STORAGE=true`. All tasks are currently routed to `claude_code` as a workaround. This is a known issue.

### Context Store cannot connect to Postgres

Verify `CODESTORAGE_CONNSTR` is set correctly. The port should be `5433` (the Docker-mapped port), not `5432`. Ensure the Docker container `code-storage-db` is running and healthy.

### "node: command not found" in service logs

The PATH for the NSSM service does not include Node.js. Update it:

```bash
nssm set HekateOrchestration AppEnvironmentExtra "PATH=C:\Program Files\nodejs;..."
nssm restart HekateOrchestration
```

### Changing environment variables

After modifying NSSM environment variables, you must restart the affected service for changes to take effect:

```bash
nssm set HekateOrchestration AppEnvironmentExtra "NEW_VAR=value" ...
nssm restart HekateOrchestration
```

There is no hot-reload for environment variables -- a service restart is always required.
