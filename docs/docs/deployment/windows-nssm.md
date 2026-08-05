# Windows Services (NSSM)

All Hekate production services run as Windows services managed by [NSSM (Non-Sucking Service Manager)](https://nssm.cc/). Services execute from the deployment directory `C:\Hekate`, not from the source repository.

## Service Registry

The full service configuration lives in `hades/services.json`. There are 13 services across four groups:

### Core Services

| Service | Port | Binary | Working Dir | Group |
|---------|------|--------|-------------|-------|
| **HekateOrchestration** | 5200 | Python 3.11 `run.py` | `C:\Hekate\orchestration` | core |
| **HekateContextStore** | 5102 | `Api.exe` (.NET publish) | `C:\Hekate\context-store` | core |
| **HekateLLMGateway** | 5210 | Python 3.11 `server.py` | `C:\Hekate\llm-gateway` | core |

### MCP Services

| Service | Port | Binary | Working Dir | Group |
|---------|------|--------|-------------|-------|
| **HekateServer** | 5110 | `HekateMcp.Server.exe --http 5110` | `C:\Hekate\server` | mcp |
| **HekatePythonWorker** | 9200 | `HekateMcp.Worker.Python.exe` | `C:\Hekate\workers\python` | mcp |
| **HekateTypeScriptWorker** | 9202 | `HekateMcp.Worker.TypeScript.exe` | `C:\Hekate\workers\typescript` | mcp |
| **HekateCppWorker** | 9201 | `HekateMcp.Worker.Cpp.exe` | `C:\Hekate\workers\cpp` | mcp |
| **HekateHadesMcp** | 5211 | Python 3.11 `mcp_bridge.py` | `C:\Hekate\hades` | mcp |
| **HekatePrometheusMcp** | 5212 | Python 3.11 `prometheus_mcp.py` | `C:\Hekate\Odin` | mcp |
| **HekateAgentContextMcp** | 5213 | Python 3.11 `server.py` | `C:\Hekate\context-store\tools\agent-context-mcp` | mcp |

### Admin Services

| Service | Port | Binary | Working Dir | Group |
|---------|------|--------|-------------|-------|
| **HekateAdmin** | 5201 | Python 3.14 `server.py` | `C:\Hekate\hades` | admin |

### External Services (not NSSM-managed)

| Service | Port | Notes | Group |
|---------|------|-------|-------|
| **Ollama** | 11434 | Installed separately, runs as its own service | external |
| **ComfyUI** | 8188 | Started manually or via separate launcher | external |

## Creating Services

### Install NSSM

Download NSSM from [nssm.cc](https://nssm.cc/) and ensure it is on your PATH.

### Service Creation Commands

Each NSSM service requires three parameters: the executable path, the arguments, and the working directory.

**Example: HekateOrchestration**

```bash
nssm install HekateOrchestration "C:\Users\jruss\AppData\Local\Programs\Python\Python311\python.exe"
nssm set HekateOrchestration AppParameters "run.py"
nssm set HekateOrchestration AppDirectory "C:\Hekate\orchestration"
nssm set HekateOrchestration Start SERVICE_AUTO_START
```

**Example: HekateContextStore**

```bash
nssm install HekateContextStore "C:\Hekate\context-store\Api.exe"
nssm set HekateContextStore AppDirectory "C:\Hekate\context-store"
nssm set HekateContextStore Start SERVICE_AUTO_START
```

**Example: HekateLLMGateway**

```bash
nssm install HekateLLMGateway "C:\Users\jruss\AppData\Local\Programs\Python\Python311\python.exe"
nssm set HekateLLMGateway AppParameters "C:\Hekate\llm-gateway\server.py"
nssm set HekateLLMGateway AppDirectory "C:\Hekate\llm-gateway"
nssm set HekateLLMGateway Start SERVICE_AUTO_START
```

**Example: HekateAdmin** (uses Python 3.14)

```bash
nssm install HekateAdmin "C:\Python314\python.exe"
nssm set HekateAdmin AppParameters "C:\Hekate\hades\server.py"
nssm set HekateAdmin AppDirectory "C:\Hekate\hades"
nssm set HekateAdmin Start SERVICE_AUTO_START
```

**Example: HekateServer** (code analysis, .NET binary with args)

```bash
nssm install HekateServer "C:\Hekate\server\HekateMcp.Server.exe"
nssm set HekateServer AppParameters "--http 5110"
nssm set HekateServer AppDirectory "C:\Hekate\server"
nssm set HekateServer Start SERVICE_AUTO_START
```

## Environment Variables for HekateOrchestration

The orchestration engine is the most environment-sensitive service. It runs as LocalSystem, which has no user profile by default. Several tools (Claude Code CLI, Gemini CLI, Node.js) expect user-specific directories to exist.

Set environment variables via NSSM:

```bash
nssm set HekateOrchestration AppEnvironmentExtra ^
    "PATH=C:\Program Files\nodejs;C:\Users\jruss\AppData\Roaming\npm;C:\Users\jruss\AppData\Local\Programs\Python\Python311;C:\Windows\System32;C:\Program Files\NSSM" ^
    "HOME=C:\Users\jruss" ^
    "USERPROFILE=C:\Users\jruss" ^
    "APPDATA=C:\Users\jruss\AppData\Roaming" ^
    "GEMINI_FORCE_FILE_STORAGE=true"
```

### Why LocalSystem Needs These Variables

| Variable | Why |
|----------|-----|
| `PATH` | LocalSystem has a minimal PATH. Must include Node.js (for frontend tooling), npm global bin (for CLI tools), Python 3.11, and nssm itself. |
| `HOME` | Claude Code CLI reads auth credentials from `~/.claude/.credentials.json`. Without HOME, `~` resolves to `C:\Windows\System32\config\systemprofile`, which has no credentials. |
| `USERPROFILE` | .NET and various tools use USERPROFILE for config discovery. Must match HOME. |
| `APPDATA` | Node.js, npm, and other tools store config under APPDATA. Without it, they fail or write to system directories. |
| `GEMINI_FORCE_FILE_STORAGE` | Forces Gemini CLI to use file-based OAuth (`~/.gemini/oauth_creds.json`) instead of Windows Credential Manager, which LocalSystem cannot access. |

!!! warning "Credential access"
    LocalSystem cannot access the Windows Credential Manager. Any CLI tool that stores OAuth tokens in the credential manager will fail. The `HOME`, `USERPROFILE`, and `GEMINI_FORCE_FILE_STORAGE` variables work around this by redirecting to file-based storage in the real user's home directory.

## Service Management Commands

### Manual NSSM commands

```bash
# Start / stop / restart a single service
nssm start HekateOrchestration
nssm stop HekateOrchestration
nssm restart HekateOrchestration

# Check service status
nssm status HekateOrchestration

# View service configuration
nssm dump HekateOrchestration

# Edit service interactively (opens GUI)
nssm edit HekateOrchestration

# Remove a service
nssm remove HekateOrchestration confirm
```

### Via Hades Admin (recommended)

The Hades admin service (port 5201) provides HTTP and MCP interfaces for service management. See [Service Management](service-management.md) for details.

## Logging

NSSM captures stdout and stderr from each service. Configure log output:

```bash
nssm set HekateOrchestration AppStdout "C:\Hekate\logs\orchestration.log"
nssm set HekateOrchestration AppStderr "C:\Hekate\logs\orchestration-error.log"
nssm set HekateOrchestration AppStdoutCreationDisposition 4
nssm set HekateOrchestration AppStderrCreationDisposition 4
nssm set HekateOrchestration AppRotateFiles 1
nssm set HekateOrchestration AppRotateBytes 10485760
```

The `CreationDisposition 4` flag appends to existing logs instead of overwriting. `AppRotateFiles 1` enables log rotation, and `AppRotateBytes` sets the maximum file size before rotation (10 MB in this example).

## Troubleshooting

### Service starts and immediately stops

Check the NSSM event log and service stderr:

```bash
nssm status HekateOrchestration
# If status is "SERVICE_STOPPED", check logs
```

Common causes:

- **Wrong Python version** -- HekateAdmin uses Python 3.14, all others use Python 3.11
- **Missing dependencies** -- Run `pip install -r requirements.txt` in the deployment directory
- **Port conflict** -- Another process is already bound to the port
- **Missing environment variables** -- Especially PATH and HOME for HekateOrchestration

### Service running but health check fails

```bash
curl http://localhost:5200/api/health
```

If the health endpoint does not respond, the service may be starting up (cold start can take 60-120 seconds for CLI-dependent services) or may have hit an unhandled exception after startup.

### "Access denied" errors in service logs

LocalSystem does not have the same permissions as your user account. Ensure the deployment directory (`C:\Hekate`) and all subdirectories are readable by LocalSystem. File-based credential paths (HOME, USERPROFILE) must also be accessible.
