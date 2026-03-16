#!/bin/bash
# Hekate — deploy latest code to C:\Hekate (NSSM service directory)
# Usage: bash scripts/deploy.sh
#
# Must be run from an admin terminal (NSSM service management requires admin)
#
# What this does:
#   1. Stops all Hekate NSSM services
#   2. Backs up C:\Hekate to C:\Hekate.old
#   3. Clones fresh from GitHub
#   4. Restores data (DB, config, .hekate index)
#   5. Installs dependencies
#   6. Restarts all services

set -e

DEPLOY_DIR="C:/Hekate"
BACKUP_DIR="C:/Hekate.old"
REPO_URL="https://github.com/JMRussas/Hekate.git"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}=== Hekate Deploy ===${NC}"

# 1. Stop services
echo -e "${YELLOW}Stopping services...${NC}"
SERVICES=(HekateOrchestration HekateContextStore HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker)
for svc in "${SERVICES[@]}"; do
    nssm stop "$svc" > /dev/null 2>&1 || true
    echo -e "  $svc: ${RED}stopped${NC}"
done
sleep 2

# 2. Backup
echo -e "${YELLOW}Backing up...${NC}"
if [ -d "$BACKUP_DIR" ]; then
    rm -rf "$BACKUP_DIR"
fi
if [ -d "$DEPLOY_DIR" ]; then
    mv "$DEPLOY_DIR" "$BACKUP_DIR"
    echo -e "  Backed up to ${BACKUP_DIR}"
fi

# 3. Clone
echo -e "${YELLOW}Cloning from GitHub...${NC}"
git clone "$REPO_URL" "$DEPLOY_DIR" 2>&1 | tail -3
echo -e "  ${GREEN}Cloned${NC}"

# 4. Restore data
echo -e "${YELLOW}Restoring data...${NC}"

# Orchestration DB
if [ -f "$BACKUP_DIR/orchestration/data/orchestration.db" ]; then
    mkdir -p "$DEPLOY_DIR/orchestration/data"
    cp "$BACKUP_DIR/orchestration/data/orchestration.db" "$DEPLOY_DIR/orchestration/data/"
    cp "$BACKUP_DIR/orchestration/data/orchestration.db-wal" "$DEPLOY_DIR/orchestration/data/" 2>/dev/null || true
    cp "$BACKUP_DIR/orchestration/data/orchestration.db-shm" "$DEPLOY_DIR/orchestration/data/" 2>/dev/null || true
    echo -e "  Orchestration DB: ${GREEN}restored${NC}"
fi

# Orchestration config
if [ -f "$BACKUP_DIR/orchestration/config.json" ]; then
    cp "$BACKUP_DIR/orchestration/config.json" "$DEPLOY_DIR/orchestration/"
    echo -e "  Orchestration config: ${GREEN}restored${NC}"
fi

# .hekate index directories
for dir in orchestration context-store/Api; do
    if [ -d "$BACKUP_DIR/$dir/.hekate" ]; then
        cp -r "$BACKUP_DIR/$dir/.hekate" "$DEPLOY_DIR/$dir/"
        echo -e "  $dir/.hekate: ${GREEN}restored${NC}"
    fi
done

# Context store docker-compose data (if volume mounts are relative)
if [ -f "$BACKUP_DIR/context-store/docker-compose.yml" ]; then
    echo -e "  Context store DB: ${GREEN}Postgres (Docker volume, no copy needed)${NC}"
fi

# 5. Install dependencies
echo -e "${YELLOW}Installing dependencies...${NC}"

# Python deps
cd "$DEPLOY_DIR/orchestration"
pip install -r requirements.txt -q 2>&1 | tail -1
echo -e "  Python deps: ${GREEN}installed${NC}"

# Context store UI deps
cd "$DEPLOY_DIR/context-store/ui"
npm install --silent 2>&1 | tail -1
echo -e "  UI deps: ${GREEN}installed${NC}"

# Orchestration frontend build
cd "$DEPLOY_DIR/orchestration/frontend"
npm install --silent 2>&1 | tail -1
npm run build --silent 2>&1 | tail -1
echo -e "  Orchestration frontend: ${GREEN}built${NC}"

# 6. Restart services
echo -e "${YELLOW}Starting services...${NC}"
for svc in "${SERVICES[@]}"; do
    nssm start "$svc" > /dev/null 2>&1 || true
done

# Wait for health
echo -n "  Health checks"
for i in $(seq 1 20); do
    ORCH=$(curl -s http://localhost:5200/api/health 2>/dev/null)
    CS=$(curl -s http://localhost:5102/api/health 2>/dev/null)
    if echo "$ORCH" | grep -q "ok" && echo "$CS" | grep -q "ok"; then
        echo -e " ${GREEN}OK${NC}"
        break
    fi
    echo -n "."
    sleep 1
done

echo ""
echo -e "${GREEN}=== Deploy complete ===${NC}"
echo -e "  Orchestration: http://localhost:5200"
echo -e "  Context Store: http://localhost:5102"
echo -e "  Context Store UI: http://localhost:5179"
echo -e "  Backup at: ${BACKUP_DIR}"
