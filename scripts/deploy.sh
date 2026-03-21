#!/bin/bash
# Hekate — deploy from source repo to C:\Hekate (NSSM service directory)
# Usage: bash scripts/deploy.sh
#
# Can be run from:
#   - Git Bash: bash scripts/deploy.sh
#   - PowerShell (admin): bash scripts/deploy.sh
#   - Any terminal in the source repo directory
#
# Strategy: build into staging dir, syntax check, atomic swap, health check.
# If health checks fail, swap back to the previous deployment.
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

STAGING="${TARGET}.staging"
OLD="${TARGET}.old"
FAILED="${TARGET}.failed"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}=== Hekate Deploy (atomic swap) ===${NC}"
echo "  Source:  $SOURCE"
echo "  Target:  $TARGET"
echo "  Staging: $STAGING"
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

# Clean up leftover staging/old dirs from previous failed deploys
if [ -d "$STAGING" ]; then
    echo -e "  ${YELLOW}Removing leftover staging dir${NC}"
    rm -rf "$STAGING"
fi
if [ -d "$FAILED" ]; then
    echo -e "  ${YELLOW}Removing leftover failed dir${NC}"
    rm -rf "$FAILED"
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
# 2. Build staging directory
# ---------------------------------------------------------------
echo -e "${YELLOW}Creating staging directory...${NC}"
mkdir -p "$STAGING"
echo -e "  ${GREEN}Created $STAGING${NC}"

# ---------------------------------------------------------------
# 3. Publish Context Store (dotnet publish → binary)
# ---------------------------------------------------------------
echo -e "${YELLOW}Publishing context store...${NC}"
cd "$SOURCE/context-store/Api"
mkdir -p "$STAGING/context-store"
dotnet publish -c Release -o "$STAGING/context-store/" -q 2>&1
# Copy MCP config + tools + CLAUDE.md for CLI discovery
cp -f "$SOURCE/context-store/.mcp.json" "$STAGING/context-store/.mcp.json" 2>/dev/null || true
cp -f "$SOURCE/context-store/CLAUDE.md" "$STAGING/context-store/CLAUDE.md" 2>/dev/null || true
cp -r "$SOURCE/context-store/tools" "$STAGING/context-store/tools"
find "$STAGING/context-store/tools" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
echo -e "  ${GREEN}Published${NC}"

# ---------------------------------------------------------------
# 4. Copy Orchestration source
# ---------------------------------------------------------------
echo -e "${YELLOW}Copying orchestration source...${NC}"
mkdir -p "$STAGING/orchestration"

# Backend
cp -r "$SOURCE/orchestration/backend" "$STAGING/orchestration/backend"
find "$STAGING/orchestration/backend" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# Tools
cp -r "$SOURCE/orchestration/tools" "$STAGING/orchestration/tools"

# Tests
cp -r "$SOURCE/orchestration/tests" "$STAGING/orchestration/tests"

# Root files
cp "$SOURCE/orchestration/run.py" "$STAGING/orchestration/" 2>/dev/null || true
cp "$SOURCE/orchestration/requirements.txt" "$STAGING/orchestration/" 2>/dev/null || true
cp "$SOURCE/orchestration/Dockerfile" "$STAGING/orchestration/" 2>/dev/null || true
cp "$SOURCE/orchestration/CLAUDE.md" "$STAGING/orchestration/" 2>/dev/null || true

# Frontend source (for build)
if [ -d "$SOURCE/orchestration/frontend/src" ]; then
    mkdir -p "$STAGING/orchestration/frontend"
    cp -r "$SOURCE/orchestration/frontend/src" "$STAGING/orchestration/frontend/src"
    cp "$SOURCE/orchestration/frontend/package.json" "$STAGING/orchestration/frontend/" 2>/dev/null || true
    cp "$SOURCE/orchestration/frontend/vite.config.ts" "$STAGING/orchestration/frontend/" 2>/dev/null || true
    cp "$SOURCE/orchestration/frontend/tsconfig"* "$STAGING/orchestration/frontend/" 2>/dev/null || true
    cp "$SOURCE/orchestration/frontend/index.html" "$STAGING/orchestration/frontend/" 2>/dev/null || true
fi

echo -e "  ${GREEN}Copied${NC}"

# ---------------------------------------------------------------
# 4b. Copy Hades
# ---------------------------------------------------------------
echo -e "${YELLOW}Copying hades...${NC}"
mkdir -p "$STAGING/hades"
cp "$SOURCE/hades/"*.py "$STAGING/hades/" 2>/dev/null || true
cp "$SOURCE/hades/requirements.txt" "$STAGING/hades/" 2>/dev/null || true
echo -e "  ${GREEN}Copied${NC}"

# ---------------------------------------------------------------
# 5. Copy LLM Gateway
# ---------------------------------------------------------------
echo -e "${YELLOW}Copying llm-gateway...${NC}"
if [ -d "$TARGET/llm-gateway" ]; then
    cp -r "$TARGET/llm-gateway" "$STAGING/llm-gateway"
fi
# Overwrite with source files
mkdir -p "$STAGING/llm-gateway"
cp "$SOURCE/llm-gateway/"*.py "$STAGING/llm-gateway/" 2>/dev/null || true
cp "$SOURCE/llm-gateway/requirements.txt" "$STAGING/llm-gateway/" 2>/dev/null || true
echo -e "  ${GREEN}Copied${NC}"

# ---------------------------------------------------------------
# 6. Copy Odin / Gods
# ---------------------------------------------------------------
echo -e "${YELLOW}Copying Odin...${NC}"
if [ -d "$SOURCE/Odin" ]; then
    mkdir -p "$STAGING/Odin"
    cp -r "$SOURCE/Odin/" "$STAGING/Odin/" 2>/dev/null || true
    find "$STAGING/Odin" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
fi
echo -e "  ${GREEN}Copied${NC}"

# ---------------------------------------------------------------
# 7. Carry over persistent/runtime data from current target
# ---------------------------------------------------------------
echo -e "${YELLOW}Carrying over persistent data...${NC}"

# Database
if [ -d "$TARGET/orchestration/data" ]; then
    cp -r "$TARGET/orchestration/data" "$STAGING/orchestration/data"
    echo -e "  ${GREEN}orchestration/data${NC}"
fi

# Config (preserve existing, copy from source only if target has none)
if [ -f "$TARGET/orchestration/config.json" ]; then
    cp "$TARGET/orchestration/config.json" "$STAGING/orchestration/config.json"
    echo -e "  ${GREEN}orchestration/config.json (preserved)${NC}"
elif [ -f "$SOURCE/orchestration/config.json" ]; then
    cp "$SOURCE/orchestration/config.json" "$STAGING/orchestration/config.json"
    echo -e "  ${GREEN}orchestration/config.json (from source)${NC}"
else
    echo -e "  ${YELLOW}orchestration/config.json missing — create from config.example.json${NC}"
fi

# Hades services.json (preserve existing, seed from source only if target has none)
if [ -f "$TARGET/hades/services.json" ]; then
    cp "$TARGET/hades/services.json" "$STAGING/hades/services.json"
    echo -e "  ${GREEN}hades/services.json (preserved)${NC}"
elif [ -f "$SOURCE/hades/services.json" ]; then
    cp "$SOURCE/hades/services.json" "$STAGING/hades/services.json"
    echo -e "  ${GREEN}hades/services.json (seeded)${NC}"
fi

# Frontend node_modules (saves npm install time)
if [ -d "$TARGET/orchestration/frontend/node_modules" ]; then
    cp -r "$TARGET/orchestration/frontend/node_modules" "$STAGING/orchestration/frontend/node_modules"
    echo -e "  ${GREEN}frontend/node_modules${NC}"
fi

# Frontend dist (will be rebuilt, but copy as fallback)
if [ -d "$TARGET/orchestration/frontend/dist" ]; then
    cp -r "$TARGET/orchestration/frontend/dist" "$STAGING/orchestration/frontend/dist"
fi

# Scripts
mkdir -p "$STAGING/scripts"
cp "$SOURCE/scripts/"* "$STAGING/scripts/" 2>/dev/null || true
echo -e "  ${GREEN}scripts${NC}"

# Any other top-level dirs in target that we don't build from source
# (e.g., .worktrees, extension builds, etc.)
for dir in "$TARGET"/*; do
    dirname=$(basename "$dir")
    # Skip dirs we already handled
    case "$dirname" in
        context-store|orchestration|hades|llm-gateway|Odin|scripts) continue ;;
    esac
    if [ -d "$dir" ] && [ ! -d "$STAGING/$dirname" ]; then
        cp -r "$dir" "$STAGING/$dirname"
        echo -e "  ${GREEN}$dirname (carried over)${NC}"
    elif [ -f "$dir" ] && [ ! -f "$STAGING/$dirname" ]; then
        cp "$dir" "$STAGING/$dirname"
    fi
done

echo -e "  ${GREEN}Done${NC}"

# ---------------------------------------------------------------
# 8. Sync migrations
# ---------------------------------------------------------------
echo -e "${YELLOW}Syncing migrations...${NC}"
SRC_MIG="$SOURCE/orchestration/backend/migrations/versions"
STG_MIG="$STAGING/orchestration/backend/migrations/versions"
SRC_COUNT=$(ls "$SRC_MIG/"*.py 2>/dev/null | wc -l)
STG_COUNT=$(ls "$STG_MIG/"*.py 2>/dev/null | wc -l)
echo -e "  Source: $SRC_COUNT  Staging: $STG_COUNT"
if [ "$SRC_COUNT" != "$STG_COUNT" ]; then
    cp "$SRC_MIG/"*.py "$STG_MIG/"
    echo -e "  ${GREEN}Synced${NC}"
else
    echo -e "  ${GREEN}In sync${NC}"
fi

# ---------------------------------------------------------------
# 9. Set Postgres environment for NSSM services
# ---------------------------------------------------------------
echo -e "${YELLOW}Configuring Postgres environment...${NC}"
python "$STAGING/scripts/set_pg_env.py"
echo -e "  ${GREEN}Done${NC}"

# ---------------------------------------------------------------
# 10. Build frontend
# ---------------------------------------------------------------
echo -e "${YELLOW}Building orchestration frontend...${NC}"
cd "$STAGING/orchestration/frontend"
if [ ! -d "node_modules" ]; then
    npm install --silent 2>&1 | tail -1
fi
npm run build --silent 2>&1 | tail -1
echo -e "  ${GREEN}Built${NC}"

# ---------------------------------------------------------------
# 11. Python syntax check (on staging, before swap)
# ---------------------------------------------------------------
echo -e "${YELLOW}Syntax check...${NC}"
SYNTAX_ERRORS=$(python -c "
import os
errors = []
for root, dirs, files in os.walk('$STAGING/orchestration/backend'):
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
    echo -e "  ${RED}Syntax errors detected — aborting deploy, cleaning staging${NC}"
    rm -rf "$STAGING"
    # Restart services from existing target (unchanged)
    echo -e "${YELLOW}Restarting services from existing deployment...${NC}"
    for svc in HekateContextStore HekateOrchestration HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker HekateAdmin; do
        nssm start "$svc" > /dev/null 2>&1 || true
    done
    exit 1
else
    echo -e "  ${GREEN}All clean${NC}"
fi

# ---------------------------------------------------------------
# 12. Atomic swap: staging → target
# ---------------------------------------------------------------
echo -e "${YELLOW}Atomic swap...${NC}"

# Remove any leftover .old from a previous deploy
if [ -d "$OLD" ]; then
    echo -e "  ${YELLOW}Removing previous .old backup${NC}"
    rm -rf "$OLD"
fi

# Swap: target → old, staging → target
mv "$TARGET" "$OLD"
mv "$STAGING" "$TARGET"
echo -e "  ${GREEN}Swapped: staging is now live${NC}"

# ---------------------------------------------------------------
# 13. Start services
# ---------------------------------------------------------------
echo -e "${YELLOW}Starting services...${NC}"
for svc in HekateContextStore HekateOrchestration HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker HekateAdmin; do
    nssm start "$svc" > /dev/null 2>&1 || true
done

# ---------------------------------------------------------------
# 14. Health checks
# ---------------------------------------------------------------
echo -n "  Health checks"
HEALTH_OK=false
for i in $(seq 1 20); do
    ORCH=$(curl -s http://localhost:5200/api/health 2>/dev/null)
    CS=$(curl -s http://localhost:5102/api/health 2>/dev/null)
    if echo "$ORCH" | grep -q "ok" && echo "$CS" | grep -q "ok"; then
        echo -e " ${GREEN}OK${NC}"
        HEALTH_OK=true
        break
    fi
    echo -n "."
    sleep 1
done

if [ "$HEALTH_OK" = false ]; then
    echo -e " ${RED}FAILED${NC}"
    echo -e "${RED}Health checks failed — rolling back...${NC}"

    # Stop broken services
    for svc in HekateOrchestration HekateContextStore HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker HekateAdmin; do
        nssm stop "$svc" > /dev/null 2>&1 || true
    done
    sleep 2

    # Swap back: current (broken) → failed, old → target
    mv "$TARGET" "$FAILED"
    mv "$OLD" "$TARGET"

    # Restart with the old (known-good) deployment
    for svc in HekateContextStore HekateOrchestration HekateServer HekatePythonWorker HekateTypeScriptWorker HekateCppWorker HekateAdmin; do
        nssm start "$svc" > /dev/null 2>&1 || true
    done

    echo -e "${YELLOW}Rolled back to previous deployment${NC}"
    echo -e "  Failed deployment saved at: $FAILED"
    echo -e "  ${RED}Investigate and re-deploy${NC}"
    exit 1
fi

# ---------------------------------------------------------------
# 15. Cleanup old deployment
# ---------------------------------------------------------------
echo -e "${YELLOW}Cleaning up previous deployment...${NC}"
if [ -d "$OLD" ]; then
    rm -rf "$OLD"
    echo -e "  ${GREEN}Removed .old backup${NC}"
fi

# ---------------------------------------------------------------
# 16. Verify
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
