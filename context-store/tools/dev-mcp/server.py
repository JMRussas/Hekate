#!/usr/bin/env python3
#  CodeStoragePoc Dev Environment MCP Server
#
#  Manages the dev environment for CodeStoragePoc: checks service status,
#  starts/stops the API and frontend, verifies DB config and connectivity.
#
#  Tools: check_status, start_api, start_frontend, verify_config, stop_all, build, test_chat
#
#  Depends on: mcp, httpx, psycopg2
#  Used by:    Claude Code (registered via `claude mcp add`)

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import psycopg2
from mcp.server.fastmcp import FastMCP

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # tools/dev-mcp -> tools -> CodeStoragePoc

log = logging.getLogger("dev-mcp")
logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

# --- Configuration ---

CONFIG = {
    "api_port": 5102,
    "api_project": str(PROJECT_ROOT / "Api" / "Api.csproj"),
    "api_health_url": "http://localhost:5102/api/health",
    "frontend_port": 5179,
    "frontend_dir": str(PROJECT_ROOT / "ui"),
    "db_host": "localhost",
    "db_port": 5433,
    "db_name": "code_storage",
    "db_user": "postgres",
    "db_password": "postgres",
    "required_env_vars": ["ANTHROPIC_API_KEY"],
    "project_id": "8196b44e-6299-45a0-a5b0-bbd111f2990b",  # IdeationSessions
}

mcp = FastMCP("codestoragepoc-dev")


# --- Helpers ---

def _port_in_use(port: int) -> bool:
    """Check if a TCP port is currently listening (tries IPv4 and IPv6)."""
    for family, addr in [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")]:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                if s.connect_ex((addr, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def _find_processes(name: str) -> list[dict]:
    """Find processes by name (Windows tasklist)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        processes = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("INFO:"):
                parts = line.replace('"', '').split(",")
                if len(parts) >= 2:
                    processes.append({"name": parts[0], "pid": parts[1]})
        return processes
    except Exception as e:
        return [{"error": str(e)}]


def _kill_processes(name: str) -> str:
    """Kill all processes with the given name."""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", name],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or result.stderr.strip()
    except Exception as e:
        return f"Error: {e}"


def _get_db_connection():
    """Get a psycopg2 connection to the database."""
    return psycopg2.connect(
        host=CONFIG["db_host"],
        port=CONFIG["db_port"],
        dbname=CONFIG["db_name"],
        user=CONFIG["db_user"],
        password=CONFIG["db_password"],
        connect_timeout=5,
    )


def _find_pid_on_port(port: int) -> int | None:
    """Find the PID listening on a given port (Windows)."""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.split("\n"):
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = parts[-1].strip()
                if pid.isdigit():
                    return int(pid)
    except Exception:
        pass
    return None


def _kill_pid(pid: int) -> str:
    """Kill a process by PID."""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or result.stderr.strip()
    except Exception as e:
        return f"Error: {e}"


async def _post_system_message(text: str, level: str = "info") -> dict:
    """Post a system message to the API for display in the UI."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"http://localhost:{CONFIG['api_port']}/api/system-message",
                json={"text": text, "level": level},
            )
            return {"status": "sent", "code": resp.status_code}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


async def _check_http(url: str, timeout: float = 3.0) -> dict:
    """Check an HTTP endpoint and return status."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            return {"status": "ok", "code": resp.status_code, "body": resp.text[:200]}
    except httpx.ConnectError:
        return {"status": "unreachable", "error": "Connection refused"}
    except httpx.TimeoutException:
        return {"status": "timeout", "error": "Request timed out"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# --- MCP Tools ---

@mcp.tool()
async def check_status() -> str:
    """Check the status of all CodeStoragePoc services: Docker/PostgreSQL, API, and Vite frontend.

    Returns a structured report showing which services are running, their ports,
    and whether they're responding correctly.
    """
    report = {"services": {}}

    # 1. Database
    db_status = {"port": CONFIG["db_port"], "listening": _port_in_use(CONFIG["db_port"])}
    if db_status["listening"]:
        try:
            conn = _get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT version()")
            db_status["version"] = cur.fetchone()[0][:80]
            cur.execute("SELECT count(*) FROM projects")
            db_status["project_count"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM nodes")
            db_status["node_count"] = cur.fetchone()[0]
            db_status["status"] = "ok"
            conn.close()
        except Exception as e:
            db_status["status"] = "error"
            db_status["error"] = str(e)
    else:
        db_status["status"] = "not running"
    report["services"]["postgresql"] = db_status

    # 2. API
    api_status = {
        "port": CONFIG["api_port"],
        "listening": _port_in_use(CONFIG["api_port"]),
    }
    if api_status["listening"]:
        health = await _check_http(CONFIG["api_health_url"])
        api_status.update(health)
    else:
        api_status["status"] = "not running"
    api_processes = _find_processes("Api.exe")
    api_status["processes"] = len([p for p in api_processes if "pid" in p])
    report["services"]["api"] = api_status

    # 3. Frontend (Vite)
    fe_status = {
        "port": CONFIG["frontend_port"],
        "listening": _port_in_use(CONFIG["frontend_port"]),
    }
    if fe_status["listening"]:
        fe_health = await _check_http(f"http://localhost:{CONFIG['frontend_port']}/")
        fe_status["status"] = fe_health["status"]
    else:
        fe_status["status"] = "not running"
    report["services"]["vite_frontend"] = fe_status

    # 4. Environment
    env_status = {}
    for var in CONFIG["required_env_vars"]:
        val = os.environ.get(var)
        env_status[var] = "set" if val else "MISSING"
    report["environment"] = env_status

    return json.dumps(report, indent=2)


@mcp.tool()
async def start_api(kill_stale: bool = True) -> str:
    """Kill stale Api.exe processes, build the API project, and start it.

    Args:
        kill_stale: If True, kill any existing Api.exe processes before starting.

    Returns a log of actions taken and the final status.
    """
    log_lines = []

    # 1. Kill stale processes
    if kill_stale:
        existing = _find_processes("Api.exe")
        count = len([p for p in existing if "pid" in p])
        if count > 0:
            log_lines.append(f"Found {count} stale Api.exe process(es), killing...")
            result = _kill_processes("Api.exe")
            log_lines.append(f"  {result}")
            await asyncio.sleep(1)
        else:
            log_lines.append("No stale Api.exe processes found.")

    # 2. Build
    log_lines.append("Building API project...")
    build_result = subprocess.run(
        ["dotnet", "build", CONFIG["api_project"], "-c", "Debug", "--nologo", "-v", "q"],
        capture_output=True, text=True, timeout=120,
        cwd=str(PROJECT_ROOT),
    )
    if build_result.returncode != 0:
        log_lines.append(f"BUILD FAILED:\n{build_result.stderr[-500:]}")
        return "\n".join(log_lines)
    log_lines.append("Build succeeded.")

    # 3. Start (detached)
    log_lines.append("Starting API server...")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
    subprocess.Popen(
        ["dotnet", "run", "--project", CONFIG["api_project"], "--no-build"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )

    # 4. Wait for health
    log_lines.append("Waiting for health check...")
    for i in range(15):
        await asyncio.sleep(1)
        health = await _check_http(CONFIG["api_health_url"])
        if health["status"] == "ok":
            log_lines.append(f"API healthy after {i + 1}s: {health['body']}")
            return "\n".join(log_lines)

    log_lines.append("WARNING: API did not respond to health check within 15s")
    log_lines.append(f"Port {CONFIG['api_port']} listening: {_port_in_use(CONFIG['api_port'])}")
    return "\n".join(log_lines)


@mcp.tool()
async def start_frontend() -> str:
    """Start the Vite dev server for the React frontend if not already running.

    Returns the status of the frontend after attempting to start it.
    """
    # Kill stale process on the port first
    pid = _find_pid_on_port(CONFIG["frontend_port"])
    if pid:
        _kill_pid(pid)
        await asyncio.sleep(1)

    if _port_in_use(CONFIG["frontend_port"]):
        health = await _check_http(f"http://localhost:{CONFIG['frontend_port']}/")
        return f"Frontend already running on port {CONFIG['frontend_port']} (status: {health['status']})"

    # Check node_modules
    node_modules = Path(CONFIG["frontend_dir"]) / "node_modules"
    if not node_modules.exists():
        install_result = subprocess.run(
            ["npm", "install"],
            capture_output=True, text=True, timeout=120,
            cwd=CONFIG["frontend_dir"],
            shell=True,
        )
        if install_result.returncode != 0:
            return f"npm install failed:\n{install_result.stderr[-500:]}"

    # Start Vite — on Windows, use `start` to launch in a separate process
    if sys.platform == "win32":
        subprocess.Popen(
            ["cmd", "/c", "start", "/min", "npm", "run", "dev"],
            cwd=CONFIG["frontend_dir"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=CONFIG["frontend_dir"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    # Wait for it
    for i in range(10):
        await asyncio.sleep(1)
        if _port_in_use(CONFIG["frontend_port"]):
            return f"Frontend started on port {CONFIG['frontend_port']} after {i + 1}s"

    return f"WARNING: Frontend did not start within 10s on port {CONFIG['frontend_port']}"


@mcp.tool()
async def verify_config() -> str:
    """Verify the full CodeStoragePoc configuration: env vars, DB connectivity,
    FK integrity, required tables, and project existence.

    Returns a detailed report of what passed and what failed.
    """
    checks = []

    # 1. Environment variables
    for var in CONFIG["required_env_vars"]:
        val = os.environ.get(var)
        if val:
            checks.append({"check": f"env:{var}", "status": "PASS", "detail": f"Set ({len(val)} chars)"})
        else:
            checks.append({"check": f"env:{var}", "status": "FAIL", "detail": "Not set"})

    # 2. DB connectivity
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        checks.append({"check": "db:connect", "status": "PASS", "detail": f"{CONFIG['db_host']}:{CONFIG['db_port']}/{CONFIG['db_name']}"})

        # 3. Required tables
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        tables = [r[0] for r in cur.fetchall()]
        required = ["projects", "nodes"]
        for t in required:
            if t in tables:
                cur.execute(f"SELECT count(*) FROM {t}")
                count = cur.fetchone()[0]
                checks.append({"check": f"table:{t}", "status": "PASS", "detail": f"{count} rows"})
            else:
                checks.append({"check": f"table:{t}", "status": "FAIL", "detail": "Table does not exist"})

        # 4. Project exists
        cur.execute("SELECT id, name FROM projects WHERE id = %s", (CONFIG["project_id"],))
        row = cur.fetchone()
        if row:
            checks.append({"check": "project:ideation", "status": "PASS", "detail": f"Found: {row[1]}"})
        else:
            checks.append({"check": "project:ideation", "status": "FAIL",
                          "detail": f"Project {CONFIG['project_id']} not found in projects table"})

        # 5. Extensions
        cur.execute("SELECT extname FROM pg_extension")
        extensions = [r[0] for r in cur.fetchall()]
        for ext in ["vector", "age"]:
            if ext in extensions:
                checks.append({"check": f"ext:{ext}", "status": "PASS"})
            else:
                checks.append({"check": f"ext:{ext}", "status": "WARN", "detail": f"Extension '{ext}' not loaded"})

        # 6. FK integrity check on nodes
        cur.execute("""
            SELECT count(*) FROM nodes n
            LEFT JOIN projects p ON p.id = n.project_id
            WHERE p.id IS NULL
        """)
        orphan_count = cur.fetchone()[0]
        if orphan_count == 0:
            checks.append({"check": "fk:nodes_project", "status": "PASS", "detail": "No orphaned nodes"})
        else:
            checks.append({"check": "fk:nodes_project", "status": "FAIL",
                          "detail": f"{orphan_count} nodes reference non-existent projects"})

        conn.close()
    except psycopg2.OperationalError as e:
        checks.append({"check": "db:connect", "status": "FAIL", "detail": str(e).strip()[:200]})

    # Summary
    passed = sum(1 for c in checks if c["status"] == "PASS")
    failed = sum(1 for c in checks if c["status"] == "FAIL")
    warns = sum(1 for c in checks if c["status"] == "WARN")

    result = {
        "summary": f"{passed} passed, {failed} failed, {warns} warnings",
        "all_clear": failed == 0,
        "checks": checks,
    }
    return json.dumps(result, indent=2)


@mcp.tool()
async def stop_all() -> str:
    """Stop all CodeStoragePoc services: kill Api.exe processes and any node
    processes on the frontend port.

    Does NOT stop Docker/PostgreSQL (that's shared infrastructure).
    """
    log_lines = []

    # Kill API
    api_procs = _find_processes("Api.exe")
    count = len([p for p in api_procs if "pid" in p])
    if count > 0:
        result = _kill_processes("Api.exe")
        log_lines.append(f"API: killed {count} process(es) — {result}")
    else:
        log_lines.append("API: no processes running")

    # Kill Vite (process on frontend port)
    pid = _find_pid_on_port(CONFIG["frontend_port"])
    if pid:
        result = _kill_pid(pid)
        log_lines.append(f"Frontend: killed PID {pid} — {result}")
    else:
        log_lines.append("Frontend: not running")

    return "\n".join(log_lines)


@mcp.tool()
async def build() -> str:
    """Build and restart the API, then send a system message to the UI confirming the result.

    This is the recommended way to rebuild after code changes. It:
    1. Kills stale Api.exe processes
    2. Builds the C# project
    3. Starts the API server
    4. Waits for health check
    5. Sends a system message to the UI with the result
    """
    log_lines = []

    # 1. Kill stale
    existing = _find_processes("Api.exe")
    count = len([p for p in existing if "pid" in p])
    if count > 0:
        log_lines.append(f"Killing {count} stale Api.exe process(es)...")
        _kill_processes("Api.exe")
        await asyncio.sleep(1)

    # 2. Build
    log_lines.append("Building API project...")
    build_result = subprocess.run(
        ["dotnet", "build", CONFIG["api_project"], "-c", "Debug", "--nologo", "-v", "q"],
        capture_output=True, text=True, timeout=120,
        cwd=str(PROJECT_ROOT),
    )
    if build_result.returncode != 0:
        error_msg = build_result.stderr[-300:] if build_result.stderr else "Unknown error"
        log_lines.append(f"BUILD FAILED:\n{error_msg}")
        await _post_system_message(f"Build failed: {error_msg[:200]}", "error")
        return "\n".join(log_lines)
    log_lines.append("Build succeeded.")

    # 3. Start
    log_lines.append("Starting API server...")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
    subprocess.Popen(
        ["dotnet", "run", "--project", CONFIG["api_project"], "--no-build"],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )

    # 4. Health check
    for i in range(15):
        await asyncio.sleep(1)
        health = await _check_http(CONFIG["api_health_url"])
        if health["status"] == "ok":
            log_lines.append(f"API healthy after {i + 1}s")
            await _post_system_message(f"Build complete — API healthy ({i + 1}s startup)", "info")
            return "\n".join(log_lines)

    log_lines.append("WARNING: API did not respond within 15s")
    await _post_system_message("Build complete but API not responding — check logs", "warn")
    return "\n".join(log_lines)


@mcp.tool()
async def test_chat(mode: str = "quick") -> str:
    """Test the chat pipeline end-to-end.

    Args:
        mode: "quick" (default) — health check + dry-run preview, no model call.
              "full" — sends a real message through the pipeline (costs API tokens).

    Returns a structured test report.
    """
    results = []

    # 1. Health check
    health = await _check_http(CONFIG["api_health_url"])
    if health["status"] != "ok":
        results.append({"test": "health", "status": "FAIL", "detail": health.get("error", "Not responding")})
        await _post_system_message("Test failed — API not healthy", "error")
        return json.dumps({"results": results, "passed": False}, indent=2)
    results.append({"test": "health", "status": "PASS"})

    # 2. Preview (dry-run pipeline)
    test_message = "What ideas have we discussed so far?"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"http://localhost:{CONFIG['api_port']}/api/preview",
                json={"message": test_message, "conversationId": None},
            )
            if resp.status_code == 200:
                preview = resp.json()
                results.append({
                    "test": "preview",
                    "status": "PASS",
                    "detail": {
                        "intent": preview.get("intent", {}).get("intent"),
                        "model": preview.get("parse", {}).get("model", {}).get("display"),
                        "contextNodes": preview.get("context", {}).get("nodeCount", 0),
                    },
                })
            else:
                results.append({"test": "preview", "status": "FAIL", "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"})
    except Exception as e:
        results.append({"test": "preview", "status": "FAIL", "detail": str(e)})

    # 3. Full mode — send real message through SSE
    if mode == "full":
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream(
                    "POST",
                    f"http://localhost:{CONFIG['api_port']}/api/chat",
                    json={"message": "@haiku Say OK in exactly one word", "conversationId": None},
                ) as resp:
                    events = {}
                    token_count = 0
                    async for line in resp.aiter_lines():
                        if line.startswith("event: "):
                            event_type = line[7:].strip()
                        elif line.startswith("data: ") and event_type:
                            data = json.loads(line[6:])
                            if event_type == "token":
                                token_count += 1
                            elif event_type == "done":
                                events["done"] = True
                            elif event_type == "debug_timing":
                                events["timing"] = data
                            elif event_type == "conversation_id":
                                events["conversation_id"] = data.get("id")
                            event_type = ""

                    if events.get("done") and token_count > 0:
                        results.append({
                            "test": "full_chat",
                            "status": "PASS",
                            "detail": {
                                "tokens": token_count,
                                "timing": events.get("timing"),
                                "conversationId": events.get("conversation_id"),
                            },
                        })
                    else:
                        results.append({
                            "test": "full_chat",
                            "status": "FAIL",
                            "detail": f"Stream incomplete: {token_count} tokens, done={events.get('done', False)}",
                        })
        except Exception as e:
            results.append({"test": "full_chat", "status": "FAIL", "detail": str(e)})

    # Summary
    passed = all(r["status"] == "PASS" for r in results)
    summary = f"{'All tests passed' if passed else 'Some tests failed'} ({len(results)} tests)"

    level = "info" if passed else "error"
    await _post_system_message(f"Test {mode}: {summary}", level)

    return json.dumps({"summary": summary, "passed": passed, "results": results}, indent=2)


if __name__ == "__main__":
    mcp.run()
