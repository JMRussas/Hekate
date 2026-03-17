#!/usr/bin/env python3
"""
Hades — privileged admin service for the Hekate production environment.

Runs as an NSSM service (HekateAdmin) under LocalSystem, exposing
service management, deployment, and log tailing via HTTP on port 5201.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HEKATE_ROOT = Path(os.environ.get("HEKATE_ROOT", "C:/Hekate"))
SOURCE_ROOT = Path(os.environ.get("HEKATE_SOURCE", "C:/Users/jruss/Documents/GitHub/Hekate"))
PORT = int(os.environ.get("ADMIN_MCP_PORT", "5201"))

# Service registry — loaded from services.json, with hardcoded fallback
SERVICES_FILE = Path(__file__).parent / "services.json"


def _load_services() -> tuple[dict, dict]:
    """Load service registry and infra config from JSON config."""
    if SERVICES_FILE.exists():
        with open(SERVICES_FILE, "r") as f:
            data = json.load(f)
        return data.get("services", {}), data.get("infra", {})
    # Fallback if JSON missing
    return {
        "HekateOrchestration":    {"port": 5200, "health": "http://localhost:5200/api/health", "managed": True, "group": "core"},
        "HekateContextStore":     {"port": 5102, "health": "http://localhost:5102/api/health", "managed": True, "group": "core"},
        "HekateServer":           {"port": 5110, "health": None, "managed": True, "group": "mcp"},
        "HekatePythonWorker":     {"port": 9200, "health": None, "managed": True, "group": "mcp"},
        "HekateTypeScriptWorker": {"port": 9202, "health": None, "managed": True, "group": "mcp"},
        "HekateCppWorker":        {"port": 9201, "health": None, "managed": True, "group": "mcp"},
        "HekateAdmin":            {"port": 5201, "health": "http://localhost:5201/health", "managed": False, "group": "admin"},
        "Ollama":                 {"port": 11434, "health": "http://localhost:11434/", "managed": False, "group": "external"},
        "ComfyUI":                {"port": 8188, "health": "http://localhost:8188/", "managed": False, "group": "external"},
    }, {}


def _save_services(services: dict) -> None:
    """Persist service registry to JSON config (preserves infra section)."""
    data = {"infra": INFRA, "services": services}
    with open(SERVICES_FILE, "w") as f:
        json.dump(data, f, indent=2)


SERVICES, INFRA = _load_services()
CORE_SERVICES = [name for name, info in SERVICES.items() if info.get("group") == "core"]

log = logging.getLogger("hades")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)

# ---------------------------------------------------------------------------
# Infrastructure — Docker + Postgres readiness
# ---------------------------------------------------------------------------

def _docker_daemon_ready() -> bool:
    """Return True if the Docker daemon is accepting connections."""
    r = _run(["docker", "info"], timeout=10)
    return r["returncode"] == 0


async def _ensure_docker() -> dict:
    """Ensure Docker Desktop is running. Start it if not, wait up to startup_timeout."""
    if _docker_daemon_ready():
        return {"docker": "already_running"}

    desktop = INFRA.get("docker_desktop", r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
    timeout = INFRA.get("startup_timeout", 90)

    if not Path(desktop).exists():
        return {"docker": "error", "detail": f"Docker Desktop not found at {desktop}"}

    log.info("Docker not running — launching Docker Desktop")
    subprocess.Popen([desktop], shell=False)

    for i in range(timeout):
        await asyncio.sleep(1)
        if _docker_daemon_ready():
            log.info("Docker daemon ready after %ds", i + 1)
            return {"docker": "started", "wait_seconds": i + 1}

    return {"docker": "timeout", "detail": f"Docker daemon not ready after {timeout}s"}


async def _ensure_postgres() -> dict:
    """Ensure the Postgres container is running via docker compose."""
    container = INFRA.get("postgres_container", "code-storage-db")
    compose_file = INFRA.get("compose_file", "")

    # Check if container is already running
    r = _run(["docker", "inspect", "--format", "{{.State.Running}}", container], timeout=10)
    if r["returncode"] == 0 and r["stdout"].strip() == "true":
        return {"postgres": "already_running", "container": container}

    # Start via docker compose
    if not compose_file or not Path(compose_file).exists():
        return {"postgres": "error", "detail": f"compose_file not found: {compose_file}"}

    compose_dir = str(Path(compose_file).parent)
    log.info("Starting Postgres container via docker compose in %s", compose_dir)
    r = _run(["docker", "compose", "up", "-d"], timeout=30, cwd=compose_dir)

    if r["returncode"] != 0:
        return {"postgres": "error", "detail": r["stderr"] or r["stdout"]}

    # Wait for container health
    port = INFRA.get("postgres_port", 5433)
    for i in range(30):
        await asyncio.sleep(1)
        r = _run(["docker", "inspect", "--format", "{{.State.Running}}", container], timeout=5)
        if r["returncode"] == 0 and r["stdout"].strip() == "true":
            log.info("Postgres container ready after %ds on port %d", i + 1, port)
            return {"postgres": "started", "container": container, "wait_seconds": i + 1}

    return {"postgres": "timeout", "detail": f"container '{container}' not running after 30s"}


async def _ensure_infra() -> dict:
    """Ensure Docker and Postgres are ready. Called before starting core services."""
    docker_result = await _ensure_docker()
    if docker_result.get("docker") in ("error", "timeout"):
        return {"ok": False, **docker_result}

    postgres_result = await _ensure_postgres()
    if postgres_result.get("postgres") in ("error", "timeout"):
        return {"ok": False, **docker_result, **postgres_result}

    return {"ok": True, **docker_result, **postgres_result}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 30, cwd: str | None = None) -> dict:
    """Run a subprocess and return structured result."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"Timeout after {timeout}s"}
    except FileNotFoundError:
        return {"returncode": -1, "stdout": "", "stderr": f"Command not found: {cmd[0]}"}


def _nssm_status(service: str) -> str:
    """Get NSSM service status."""
    result = _run(["nssm", "status", service])
    if result["returncode"] == 0:
        return result["stdout"].strip()
    return result["stderr"] or "UNKNOWN"


async def _stop_and_wait(service: str, timeout: int = 20) -> str:
    """Stop a service via NSSM and poll until SERVICE_STOPPED or timeout."""
    _run(["nssm", "stop", service], timeout=15)
    for _ in range(timeout):
        status = _nssm_status(service)
        if status == "SERVICE_STOPPED":
            return status
        await asyncio.sleep(1)
    return _nssm_status(service)


async def _start_and_health(service: str, retries: int = 15) -> dict:
    """Start a service and wait for health check to pass."""
    _run(["nssm", "start", service], timeout=15)
    info = SERVICES.get(service, {})
    health_url = info.get("health")
    if not health_url:
        await asyncio.sleep(1)
        return {"nssm_status": _nssm_status(service)}

    for _ in range(retries):
        await asyncio.sleep(1)
        status = _nssm_status(service)
        if status == "SERVICE_RUNNING":
            h = await _health_check(health_url)
            if h["status"] == "ok":
                return {"nssm_status": status, "health": h}
    return {"nssm_status": _nssm_status(service), "health": await _health_check(health_url)}


def _validate_service(name: str) -> None:
    """Raise 404 if service name is not in the whitelist."""
    if name not in SERVICES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown service '{name}'. Valid: {', '.join(sorted(SERVICES.keys()))}",
        )


async def _health_check(url: str, timeout: float = 3.0) -> dict:
    """Check an HTTP health endpoint."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            return {"status": "ok", "code": resp.status_code}
    except httpx.ConnectError:
        return {"status": "unreachable"}
    except httpx.TimeoutException:
        return {"status": "timeout"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Hades starting on port %d", PORT)
    log.info("HEKATE_ROOT: %s", HEKATE_ROOT)
    log.info("SOURCE_ROOT: %s", SOURCE_ROOT)
    yield
    log.info("Hades shutting down")


app = FastAPI(
    title="Hades",
    description="Privileged admin service for the Hekate production environment",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ServiceAction(BaseModel):
    service: str

class DeployRequest(BaseModel):
    skip_frontend: bool = False

class TailRequest(BaseModel):
    service: str
    lines: int = 50

class ClearCacheRequest(BaseModel):
    path: Optional[str] = None  # defaults to HEKATE_ROOT/orchestration

class ExecRequest(BaseModel):
    command: str
    cwd: Optional[str] = None  # defaults to SOURCE_ROOT
    timeout: int = 120  # seconds, max 600
    shell: str = "bash"  # "bash", "cmd", "powershell"

class CreateServiceRequest(BaseModel):
    name: str              # must start with "Hekate"
    app: str               # path to executable
    app_args: str = ""
    app_dir: str = ""      # defaults to exe's parent
    port: Optional[int] = None
    health: Optional[str] = None
    group: str = "custom"
    managed: bool = True
    env_vars: Optional[dict[str, str]] = None
    stdout_log: Optional[str] = None
    stderr_log: Optional[str] = None


# ---------------------------------------------------------------------------
# Routes — Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "hades", "port": PORT}


# ---------------------------------------------------------------------------
# Routes — Service Status
# ---------------------------------------------------------------------------

@app.get("/services")
async def list_services():
    """Get status of all known NSSM services."""
    results = {}
    for name, info in SERVICES.items():
        status = _nssm_status(name)
        entry = {"nssm_status": status, "port": info.get("port")}

        # Health check for running services with health endpoints
        if status == "SERVICE_RUNNING" and info.get("health"):
            entry["health"] = await _health_check(info["health"])

        results[name] = entry

    return {"services": results}


@app.get("/services/sync-check")
async def sync_check():
    """Compare JSON config vs actual NSSM state. Reports drift."""
    result = _run(["powershell", "-NoProfile", "-Command",
                    "Get-Service -Name 'Hekate*' | Select-Object -ExpandProperty Name"])

    nssm_services = set()
    if result["returncode"] == 0 and result["stdout"]:
        nssm_services = {s.strip() for s in result["stdout"].splitlines() if s.strip()}

    config_services = set(SERVICES.keys())
    config_hekate = {s for s in config_services if s.startswith("Hekate")}

    in_nssm_not_config = nssm_services - config_hekate
    in_config_not_nssm = config_hekate - nssm_services
    in_both = nssm_services & config_hekate

    return {
        "synced": len(in_nssm_not_config) == 0 and len(in_config_not_nssm) == 0,
        "in_both": sorted(in_both),
        "in_nssm_only": sorted(in_nssm_not_config),
        "in_config_only": sorted(in_config_not_nssm),
        "external_services": sorted(config_services - config_hekate),
    }


@app.get("/services/{name}")
async def get_service(name: str):
    """Get status of a specific NSSM service."""
    _validate_service(name)
    info = SERVICES[name]
    status = _nssm_status(name)
    result = {"service": name, "nssm_status": status, "port": info.get("port")}

    if status == "SERVICE_RUNNING" and info.get("health"):
        result["health"] = await _health_check(info["health"])

    return result


@app.post("/services")
async def create_service(req: CreateServiceRequest):
    """Provision a new NSSM service and register it in the config."""
    if req.name in SERVICES:
        raise HTTPException(409, f"Service '{req.name}' already exists in registry")

    if not req.name.startswith("Hekate"):
        raise HTTPException(400, "Service name must start with 'Hekate'")

    if not Path(req.app).exists():
        raise HTTPException(400, f"Application not found: {req.app}")

    app_dir = req.app_dir or str(Path(req.app).parent)

    # 1. nssm install
    install_cmd = ["nssm", "install", req.name, req.app]
    if req.app_args:
        install_cmd.append(req.app_args)
    result = _run(install_cmd)
    if result["returncode"] != 0:
        raise HTTPException(500, f"nssm install failed: {result['stderr']}")

    # 2. Set AppDirectory
    _run(["nssm", "set", req.name, "AppDirectory", app_dir])

    # 3. Log paths
    if req.stdout_log:
        _run(["nssm", "set", req.name, "AppStdout", req.stdout_log])
    if req.stderr_log:
        _run(["nssm", "set", req.name, "AppStderr", req.stderr_log])

    # 4. Environment variables
    if req.env_vars:
        env_str = " ".join(f"{k}={v}" for k, v in req.env_vars.items())
        _run(["nssm", "set", req.name, "AppEnvironmentExtra", env_str])

    # 5. Register in config
    entry = {
        "port": req.port,
        "health": req.health,
        "app": req.app,
        "app_args": req.app_args,
        "app_dir": app_dir,
        "group": req.group,
        "managed": req.managed,
    }
    SERVICES[req.name] = entry
    _save_services(SERVICES)

    log.info("Created service: %s -> %s", req.name, req.app)
    return {"action": "create", "service": req.name, "nssm_status": _nssm_status(req.name), **entry}


@app.delete("/services/{name}")
async def remove_service(name: str, confirm: bool = False):
    """Remove an NSSM service and unregister from config."""
    _validate_service(name)

    if not confirm:
        raise HTTPException(400, "Pass ?confirm=true to actually remove the service")

    if name == "HekateAdmin":
        raise HTTPException(400, "Cannot remove the admin service from itself")

    # Stop if running
    status = _nssm_status(name)
    if status == "SERVICE_RUNNING":
        await _stop_and_wait(name)

    # nssm remove
    result = _run(["nssm", "remove", name, "confirm"])
    if result["returncode"] != 0:
        raise HTTPException(500, f"nssm remove failed: {result['stderr']}")

    # Remove from config
    SERVICES.pop(name, None)
    _save_services(SERVICES)

    log.info("Removed service: %s", name)
    return {"action": "remove", "service": name, "status": "removed"}


# ---------------------------------------------------------------------------
# Routes — Service Control
# ---------------------------------------------------------------------------

@app.post("/services/{name}/restart")
async def restart_service(name: str):
    """Restart an NSSM service."""
    _validate_service(name)

    # Self-restart: exit the process and let NSSM bring us back
    if name == "HekateAdmin":
        log.info("Self-restart requested — exiting for NSSM to restart")
        # Respond first, then exit after a short delay
        import threading
        threading.Timer(1.0, lambda: os._exit(0)).start()
        return {"service": name, "action": "self_restart", "detail": "Exiting for NSSM restart"}

    log.info("Restarting service: %s", name)
    stop_status = await _stop_and_wait(name)
    infra = {}
    if SERVICES.get(name, {}).get("group") == "core":
        infra = await _ensure_infra()
        log.info("Infra readiness: %s", infra)
    start_result = await _start_and_health(name)
    result = {"service": name, "action": "restart", "stop_status": stop_status, **start_result}
    if infra:
        result["infra"] = infra
    return result


@app.post("/services/{name}/stop")
async def stop_service(name: str):
    """Stop an NSSM service and wait for clean shutdown."""
    _validate_service(name)

    if name == "HekateAdmin":
        raise HTTPException(400, "Cannot stop the admin service from itself")

    log.info("Stopping service: %s", name)
    final_status = await _stop_and_wait(name)
    return {"service": name, "action": "stop", "status": final_status}


@app.post("/services/{name}/start")
async def start_service(name: str):
    """Start an NSSM service and wait for health."""
    _validate_service(name)
    log.info("Starting service: %s", name)
    infra = {}
    if SERVICES.get(name, {}).get("group") == "core":
        infra = await _ensure_infra()
        log.info("Infra readiness: %s", infra)
    result = await _start_and_health(name)
    if infra:
        result["infra"] = infra
    return {"service": name, "action": "start", **result}


# ---------------------------------------------------------------------------
# Routes — Deploy
# ---------------------------------------------------------------------------

@app.post("/deploy")
async def deploy(req: DeployRequest = DeployRequest()):
    """Compound deploy: stop → publish → copy → syntax check → start → health.

    Pure Python — no bash dependency. Safe lifecycle via NSSM stop/wait.
    """
    log.info("Deploy starting (skip_frontend=%s)", req.skip_frontend)
    steps = []

    # --- 1. Stop all managed services and wait for clean shutdown ---
    managed = [s for s, info in SERVICES.items() if info.get("managed", True)]
    stop_results = {}
    for svc in managed:
        status = await _stop_and_wait(svc, timeout=20)
        stop_results[svc] = status
        log.info("Stop %s: %s", svc, status)
    steps.append({"step": "stop_services", "results": stop_results})

    failed_stops = [s for s, st in stop_results.items() if st != "SERVICE_STOPPED"]
    if failed_stops:
        steps.append({"step": "error", "detail": f"Services failed to stop: {failed_stops}"})
        return {"action": "deploy", "success": False, "steps": steps}

    try:
        # --- 2. Publish context store (.NET) ---
        dotnet_cmd = [
            "dotnet", "publish",
            str(SOURCE_ROOT / "context-store" / "Api" / "Api.csproj"),
            "-c", "Release",
            "-o", str(HEKATE_ROOT / "context-store"),
        ]
        dotnet_result = _run(dotnet_cmd, timeout=120, cwd=str(SOURCE_ROOT))
        steps.append({
            "step": "publish_context_store",
            "returncode": dotnet_result["returncode"],
            "output": dotnet_result["stdout"][-1000:],
        })
        if dotnet_result["returncode"] != 0:
            # Check if it actually succeeded despite MSBuild noise
            api_dll = HEKATE_ROOT / "context-store" / "Api.dll"
            if not api_dll.exists():
                steps.append({"step": "error", "detail": "dotnet publish failed"})
                return {"action": "deploy", "success": False, "steps": steps}

        # --- 3. Copy orchestration source ---
        orch_src = SOURCE_ROOT / "orchestration"
        orch_dst = HEKATE_ROOT / "orchestration"
        for subdir in ["backend", "tools"]:
            src = orch_src / subdir
            dst = orch_dst / subdir
            if src.exists():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        for fname in ["run.py", "requirements.txt"]:
            src = orch_src / fname
            if src.exists():
                shutil.copy2(src, orch_dst / fname)
        # Preserve config.json if it exists in target
        src_config = orch_src / "config.json"
        dst_config = orch_dst / "config.json"
        if src_config.exists() and not dst_config.exists():
            shutil.copy2(src_config, dst_config)
        steps.append({"step": "copy_orchestration", "status": "ok"})

        # --- 4. Copy hades ---
        hades_src = SOURCE_ROOT / "hades"
        hades_dst = HEKATE_ROOT / "hades"
        hades_dst.mkdir(parents=True, exist_ok=True)
        for f in hades_src.glob("*.py"):
            shutil.copy2(f, hades_dst / f.name)
        req_file = hades_src / "requirements.txt"
        if req_file.exists():
            shutil.copy2(req_file, hades_dst / "requirements.txt")
        # Seed services.json only if missing (target may have custom services)
        src_svc_json = hades_src / "services.json"
        dst_svc_json = hades_dst / "services.json"
        if src_svc_json.exists() and not dst_svc_json.exists():
            shutil.copy2(src_svc_json, dst_svc_json)
        steps.append({"step": "copy_hades", "status": "ok"})

        # --- 5. Copy scripts ---
        scripts_src = SOURCE_ROOT / "scripts"
        scripts_dst = HEKATE_ROOT / "scripts"
        if scripts_src.exists():
            scripts_dst.mkdir(parents=True, exist_ok=True)
            for f in scripts_src.iterdir():
                if f.is_file():
                    shutil.copy2(f, scripts_dst / f.name)
        steps.append({"step": "copy_scripts", "status": "ok"})

        # --- 6. Sync migrations ---
        mig_src = orch_src / "backend" / "migrations" / "versions"
        mig_dst = orch_dst / "backend" / "migrations" / "versions"
        if mig_src.exists():
            src_count = len(list(mig_src.glob("*.py")))
            dst_count = len(list(mig_dst.glob("*.py"))) if mig_dst.exists() else 0
            if src_count != dst_count:
                if mig_dst.exists():
                    shutil.rmtree(mig_dst)
                shutil.copytree(mig_src, mig_dst)
                steps.append({"step": "sync_migrations", "source": src_count, "target_before": dst_count})
            else:
                steps.append({"step": "sync_migrations", "status": "already_synced", "count": src_count})

        # --- 7. Frontend build (optional) ---
        if not req.skip_frontend:
            frontend_dir = orch_dst / "frontend"
            if frontend_dir.exists() and (frontend_dir / "package.json").exists():
                npm_install = _run(["npm", "install"], timeout=120, cwd=str(frontend_dir))
                npm_build = _run(["npm", "run", "build"], timeout=120, cwd=str(frontend_dir))
                steps.append({
                    "step": "frontend_build",
                    "install_rc": npm_install["returncode"],
                    "build_rc": npm_build["returncode"],
                })
        else:
            steps.append({"step": "frontend_build", "status": "skipped"})

        # --- 8. Install Python dependencies ---
        req_file = orch_dst / "requirements.txt"
        if req_file.exists():
            pip_result = _run(
                [sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)],
                timeout=120,
            )
            steps.append({
                "step": "pip_install",
                "returncode": pip_result["returncode"],
            })

        # --- 9. Python syntax check ---
        py_dir = orch_dst / "backend"
        syntax_errors = []
        if py_dir.exists():
            for pyfile in py_dir.rglob("*.py"):
                if "__pycache__" in str(pyfile):
                    continue
                result = _run([sys.executable, "-c", f"import py_compile; py_compile.compile(r'{pyfile}', doraise=True)"], timeout=10)
                if result["returncode"] != 0:
                    syntax_errors.append(str(pyfile.relative_to(orch_dst)))
        steps.append({"step": "syntax_check", "errors": syntax_errors})
        if syntax_errors:
            steps.append({"step": "error", "detail": f"Syntax errors in {len(syntax_errors)} files — not starting services"})
            return {"action": "deploy", "success": False, "steps": steps}

    except Exception as e:
        steps.append({"step": "error", "detail": str(e)})
        # Still try to start services even on copy failure
        log.error("Deploy error: %s", e, exc_info=True)

    # --- 9. Ensure infrastructure (Docker + Postgres) before starting ---
    infra = await _ensure_infra()
    steps.append({"step": "ensure_infra", **infra})
    if not infra.get("ok"):
        steps.append({"step": "error", "detail": "Infrastructure not ready — services not started"})
        return {"action": "deploy", "success": False, "steps": steps}

    # --- 10. Start all services and health check ---
    start_results = {}
    for svc in managed:
        result = await _start_and_health(svc)
        start_results[svc] = result
        log.info("Start %s: %s", svc, result)
    steps.append({"step": "start_services", "results": start_results})

    all_healthy = all(
        r.get("health", {}).get("status") == "ok"
        for name, r in start_results.items()
        if SERVICES.get(name, {}).get("health")
    )

    log.info("Deploy complete — healthy: %s", all_healthy)
    return {"action": "deploy", "success": all_healthy, "steps": steps}


# ---------------------------------------------------------------------------
# Routes — Command Execution
# ---------------------------------------------------------------------------

@app.post("/exec")
async def exec_command(req: ExecRequest):
    """Execute a shell command. Supports bash, cmd, powershell.

    This is the general-purpose execution endpoint — covers bash, npm,
    python, git, dotnet, and anything else on PATH.
    """
    timeout = min(req.timeout, 600)
    cwd = req.cwd or str(SOURCE_ROOT)

    if not Path(cwd).exists():
        raise HTTPException(400, f"Working directory not found: {cwd}")

    # Use Git Bash explicitly — LocalSystem's "bash" resolves to WSL which
    # doesn't support running as LocalSystem.
    git_bash = r"C:\Program Files\Git\bin\bash.exe"
    shell_map = {
        "bash": [git_bash, "-c", req.command],
        "cmd": ["cmd", "/c", req.command],
        "powershell": ["powershell", "-NoProfile", "-Command", req.command],
    }
    cmd = shell_map.get(req.shell)
    if cmd is None:
        raise HTTPException(400, f"Unknown shell '{req.shell}'. Valid: bash, cmd, powershell")

    log.info("Exec [%s] cwd=%s timeout=%d: %s", req.shell, cwd, timeout, req.command[:200])
    result = _run(cmd, timeout=timeout, cwd=cwd)
    log.info("Exec finished with returncode %d", result["returncode"])

    return {
        "command": req.command,
        "shell": req.shell,
        "cwd": cwd,
        "returncode": result["returncode"],
        "stdout": result["stdout"][-10000:],
        "stderr": result["stderr"][-3000:] if result["stderr"] else None,
    }


# ---------------------------------------------------------------------------
# Routes — Log Tailing
# ---------------------------------------------------------------------------

@app.post("/logs/tail")
async def tail_logs(req: TailRequest):
    """Tail NSSM service log files."""
    _validate_service(req.service)

    # NSSM logs go to stdout/stderr files configured per service
    # Query NSSM for the log paths
    stdout_result = _run(["nssm", "get", req.service, "AppStdout"])
    stderr_result = _run(["nssm", "get", req.service, "AppStderr"])

    logs = {}

    for label, result in [("stdout", stdout_result), ("stderr", stderr_result)]:
        path = result["stdout"].strip()
        if path and Path(path).exists():
            try:
                # Read last N lines
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                    tail = all_lines[-req.lines:]
                logs[label] = {
                    "path": path,
                    "lines": len(tail),
                    "total_lines": len(all_lines),
                    "content": "".join(tail),
                }
            except Exception as e:
                logs[label] = {"path": path, "error": str(e)}
        elif path:
            logs[label] = {"path": path, "error": "File not found"}
        else:
            logs[label] = {"error": "No log path configured in NSSM"}

    return {"service": req.service, "logs": logs}


@app.get("/logs/{service}")
async def get_logs(service: str, lines: int = 50):
    """GET shorthand for log tailing."""
    return await tail_logs(TailRequest(service=service, lines=lines))


# ---------------------------------------------------------------------------
# Routes — Cache Management
# ---------------------------------------------------------------------------

@app.post("/clear-pycache")
async def clear_pycache(req: ClearCacheRequest = ClearCacheRequest()):
    """Recursively delete __pycache__ directories."""
    target = Path(req.path) if req.path else HEKATE_ROOT / "orchestration"

    if not target.exists():
        raise HTTPException(404, f"Path not found: {target}")

    # Safety: only allow clearing under known roots
    target_resolved = target.resolve()
    allowed_roots = [HEKATE_ROOT.resolve(), SOURCE_ROOT.resolve()]
    if not any(str(target_resolved).startswith(str(r)) for r in allowed_roots):
        raise HTTPException(
            403,
            f"Path must be under {HEKATE_ROOT} or {SOURCE_ROOT}",
        )

    removed = []
    for cache_dir in target.rglob("__pycache__"):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)
            removed.append(str(cache_dir))

    log.info("Cleared %d __pycache__ dirs under %s", len(removed), target)
    return {"action": "clear_pycache", "target": str(target), "removed": len(removed), "paths": removed[:20]}


# ---------------------------------------------------------------------------
# Routes — Bulk Operations
# ---------------------------------------------------------------------------

@app.post("/restart-core")
async def restart_core():
    """Restart core services (Orchestration + Context Store) with health checks."""
    log.info("Restarting core services")

    # Stop all core, wait for clean shutdown
    stop_results = {}
    for svc in CORE_SERVICES:
        stop_results[svc] = await _stop_and_wait(svc)

    # Ensure Docker + Postgres are up before starting
    infra = await _ensure_infra()
    log.info("Infra readiness: %s", infra)

    # Start all core, wait for health
    start_results = {}
    for svc in CORE_SERVICES:
        start_results[svc] = await _start_and_health(svc)

    return {"action": "restart_core", "infra": infra, "stopped": stop_results, "started": start_results}


@app.post("/restart-all")
async def restart_all():
    """Restart all managed services (those with managed=true in config)."""
    log.info("Restarting all Hekate services")
    managed = [s for s, info in SERVICES.items() if info.get("managed", True)]

    # Stop all, wait for clean shutdown
    stop_results = {}
    for svc in managed:
        stop_results[svc] = await _stop_and_wait(svc)

    # Ensure Docker + Postgres are up before starting
    infra = await _ensure_infra()
    log.info("Infra readiness: %s", infra)

    # Start all, wait for health
    start_results = {}
    for svc in managed:
        start_results[svc] = await _start_and_health(svc)

    return {"action": "restart_all", "infra": infra, "stopped": stop_results, "started": start_results}


# ---------------------------------------------------------------------------
# Routes — System Info
# ---------------------------------------------------------------------------

@app.get("/info")
async def system_info():
    """Basic system information."""
    import platform
    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python": sys.version,
        "hekate_root": str(HEKATE_ROOT),
        "source_root": str(SOURCE_ROOT),
        "port": PORT,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
