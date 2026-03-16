#!/bin/bash
# Hekate — deploy from source repo to C:\Hekate service directory
# Usage: bash scripts/deploy.sh
#
# Source: C:\Users\jruss\Documents\GitHub\Hekate (git repo)
# Target: C:\Hekate (NSSM service directory — published binaries + Python source)
#
# What this does:
#   1. Stops NSSM services
#   2. Publishes context store (dotnet publish → C:\Hekate\context-store\)
#   3. Copies orchestration Python source (preserves data/config)
#   4. Restarts NSSM services

SOURCE="C:/Users/jruss/Documents/GitHub/Hekate"
TARGET="C:/Hekate"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}=== Hekate Deploy ===${NC}"
echo "  Source: $SOURCE"
echo "  Target: $TARGET"
echo ""

# 1. Stop services
echo -e "${YELLOW}Stopping services...${NC}"
for svc in HekateOrchestration HekateContextStore HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker; do
    nssm stop "$svc" > /dev/null 2>&1
done
sleep 2
echo -e "  ${RED}Stopped${NC}"

# 2. Publish context store
echo -e "${YELLOW}Publishing context store...${NC}"
cd "$SOURCE/context-store/Api"
dotnet publish -c Release -o "$TARGET/context-store/" 2>&1 | tail -3
echo -e "  ${GREEN}Published${NC}"

# 3. Copy orchestration source (preserve data, config, DB)
echo -e "${YELLOW}Copying orchestration...${NC}"
robocopy "$SOURCE\\orchestration" "$TARGET\\orchestration" /MIR \
    /XD data __pycache__ .worktrees node_modules "frontend\\node_modules" \
    /XF config.json "*.db" "*.db-wal" "*.db-shm" \
    > /dev/null 2>&1
echo -e "  ${GREEN}Copied${NC}"

# 4. Copy scripts
echo -e "${YELLOW}Copying scripts...${NC}"
mkdir -p "$TARGET/scripts"
cp "$SOURCE/scripts/"* "$TARGET/scripts/" 2>/dev/null
echo -e "  ${GREEN}Copied${NC}"

# 5. Build orchestration frontend
echo -e "${YELLOW}Building orchestration frontend...${NC}"
cd "$TARGET/orchestration/frontend"
if [ -d "node_modules" ]; then
    npm run build --silent 2>&1 | tail -1
else
    npm install --silent 2>&1 | tail -1
    npm run build --silent 2>&1 | tail -1
fi
echo -e "  ${GREEN}Built${NC}"

# 6. Restart services
echo -e "${YELLOW}Starting services...${NC}"
for svc in HekateContextStore HekateOrchestration HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker; do
    nssm start "$svc" > /dev/null 2>&1
done

# Health check
echo -n "  Health checks"
for i in $(seq 1 15); do
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
