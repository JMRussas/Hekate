#!/bin/bash
# Hekate — stop and restart all services via NSSM
# Usage: bash scripts/restart.sh [stop|start|restart|status]
#
# NSSM Services:
#   - HekateOrchestration (port 5200)
#   - HekateContextStore  (port 5102)
#   - HekateServer        (hekate-mcp)
#   - HekatePythonWorker
#   - HekateTypeScriptWorker
#   - HekateCppWorker
#   - HekateAdmin        (hades — port 5201)
#   - Ollama
#   - ComfyUI

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Core services (always managed)
CORE_SERVICES=(HekateOrchestration HekateContextStore)

# All services (for full restart)
ALL_SERVICES=(HekateOrchestration HekateContextStore HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker HekateAdmin)

# Pick service set
SERVICES=("${CORE_SERVICES[@]}")
if [ "$2" = "--all" ] || [ "$2" = "-a" ]; then
    SERVICES=("${ALL_SERVICES[@]}")
fi

stop_services() {
    echo -e "${YELLOW}Stopping services...${NC}"
    for svc in "${SERVICES[@]}"; do
        nssm stop "$svc" > /dev/null 2>&1
        STATUS=$(nssm status "$svc" 2>/dev/null)
        echo -e "  $svc: ${RED}$STATUS${NC}"
    done
}

start_services() {
    echo -e "${YELLOW}Starting services...${NC}"
    for svc in "${SERVICES[@]}"; do
        nssm start "$svc" > /dev/null 2>&1
        STATUS=$(nssm status "$svc" 2>/dev/null)
        if [ "$STATUS" = "SERVICE_RUNNING" ]; then
            echo -e "  $svc: ${GREEN}UP${NC}"
        else
            echo -e "  $svc: ${RED}$STATUS${NC}"
        fi
    done

    # Wait for health checks
    echo -n "  Health checks"
    for i in $(seq 1 15); do
        ORCH=$(curl -s http://localhost:5200/api/health 2>/dev/null)
        CS=$(curl -s http://localhost:5102/api/health 2>/dev/null)
        if echo "$ORCH" | grep -q "ok" && echo "$CS" | grep -q "ok"; then
            echo -e " ${GREEN}OK${NC}"
            return 0
        fi
        echo -n "."
        sleep 1
    done
    echo -e " ${RED}TIMEOUT${NC}"
}

restart_services() {
    echo -e "${YELLOW}Restarting services...${NC}"
    for svc in "${SERVICES[@]}"; do
        nssm restart "$svc" > /dev/null 2>&1
        STATUS=$(nssm status "$svc" 2>/dev/null)
        if [ "$STATUS" = "SERVICE_RUNNING" ]; then
            echo -e "  $svc: ${GREEN}UP${NC}"
        else
            echo -e "  $svc: ${RED}$STATUS${NC}"
        fi
    done

    echo -n "  Health checks"
    for i in $(seq 1 15); do
        ORCH=$(curl -s http://localhost:5200/api/health 2>/dev/null)
        CS=$(curl -s http://localhost:5102/api/health 2>/dev/null)
        if echo "$ORCH" | grep -q "ok" && echo "$CS" | grep -q "ok"; then
            echo -e " ${GREEN}OK${NC}"
            return 0
        fi
        echo -n "."
        sleep 1
    done
    echo -e " ${RED}TIMEOUT${NC}"
}

status() {
    echo -e "${YELLOW}NSSM Services:${NC}"
    for svc in "${ALL_SERVICES[@]}" HekateAdmin Ollama ComfyUI; do
        STATUS=$(nssm status "$svc" 2>/dev/null)
        if [ "$STATUS" = "SERVICE_RUNNING" ]; then
            echo -e "  $svc: ${GREEN}$STATUS${NC}"
        elif [ "$STATUS" = "SERVICE_STOPPED" ]; then
            echo -e "  $svc: ${RED}$STATUS${NC}"
        else
            echo -e "  $svc: ${YELLOW}${STATUS:-NOT_FOUND}${NC}"
        fi
    done

    echo -e "\n${YELLOW}Health checks:${NC}"
    curl -s http://localhost:5200/api/health > /dev/null 2>&1 \
        && echo -e "  Orchestration (5200): ${GREEN}UP${NC}" \
        || echo -e "  Orchestration (5200): ${RED}DOWN${NC}"
    curl -s http://localhost:5102/api/health > /dev/null 2>&1 \
        && echo -e "  Context Store (5102): ${GREEN}UP${NC}" \
        || echo -e "  Context Store (5102): ${RED}DOWN${NC}"
    curl -s http://localhost:5179 > /dev/null 2>&1 \
        && echo -e "  Context Store UI (5179): ${GREEN}UP${NC}" \
        || echo -e "  Context Store UI (5179): ${RED}DOWN${NC}"
    curl -s http://localhost:5201/health > /dev/null 2>&1 \
        && echo -e "  Hades (5201): ${GREEN}UP${NC}" \
        || echo -e "  Hades (5201): ${RED}DOWN${NC}"
}

case "${1:-restart}" in
    stop)    stop_services ;;
    start)   start_services ;;
    restart) restart_services ;;
    status)  status ;;
    *)
        echo "Usage: bash scripts/restart.sh [stop|start|restart|status] [--all]"
        echo "  Default: restarts core services (Orchestration + Context Store)"
        echo "  --all:   includes MCP server and language workers"
        exit 1
        ;;
esac
