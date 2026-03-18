#!/bin/bash
# Hekate — deploy from source repo to C:\Hekate (NSSM service directory)
# Usage: bash scripts/deploy.sh
#
# Can be run from:
#   - Git Bash: bash scripts/deploy.sh
#   - PowerShell (admin): bash scripts/deploy.sh
#   - Any terminal in the source repo directory
#
# Requires: admin terminal for NSSM service management

set -e

# ---------------------------------------------------------------
# Path resolution — works from Git Bash, PowerShell, or cmd
# ---------------------------------------------------------------

# Find source repo from script location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$(cd "$SCRIPT_DIR/.." && pwd)"

# Target is always C:\Hekate — try both path formats
if [ -d "/c/Hekate" ]; then
    TARGET="/c/Hekate"
elif [ -d "C:/Hekate" ]; then
    TARGET="C:/Hekate"
else
    echo "ERROR: C:\\Hekate not found"
    exit 1
fi

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
PREFLIGHT_OK=true

if [ ! -d "$SOURCE/orchestration" ]; then
    echo -e "  ${RED}Source repo missing orchestration/ at $SOURCE${NC}"
    PREFLIGHT_OK=false
fi
if [ ! -d "$TARGET" ]; then
    echo -e "  ${RED}Target $TARGET not found${NC}"
    PREFLIGHT_OK=false
fi
if ! command -v node &> /dev/null; then
    echo -e "  ${RED}node not on PATH${NC}"
    PREFLIGHT_OK=false
fi
if ! command -v dotnet &> /dev/null; then
    echo -e "  ${RED}dotnet not on PATH${NC}"
    PREFLIGHT_OK=false
fi
if ! command -v nssm &> /dev/null; then
    echo -e "  ${YELLOW}nssm not on PATH — service management will fail${NC}"
fi

if [ "$PREFLIGHT_OK" = false ]; then
    exit 1
fi
echo -e "  ${GREEN}OK${NC}"

# ---------------------------------------------------------------
# 1. Stop services
# ---------------------------------------------------------------
echo -e "${YELLOW}Stopping services...${NC}"
for svc in HekateOrchestration HekateContextStore HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker HekateAdmin; do
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
# Copy MCP config + tools + CLAUDE.md for CLI discovery
cp -f "$SOURCE/context-store/.mcp.json" "$TARGET/context-store/.mcp.json" 2>/dev/null || true
cp -f "$SOURCE/context-store/CLAUDE.md" "$TARGET/context-store/CLAUDE.md" 2>/dev/null || true
rm -rf "$TARGET/context-store/tools"
cp -r "$SOURCE/context-store/tools" "$TARGET/context-store/tools"
find "$TARGET/context-store/tools" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
echo -e "  ${GREEN}Published${NC}"

# ---------------------------------------------------------------
# 3. Copy Orchestration source
# ---------------------------------------------------------------
echo -e "${YELLOW}Copying orchestration source...${NC}"

# Use cp -r instead of robocopy for cross-platform compatibility
# Backend
rm -rf "$TARGET/orchestration/backend"
cp -r "$SOURCE/orchestration/backend" "$TARGET/orchestration/backend"
find "$TARGET/orchestration/backend" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Tools
rm -rf "$TARGET/orchestration/tools"
cp -r "$SOURCE/orchestration/tools" "$TARGET/orchestration/tools"

# Tests
rm -rf "$TARGET/orchestration/tests"
cp -r "$SOURCE/orchestration/tests" "$TARGET/orchestration/tests"

# Root files
cp "$SOURCE/orchestration/run.py" "$TARGET/orchestration/" 2>/dev/null || true
cp "$SOURCE/orchestration/requirements.txt" "$TARGET/orchestration/" 2>/dev/null || true
cp "$SOURCE/orchestration/Dockerfile" "$TARGET/orchestration/" 2>/dev/null || true
cp "$SOURCE/orchestration/CLAUDE.md" "$TARGET/orchestration/" 2>/dev/null || true

# Frontend source (for build)
if [ -d "$SOURCE/orchestration/frontend/src" ]; then
    rm -rf "$TARGET/orchestration/frontend/src"
    cp -r "$SOURCE/orchestration/frontend/src" "$TARGET/orchestration/frontend/src"
    cp "$SOURCE/orchestration/frontend/package.json" "$TARGET/orchestration/frontend/" 2>/dev/null || true
    cp "$SOURCE/orchestration/frontend/vite.config.ts" "$TARGET/orchestration/frontend/" 2>/dev/null || true
    cp "$SOURCE/orchestration/frontend/tsconfig"* "$TARGET/orchestration/frontend/" 2>/dev/null || true
    cp "$SOURCE/orchestration/frontend/index.html" "$TARGET/orchestration/frontend/" 2>/dev/null || true
fi

echo -e "  ${GREEN}Copied${NC}"

# ---------------------------------------------------------------
# 3b. Copy Hades
# ---------------------------------------------------------------
echo -e "${YELLOW}Copying hades...${NC}"
mkdir -p "$TARGET/hades"
cp "$SOURCE/hades/"*.py "$TARGET/hades/" 2>/dev/null || true
cp "$SOURCE/hades/requirements.txt" "$TARGET/hades/" 2>/dev/null || true
# Seed services.json only if missing (target may have custom services)
if [ ! -f "$TARGET/hades/services.json" ] && [ -f "$SOURCE/hades/services.json" ]; then
    cp "$SOURCE/hades/services.json" "$TARGET/hades/services.json"
    echo -e "  Seeded services.json"
fi
echo -e "  ${GREEN}Copied${NC}"

# ---------------------------------------------------------------
# 3c. Copy Odin god server (HekateOdin — port 5220)
# ---------------------------------------------------------------
if [ -d "$SOURCE/Odin/gods/odin" ]; then
    echo -e "${YELLOW}Copying Odin god server...${NC}"
    mkdir -p "$TARGET/gods/odin"
    cp -r "$SOURCE/Odin/gods/odin/" "$TARGET/gods/odin/"
    find "$TARGET/gods/odin" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    echo -e "  ${GREEN}Copied${NC}"
fi

# ---------------------------------------------------------------
# 4. Sync migrations
# ---------------------------------------------------------------
echo -e "${YELLOW}Syncing migrations...${NC}"
SRC_MIG="$SOURCE/orchestration/backend/migrations/versions"
TGT_MIG="$TARGET/orchestration/backend/migrations/versions"
SRC_COUNT=$(ls "$SRC_MIG/"*.py 2>/dev/null | wc -l)
TGT_COUNT=$(ls "$TGT_MIG/"*.py 2>/dev/null | wc -l)
echo -e "  Source: $SRC_COUNT  Target: $TGT_COUNT"
if [ "$SRC_COUNT" != "$TGT_COUNT" ]; then
    cp "$SRC_MIG/"*.py "$TGT_MIG/"
    echo -e "  ${GREEN}Synced${NC}"
else
    echo -e "  ${GREEN}In sync${NC}"
fi

# ---------------------------------------------------------------
# 5. Sync DB
# ---------------------------------------------------------------
echo -e "${YELLOW}Syncing database...${NC}"
SRC_DB="$SOURCE/orchestration/data/orchestration.db"
TGT_DB="$TARGET/orchestration/data/orchestration.db"
mkdir -p "$TARGET/orchestration/data"

if [ ! -f "$TGT_DB" ]; then
    cp "$SRC_DB" "$TGT_DB"
    cp "${SRC_DB}-wal" "$TARGET/orchestration/data/" 2>/dev/null || true
    cp "${SRC_DB}-shm" "$TARGET/orchestration/data/" 2>/dev/null || true
    echo -e "  ${GREEN}Copied (missing)${NC}"
elif [ "$SRC_DB" -nt "$TGT_DB" ]; then
    cp "$SRC_DB" "$TGT_DB"
    cp "${SRC_DB}-wal" "$TARGET/orchestration/data/" 2>/dev/null || true
    cp "${SRC_DB}-shm" "$TARGET/orchestration/data/" 2>/dev/null || true
    echo -e "  ${GREEN}Copied (newer)${NC}"
else
    echo -e "  ${GREEN}Current${NC}"
fi

# ---------------------------------------------------------------
# 6. Config
# ---------------------------------------------------------------
echo -e "${YELLOW}Config...${NC}"
if [ ! -f "$TARGET/orchestration/config.json" ]; then
    if [ -f "$SOURCE/orchestration/config.json" ]; then
        cp "$SOURCE/orchestration/config.json" "$TARGET/orchestration/"
        echo -e "  ${GREEN}Copied${NC}"
    else
        echo -e "  ${YELLOW}Missing — create from config.example.json${NC}"
    fi
else
    echo -e "  ${GREEN}Preserved${NC}"
fi

# ---------------------------------------------------------------
# 7. Scripts
# ---------------------------------------------------------------
echo -e "${YELLOW}Copying scripts...${NC}"
mkdir -p "$TARGET/scripts"
cp "$SOURCE/scripts/"* "$TARGET/scripts/" 2>/dev/null || true
echo -e "  ${GREEN}Copied${NC}"

# ---------------------------------------------------------------
# 8. Build frontend
# ---------------------------------------------------------------
echo -e "${YELLOW}Building orchestration frontend...${NC}"
cd "$TARGET/orchestration/frontend"
if [ ! -d "node_modules" ]; then
    npm install --silent 2>&1 | tail -1
fi
npm run build --silent 2>&1 | tail -1
echo -e "  ${GREEN}Built${NC}"

# ---------------------------------------------------------------
# 9. Python syntax check
# ---------------------------------------------------------------
echo -e "${YELLOW}Syntax check...${NC}"
SYNTAX_ERRORS=$(python -c "
import os
errors = []
for root, dirs, files in os.walk('$TARGET/orchestration/backend'):
    dirs[:] = [d for d in dirs if d != '__pycache__']
    for f in files:
        if f.endswith('.py'):
            path = os.path.join(root, f)
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    compile(fh.read(), path, 'exec')
            except SyntaxError as e:
                errors.append(f'{path}:{e.lineno} {e.msg}')
if errors:
    for e in errors:
        print(e)
" 2>/dev/null)

if [ -n "$SYNTAX_ERRORS" ]; then
    echo -e "  ${RED}ERRORS:${NC}"
    echo "$SYNTAX_ERRORS" | while read line; do echo "    $line"; done
    echo -e "  ${RED}Fix syntax errors before starting services${NC}"
    exit 1
else
    echo -e "  ${GREEN}All clean${NC}"
fi

# ---------------------------------------------------------------
# 10. Start services
# ---------------------------------------------------------------
echo -e "${YELLOW}Starting services...${NC}"
for svc in HekateContextStore HekateOrchestration HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker HekateAdmin; do
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
# 11. Verify
# ---------------------------------------------------------------
echo ""
echo -e "${YELLOW}Verification:${NC}"
curl -s http://localhost:5200/api/health > /dev/null 2>&1 \
    && echo -e "  Orchestration (5200): ${GREEN}UP${NC}" \
    || echo -e "  Orchestration (5200): ${RED}DOWN${NC}"
curl -s http://localhost:5102/api/health > /dev/null 2>&1 \
    && echo -e "  Context Store (5102): ${GREEN}UP${NC}" \
    || echo -e "  Context Store (5102): ${RED}DOWN${NC}"

echo ""
echo -e "${GREEN}=== Deploy complete ===${NC}"
