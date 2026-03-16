#!/bin/bash
# Hekate — stop and restart all services
# Usage: bash scripts/restart.sh [stop|start|restart]
#
# Services:
#   - Orchestration API (port 5200) — Python 3.11
#   - Context Store API (port 5102) — .NET 8

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON311="C:/Users/jruss/AppData/Local/Programs/Python/Python311/python.exe"

# Colors (Windows terminal compatible)
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

stop_services() {
    echo -e "${YELLOW}Stopping services...${NC}"

    # Orchestration — find by port 5200
    ORCH_PID=$(netstat -ano 2>/dev/null | grep ":5200 " | grep LISTEN | awk '{print $5}' | head -1)
    if [ -n "$ORCH_PID" ]; then
        taskkill.exe //PID "$ORCH_PID" //F > /dev/null 2>&1
        echo -e "  Orchestration (PID $ORCH_PID): ${RED}stopped${NC}"
    else
        echo -e "  Orchestration: ${RED}not running${NC}"
    fi

    # Context Store — find by port 5102
    CS_PID=$(netstat -ano 2>/dev/null | grep ":5102 " | grep LISTEN | awk '{print $5}' | head -1)
    if [ -n "$CS_PID" ]; then
        taskkill.exe //PID "$CS_PID" //F > /dev/null 2>&1
        if [ $? -ne 0 ]; then
            # Try by image name if PID kill fails (access denied from different session)
            taskkill.exe //IM Api.exe //F > /dev/null 2>&1
        fi
        echo -e "  Context Store (PID $CS_PID): ${RED}stopped${NC}"
    else
        echo -e "  Context Store: ${RED}not running${NC}"
    fi

    sleep 1
}

start_services() {
    echo -e "${YELLOW}Starting services...${NC}"

    # Context Store
    cd "$REPO_ROOT/context-store"
    dotnet run --project Api/Api.csproj > /dev/null 2>&1 &
    CS_BG=$!

    # Orchestration
    cd "$REPO_ROOT/orchestration"
    "$PYTHON311" run.py > /dev/null 2>&1 &
    ORCH_BG=$!

    # Wait for both to come up
    echo -n "  Waiting"
    for i in $(seq 1 20); do
        ORCH_UP=false
        CS_UP=false
        curl -s http://localhost:5200/api/health > /dev/null 2>&1 && ORCH_UP=true
        curl -s http://localhost:5102/api/health > /dev/null 2>&1 && CS_UP=true

        if $ORCH_UP && $CS_UP; then
            echo ""
            echo -e "  Orchestration: ${GREEN}UP${NC} (port 5200)"
            echo -e "  Context Store: ${GREEN}UP${NC} (port 5102)"
            return 0
        fi
        echo -n "."
        sleep 1
    done

    echo ""
    curl -s http://localhost:5200/api/health > /dev/null 2>&1 \
        && echo -e "  Orchestration: ${GREEN}UP${NC}" \
        || echo -e "  Orchestration: ${RED}FAILED${NC}"
    curl -s http://localhost:5102/api/health > /dev/null 2>&1 \
        && echo -e "  Context Store: ${GREEN}UP${NC}" \
        || echo -e "  Context Store: ${RED}FAILED${NC}"
}

status() {
    echo -e "${YELLOW}Service status:${NC}"
    curl -s http://localhost:5200/api/health > /dev/null 2>&1 \
        && echo -e "  Orchestration: ${GREEN}UP${NC} (port 5200)" \
        || echo -e "  Orchestration: ${RED}DOWN${NC}"
    curl -s http://localhost:5102/api/health > /dev/null 2>&1 \
        && echo -e "  Context Store: ${GREEN}UP${NC} (port 5102)" \
        || echo -e "  Context Store: ${RED}DOWN${NC}"
}

case "${1:-restart}" in
    stop)
        stop_services
        ;;
    start)
        start_services
        ;;
    restart)
        stop_services
        start_services
        ;;
    status)
        status
        ;;
    *)
        echo "Usage: bash scripts/restart.sh [stop|start|restart|status]"
        exit 1
        ;;
esac
