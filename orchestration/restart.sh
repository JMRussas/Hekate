#!/bin/bash
# Orchestration Server — graceful restart
#
# Used by the sentinel system to restart the server when stale code is detected.
# Finds the process listening on port 5200, terminates it, restarts via run.py,
# and polls the health endpoint until ready.
#
# Exit codes:
#   0 — server restarted and healthy
#   1 — timeout or failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=5200
HEALTH_URL="http://localhost:${PORT}/api/health"
SHUTDOWN_TIMEOUT=30
STARTUP_TIMEOUT=60
POLL_INTERVAL=2
PIDFILE="${SCRIPT_DIR}/.orchestration.pid"
RESTART_FLAG="${SCRIPT_DIR}/.restart-needed"
PYTHON="C:/Users/jruss/AppData/Local/Programs/Python/Python311/python.exe"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${YELLOW}[restart]${NC} $*"; }
err() { echo -e "${RED}[restart]${NC} $*" >&2; }
ok()  { echo -e "${GREEN}[restart]${NC} $*"; }

# --- Find PID of process listening on the orchestration port ---
find_server_pid() {
    # Try pidfile first
    if [ -f "$PIDFILE" ]; then
        local pid
        pid=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "$pid"
            return 0
        fi
    fi

    # Fall back to netstat (Windows Git Bash)
    local pid
    pid=$(netstat -ano 2>/dev/null | grep "LISTENING" | grep ":${PORT} " | awk '{print $NF}' | head -1)
    if [ -n "$pid" ] && [ "$pid" != "0" ]; then
        echo "$pid"
        return 0
    fi

    return 1
}

# --- Stop the running server ---
stop_server() {
    local pid
    if ! pid=$(find_server_pid); then
        log "No server process found on port ${PORT}"
        return 0
    fi

    log "Stopping server (PID ${pid})..."

    # Try graceful termination first (taskkill without /F on Windows)
    taskkill //PID "$pid" //T 2>/dev/null || kill "$pid" 2>/dev/null || true

    # Wait for process to exit
    local elapsed=0
    while [ $elapsed -lt $SHUTDOWN_TIMEOUT ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            log "Server stopped after ${elapsed}s"
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done

    # Force kill if still running
    log "Graceful shutdown timed out, force killing..."
    taskkill //PID "$pid" //F //T 2>/dev/null || kill -9 "$pid" 2>/dev/null || true
    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        err "Failed to stop server (PID ${pid})"
        return 1
    fi

    log "Server force-stopped"
    return 0
}

# --- Start the server ---
start_server() {
    log "Starting server..."
    cd "$SCRIPT_DIR"

    # Start in background, save PID
    "$PYTHON" run.py &
    local pid=$!
    echo "$pid" > "$PIDFILE"

    log "Server started (PID ${pid})"
}

# --- Wait for health check ---
wait_for_health() {
    log "Waiting for health check at ${HEALTH_URL}..."

    local elapsed=0
    while [ $elapsed -lt $STARTUP_TIMEOUT ]; do
        local response
        response=$(curl -s --max-time 5 "$HEALTH_URL" 2>/dev/null) || true

        if echo "$response" | grep -q '"status"' && echo "$response" | grep -q '"ok"'; then
            ok "Server healthy after ${elapsed}s"
            return 0
        fi

        sleep $POLL_INTERVAL
        elapsed=$((elapsed + POLL_INTERVAL))
        echo -n "."
    done

    echo ""
    err "Health check timed out after ${STARTUP_TIMEOUT}s"
    return 1
}

# --- Clean up restart flag ---
cleanup_flag() {
    if [ -f "$RESTART_FLAG" ]; then
        rm -f "$RESTART_FLAG"
        log "Removed .restart-needed flag"
    fi
}

# --- Main ---
main() {
    log "=== Orchestration Server Restart ==="

    if ! stop_server; then
        err "Failed to stop server"
        exit 1
    fi

    start_server

    if wait_for_health; then
        cleanup_flag
        ok "=== Restart complete ==="
        exit 0
    else
        err "=== Restart failed — server not healthy ==="
        exit 1
    fi
}

main "$@"
