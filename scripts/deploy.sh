#!/bin/bash
# Hekate — deploy from source repo to C:\Hekate (NSSM service directory)
# Usage: bash scripts/deploy.sh
#
# Source: C:\Users\jruss\Documents\GitHub\Hekate (git repo)
# Target: C:\Hekate (NSSM service directory)
#
# Deploys:
#   - Context Store: dotnet publish (binary)
#   - Orchestration: full source copy + DB sync + migration sync
#   - Scripts: copy
#
# Preserves:
#   - config.json (never overwritten if it exists)
#   - .hekate index directories
#
# Requires: admin terminal for NSSM service management

set -e

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

# ---------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------
echo -e "${YELLOW}Pre-flight checks...${NC}"

# Verify source exists
if [ ! -d "$SOURCE/orchestration" ]; then
    echo -e "  ${RED}Source repo not found at $SOURCE${NC}"
    exit 1
fi

# Verify target exists
if [ ! -d "$TARGET" ]; then
    echo -e "  ${RED}Target directory not found at $TARGET${NC}"
    exit 1
fi

# Check node is on system PATH
if ! command -v node &> /dev/null; then
    echo -e "  ${RED}node not found on PATH — add C:\\Program Files\\nodejs to system PATH${NC}"
    exit 1
fi

# Check dotnet
if ! command -v dotnet &> /dev/null; then
    echo -e "  ${RED}dotnet not found on PATH${NC}"
    exit 1
fi

echo -e "  ${GREEN}OK${NC}"

# ---------------------------------------------------------------
# 1. Stop services
# ---------------------------------------------------------------
echo -e "${YELLOW}Stopping services...${NC}"
for svc in HekateOrchestration HekateContextStore HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker; do
    nssm stop "$svc" > /dev/null 2>&1 || true
done
sleep 2
echo -e "  ${RED}Stopped${NC}"

# ---------------------------------------------------------------
# 2. Publish Context Store (dotnet publish → binary)
# ---------------------------------------------------------------
echo -e "${YELLOW}Publishing context store...${NC}"
cd "$SOURCE/context-store/Api"
dotnet publish -c Release -o "$TARGET/context-store/" -q 2>&1
echo -e "  ${GREEN}Published${NC}"

# ---------------------------------------------------------------
# 3. Copy Orchestration (full source — Python runs from source)
# ---------------------------------------------------------------
echo -e "${YELLOW}Copying orchestration source...${NC}"

# Copy all Python source, templates, knowledge, tools
# Preserve: data/ (DB), config.json, .worktrees/
robocopy "$SOURCE\\orchestration\\backend" "$TARGET\\orchestration\\backend" /MIR \
    /XD __pycache__ > /dev/null 2>&1 || true

robocopy "$SOURCE\\orchestration\\tools" "$TARGET\\orchestration\\tools" /MIR \
    /XD __pycache__ > /dev/null 2>&1 || true

robocopy "$SOURCE\\orchestration\\tests" "$TARGET\\orchestration\\tests" /MIR \
    /XD __pycache__ > /dev/null 2>&1 || true

# Copy root files (run.py, requirements.txt, etc)
cp "$SOURCE/orchestration/run.py" "$TARGET/orchestration/" 2>/dev/null
cp "$SOURCE/orchestration/requirements.txt" "$TARGET/orchestration/" 2>/dev/null
cp "$SOURCE/orchestration/Dockerfile" "$TARGET/orchestration/" 2>/dev/null

echo -e "  ${GREEN}Source copied${NC}"

# ---------------------------------------------------------------
# 4. Sync migrations explicitly (robocopy /MIR handles this but verify)
# ---------------------------------------------------------------
echo -e "${YELLOW}Syncing migrations...${NC}"
SRC_COUNT=$(ls "$SOURCE/orchestration/backend/migrations/versions/"*.py 2>/dev/null | wc -l)
TGT_COUNT=$(ls "$TARGET/orchestration/backend/migrations/versions/"*.py 2>/dev/null | wc -l)
echo -e "  Source: $SRC_COUNT  Target: $TGT_COUNT"
if [ "$SRC_COUNT" != "$TGT_COUNT" ]; then
    cp "$SOURCE/orchestration/backend/migrations/versions/"*.py "$TARGET/orchestration/backend/migrations/versions/"
    echo -e "  ${GREEN}Synced${NC}"
else
    echo -e "  ${GREEN}Already in sync${NC}"
fi

# ---------------------------------------------------------------
# 5. Sync DB (copy source DB to target if target is older or missing)
# ---------------------------------------------------------------
echo -e "${YELLOW}Syncing database...${NC}"
SRC_DB="$SOURCE/orchestration/data/orchestration.db"
TGT_DB="$TARGET/orchestration/data/orchestration.db"

mkdir -p "$TARGET/orchestration/data"

if [ ! -f "$TGT_DB" ]; then
    cp "$SRC_DB" "$TGT_DB"
    cp "${SRC_DB}-wal" "$TARGET/orchestration/data/" 2>/dev/null || true
    cp "${SRC_DB}-shm" "$TARGET/orchestration/data/" 2>/dev/null || true
    echo -e "  ${GREEN}Copied (target was missing)${NC}"
elif [ "$SRC_DB" -nt "$TGT_DB" ]; then
    cp "$SRC_DB" "$TGT_DB"
    cp "${SRC_DB}-wal" "$TARGET/orchestration/data/" 2>/dev/null || true
    cp "${SRC_DB}-shm" "$TARGET/orchestration/data/" 2>/dev/null || true
    echo -e "  ${GREEN}Copied (source is newer)${NC}"
else
    echo -e "  ${GREEN}Target DB is current${NC}"
fi

# ---------------------------------------------------------------
# 6. Config (copy only if target doesn't have one)
# ---------------------------------------------------------------
echo -e "${YELLOW}Config...${NC}"
if [ ! -f "$TARGET/orchestration/config.json" ]; then
    if [ -f "$SOURCE/orchestration/config.json" ]; then
        cp "$SOURCE/orchestration/config.json" "$TARGET/orchestration/"
        echo -e "  ${GREEN}Copied from source${NC}"
    elif [ -f "$TARGET/orchestration/config.example.json" ]; then
        cp "$TARGET/orchestration/config.example.json" "$TARGET/orchestration/config.json"
        echo -e "  ${YELLOW}Created from example — edit config.json${NC}"
    fi
else
    echo -e "  ${GREEN}Exists (preserved)${NC}"
fi

# ---------------------------------------------------------------
# 7. Copy scripts
# ---------------------------------------------------------------
echo -e "${YELLOW}Copying scripts...${NC}"
mkdir -p "$TARGET/scripts"
cp "$SOURCE/scripts/"* "$TARGET/scripts/" 2>/dev/null
echo -e "  ${GREEN}Copied${NC}"

# ---------------------------------------------------------------
# 8. Build orchestration frontend
# ---------------------------------------------------------------
echo -e "${YELLOW}Building orchestration frontend...${NC}"
if [ -d "$TARGET/orchestration/frontend/node_modules" ]; then
    cd "$TARGET/orchestration/frontend"
    npm run build --silent 2>&1 | tail -1
else
    cd "$TARGET/orchestration/frontend"
    npm install --silent 2>&1 | tail -1
    npm run build --silent 2>&1 | tail -1
fi
echo -e "  ${GREEN}Built${NC}"

# ---------------------------------------------------------------
# 9. Start services
# ---------------------------------------------------------------
echo -e "${YELLOW}Starting services...${NC}"
for svc in HekateContextStore HekateOrchestration HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker; do
    nssm start "$svc" > /dev/null 2>&1 || true
done

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

# ---------------------------------------------------------------
# 10. Verify
# ---------------------------------------------------------------
echo ""
echo -e "${YELLOW}Verification:${NC}"
curl -s http://localhost:5200/api/services \
    -H "Authorization: Bearer orch_7e2939b473d112d3a6072164d0bebc38983f5fc1c032736c" 2>/dev/null | python -c "
import sys,json
try:
    for s in json.load(sys.stdin):
        if s.get('category') in ('ai','cli'):
            status = s['status']
            color = '\033[0;32m' if status == 'online' else '\033[0;31m'
            print(f'  {s[\"name\"]}: {color}{status}\033[0m')
except: pass
" 2>/dev/null

echo ""
echo -e "${GREEN}=== Deploy complete ===${NC}"
