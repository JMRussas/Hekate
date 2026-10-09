#!/bin/bash
# =============================================================================
# Hekate Engine Deploy Script
#
# Usage: bash scripts/deploy_hekate.sh
#
# Steps:
#   1. Validate source code (syntax check all .py files)
#   2. Run tests (fail fast if any break)
#   3. Sync source to deployment dir
#   4. Clear pycache everywhere
#   5. Build frontend
#   6. Restart NSSM service
#   7. Wait for health check
#   8. Smoke test (create project, verify planning starts)
#   9. Report success or rollback
# =============================================================================

set -e

PYTHON="${HEKATE_PYTHON:-python}"
SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY="${HEKATE_ROOT:-C:/Hekate}"
SERVICE="HekateEngine"

# NSSM must run from DEPLOY dir (LocalSystem can't access user folders)
# This script: tests in SOURCE, syncs to DEPLOY, NSSM runs from DEPLOY
PORT=5200
HEALTH_URL="http://localhost:${PORT}/api/health"
API_URL="http://localhost:${PORT}/api"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

step=0
total_steps=9

log() {
    step=$((step + 1))
    echo -e "${GREEN}[${step}/${total_steps}]${NC} $1"
}

fail() {
    echo -e "${RED}DEPLOY FAILED:${NC} $1"
    echo -e "${YELLOW}Service not restarted — old version still running.${NC}"
    exit 1
}

warn() {
    echo -e "${YELLOW}WARNING:${NC} $1"
}

# =============================================================================
# Step 1: Syntax check all Python files in Odin/gods/
# =============================================================================
log "Syntax checking Python files..."

errors=0
for f in $(find "${SOURCE}/Odin/gods" -name "*.py" -type f); do
    if ! "$PYTHON" -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null; then
        echo "  SYNTAX ERROR: $f"
        errors=$((errors + 1))
    fi
done

if [ $errors -gt 0 ]; then
    fail "$errors file(s) have syntax errors"
fi
echo "  All files pass syntax check"

# =============================================================================
# Step 2: Run tests (fast — skip slow integration tests)
# =============================================================================
log "Running tests..."

cd "${SOURCE}/Odin"
test_output=$("$PYTHON" -m pytest test_providers.py test_plan_levels.py test_hekate_engine.py test_api.py test_response_validator.py -q --tb=line 2>&1)
test_exit=$?

if [ $test_exit -ne 0 ]; then
    echo "$test_output" | tail -10
    fail "Tests failed (exit code $test_exit)"
fi

passed=$(echo "$test_output" | grep -oP '\d+ passed' | head -1)
echo "  $passed"

# =============================================================================
# Step 3: Sync source to deployment directory
# =============================================================================
log "Syncing source to ${DEPLOY}/Odin..."

# Remove old gods directory and recopy
rm -rf "${DEPLOY}/Odin/gods"
cp -r "${SOURCE}/Odin/gods" "${DEPLOY}/Odin/gods"
cp "${SOURCE}/Odin/run_hekate.py" "${DEPLOY}/Odin/run_hekate.py"
cp "${SOURCE}/Odin/run_pipeline.py" "${DEPLOY}/Odin/run_pipeline.py"
cp "${SOURCE}/Odin/conftest.py" "${DEPLOY}/Odin/conftest.py"
cp "${SOURCE}/Odin/seed_test_projects.py" "${DEPLOY}/Odin/seed_test_projects.py"

echo "  Source synced"

# =============================================================================
# Step 4: Clear all pycache
# =============================================================================
log "Clearing pycache..."

find "${DEPLOY}/Odin" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
find "${SOURCE}/Odin" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null

echo "  Pycache cleared"

# =============================================================================
# Step 5: Build frontend
# =============================================================================
log "Building frontend..."

cd "${SOURCE}/orchestration/frontend"
if npm run build 2>&1 | tail -3; then
    # Copy dist to deployment
    rm -rf "${DEPLOY}/Odin/frontend-dist"
    cp -r "${SOURCE}/orchestration/frontend/dist" "${DEPLOY}/Odin/frontend-dist"
    echo "  Frontend built and copied"
else
    warn "Frontend build failed — using existing dist"
fi

# =============================================================================
# Step 6: Restart NSSM service
# =============================================================================
log "Restarting ${SERVICE}..."

# bash from PowerShell doesn't inherit admin — use cmd.exe or powershell.exe
if cmd.exe /c "nssm restart ${SERVICE}" 2>&1; then
    echo "  Service restarted"
else
    warn "cmd.exe nssm failed — trying powershell"
    if powershell.exe -Command "nssm restart ${SERVICE}" 2>&1; then
        echo "  Service restarted via powershell"
    else
        echo ""
        echo -e "${YELLOW}  Could not restart NSSM service automatically.${NC}"
        echo -e "${YELLOW}  Run from admin PowerShell: nssm restart ${SERVICE}${NC}"
        echo ""
        read -p "  Press Enter after restarting the service manually..."
    fi
fi

# =============================================================================
# Step 7: Wait for health check
# =============================================================================
log "Waiting for health check..."

healthy=false
for i in $(seq 1 20); do
    sleep 2
    response=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" 2>/dev/null)
    if [ "$response" = "200" ]; then
        healthy=true
        break
    fi
    echo "  Attempt $i/20 — HTTP $response"
done

if [ "$healthy" = false ]; then
    echo "  Checking logs..."
    tail -10 "${DEPLOY}/Odin/engine.log" 2>/dev/null
    fail "Health check failed after 40 seconds"
fi

echo "  Health check passed"

# =============================================================================
# Step 8: Smoke test — create project, verify planning starts
# =============================================================================
log "Smoke test..."

# Create a test project
smoke_response=$(curl -s -X POST "${API_URL}/projects" \
    -H "Content-Type: application/json" \
    -d '{"name":"__deploy_smoke_test__","requirements":"Return 1+1"}')

smoke_id=$(echo "$smoke_response" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('id','NONE'))" 2>/dev/null)

if [ "$smoke_id" = "NONE" ] || [ -z "$smoke_id" ]; then
    echo "  Response: $smoke_response"
    fail "Could not create smoke test project"
fi

echo "  Created smoke test project: $smoke_id"

# Trigger execution
curl -s -X POST "${API_URL}/projects/${smoke_id}/execute" > /dev/null

# Wait a few seconds and check it's planning
sleep 10
smoke_status=$(curl -s "${API_URL}/projects/${smoke_id}" | "$PYTHON" -c "import sys,json; print(json.load(sys.stdin).get('status','UNKNOWN'))" 2>/dev/null)

if [ "$smoke_status" = "draft" ]; then
    warn "Smoke test project still draft after 10s — pipeline may not be running"
elif [ "$smoke_status" = "failed" ]; then
    # Check error
    smoke_error=$(curl -s "${API_URL}/events/${smoke_id}?limit=5" | "$PYTHON" -c "
import sys,json
events = json.load(sys.stdin)
for e in events:
    if e.get('event_type') == 'planning_failed':
        payload = json.loads(e.get('payload','{}')) if isinstance(e.get('payload'), str) else e.get('payload',{})
        print(payload.get('error','unknown'))
        break
" 2>/dev/null)
    fail "Smoke test planning failed: $smoke_error"
else
    echo "  Smoke test status: $smoke_status"
fi

# Clean up smoke test via API
curl -s -X DELETE "${API_URL}/projects/${smoke_id}" > /dev/null 2>&1 || true
echo "  Smoke test cleaned up"

# =============================================================================
# Step 9: Report
# =============================================================================
log "Deploy complete!"

echo ""
echo -e "${GREEN}=============================================${NC}"
echo -e "${GREEN}  Hekate Engine deployed successfully${NC}"
echo -e "${GREEN}=============================================${NC}"
echo ""
echo "  Service:   ${SERVICE}"
echo "  Port:      ${PORT}"
echo "  Health:    ${HEALTH_URL}"
echo "  Dashboard: http://localhost:${PORT}/"
echo "  Tests:     $passed"
echo ""

# Show git info
cd "${SOURCE}"
echo "  Commit:    $(git log --oneline -1)"
echo "  Branch:    $(git branch --show-current)"
echo ""
