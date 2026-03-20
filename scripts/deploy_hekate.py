"""Hekate Engine Deploy Script.

Usage: python scripts/deploy_hekate.py

Run from admin terminal for NSSM restart.
"""

import os
import shutil
import subprocess
import sys
import time
import json
import urllib.request
import urllib.error

PYTHON = sys.executable
SOURCE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = r"C:\Hekate"
SERVICE = "HekateEngine"
PORT = 5200
HEALTH_URL = f"http://localhost:{PORT}/api/health"
API_URL = f"http://localhost:{PORT}/api"
PG_DSN = "postgresql://postgres:postgres@localhost:5433/orchestration"

step = 0


def log(msg):
    global step
    step += 1
    print(f"\n[{step}/9] {msg}")


def fail(msg):
    print(f"\n  FAILED: {msg}")
    print("  Old version still running.")
    sys.exit(1)


def run(cmd, cwd=None, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=120, shell=True)
    if check and r.returncode != 0:
        print(f"  stdout: {r.stdout[-200:]}" if r.stdout else "")
        print(f"  stderr: {r.stderr[-200:]}" if r.stderr else "")
        fail(f"Command failed: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    return r


def http_get(url, timeout=5):
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r.status, r.read().decode()
    except Exception:
        return 0, ""


def http_post(url, data=None, timeout=10):
    try:
        if data is not None:
            body = json.dumps(data).encode()
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url, data=b"", method="POST")
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read().decode())
    except Exception as e:
        return 0, {"error": str(e)}


# =========================================================================
# Step 1: Syntax check
# =========================================================================
log("Syntax checking Python files...")

gods_dir = os.path.join(SOURCE, "Odin", "gods")
errors = 0
for root, dirs, files in os.walk(gods_dir):
    for f in files:
        if f.endswith(".py"):
            path = os.path.join(root, f)
            r = run([PYTHON, "-c", f"import py_compile; py_compile.compile(r'{path}', doraise=True)"], check=False)
            if r.returncode != 0:
                print(f"  SYNTAX ERROR: {path}")
                errors += 1

if errors:
    fail(f"{errors} file(s) have syntax errors")
print("  All files pass syntax check")

# =========================================================================
# Step 2: Run tests
# =========================================================================
log("Running tests...")

test_files = [
    "test_providers.py", "test_plan_levels.py", "test_hekate_engine.py",
    "test_api.py", "test_response_validator.py",
]
r = run([PYTHON, "-m", "pytest"] + test_files + ["-q", "--tb=line"],
        cwd=os.path.join(SOURCE, "Odin"))
last_line = r.stdout.strip().split("\n")[-1] if r.stdout else "?"
print(f"  {last_line}")

# =========================================================================
# Step 3: Sync source to deploy
# =========================================================================
log("Syncing source to deploy directory...")

deploy_odin = os.path.join(DEPLOY, "Odin")
deploy_gods = os.path.join(deploy_odin, "gods")

if os.path.exists(deploy_gods):
    shutil.rmtree(deploy_gods)
shutil.copytree(os.path.join(SOURCE, "Odin", "gods"), deploy_gods)

for f in ["run_hekate.py", "run_pipeline.py", "conftest.py", "seed_test_projects.py"]:
    src = os.path.join(SOURCE, "Odin", f)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(deploy_odin, f))

print("  Source synced")

# =========================================================================
# Step 4: Clear pycache
# =========================================================================
log("Clearing pycache...")

for root_dir in [deploy_odin, os.path.join(SOURCE, "Odin")]:
    for root, dirs, files in os.walk(root_dir):
        for d in dirs:
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)

print("  Pycache cleared")

# =========================================================================
# Step 5: Build frontend
# =========================================================================
log("Building frontend...")

frontend_dir = os.path.join(SOURCE, "orchestration", "frontend")
r = run(["npm", "run", "build"], cwd=frontend_dir, check=False)
if r.returncode == 0:
    dist_src = os.path.join(frontend_dir, "dist")
    dist_dst = os.path.join(deploy_odin, "frontend-dist")
    if os.path.exists(dist_dst):
        shutil.rmtree(dist_dst)
    shutil.copytree(dist_src, dist_dst)
    print("  Frontend built and deployed")
else:
    print("  WARNING: Frontend build failed — using existing dist")

# =========================================================================
# Step 6: Restart NSSM service
# =========================================================================
log("Restarting service via Hades...")

status, resp = http_post("http://localhost:5201/services/HekateEngine/restart", data=None)
if status == 200:
    print("  Hades restarted HekateEngine")
else:
    print(f"  Hades restart failed (status={status}): {resp}")
    print(f"  Trying nssm directly...")
    r = subprocess.run(["nssm", "restart", SERVICE], capture_output=True, text=True)
    if r.returncode == 0:
        print("  nssm restarted service")
    else:
        fail(f"Could not restart service. Run from admin: nssm restart {SERVICE}")

# =========================================================================
# Step 7: Health check
# =========================================================================
log("Waiting for health check...")

healthy = False
for i in range(20):
    time.sleep(2)
    status, body = http_get(HEALTH_URL)
    if status == 200:
        healthy = True
        break
    print(f"  Attempt {i+1}/20 — HTTP {status}")

if not healthy:
    log_path = os.path.join(deploy_odin, "engine.log")
    if os.path.exists(log_path):
        with open(log_path) as f:
            lines = f.readlines()
            print("  Last 10 log lines:")
            for line in lines[-10:]:
                print(f"    {line.rstrip()}")
    fail("Health check failed after 40 seconds")

print("  Health check passed")

# =========================================================================
# Step 8: Smoke test
# =========================================================================
log("Smoke test...")

status, resp = http_post(f"{API_URL}/projects", {
    "name": "__deploy_smoke_test__",
    "requirements": "Return 1+1",
})

smoke_id = resp.get("id", "")
if not smoke_id:
    print(f"  Response: {resp}")
    fail("Could not create smoke test project")

print(f"  Created project: {smoke_id}")

# Trigger execution
http_post(f"{API_URL}/projects/{smoke_id}/execute")

# Wait and check
time.sleep(10)
status, proj = http_get(f"{API_URL}/projects/{smoke_id}")
if status == 200:
    proj_data = json.loads(proj)
    smoke_status = proj_data.get("status", "unknown")
    print(f"  Status: {smoke_status}")
    if smoke_status == "failed":
        status2, events = http_get(f"{API_URL}/events/{smoke_id}?limit=5")
        if status2 == 200:
            for e in json.loads(events):
                if e.get("event_type") == "planning_failed":
                    payload = json.loads(e["payload"]) if isinstance(e["payload"], str) else e["payload"]
                    fail(f"Planning failed: {payload.get('error', 'unknown')}")

# Cleanup via API
try:
    urllib.request.urlopen(
        urllib.request.Request(f"{API_URL}/projects/{smoke_id}", method="DELETE"),
        timeout=5,
    )
except Exception:
    pass
print("  Smoke test cleaned up")

# =========================================================================
# Step 9: Done
# =========================================================================
log("Deploy complete!")

print()
print("=" * 50)
print("  Hekate Engine deployed successfully")
print("=" * 50)
print()
print(f"  Service:   {SERVICE}")
print(f"  Port:      {PORT}")
print(f"  Dashboard: http://localhost:{PORT}/")

r = run(["git", "log", "--oneline", "-1"], cwd=SOURCE, check=False)
if r.stdout:
    print(f"  Commit:    {r.stdout.strip()}")
print()
