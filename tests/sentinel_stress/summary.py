"""Summary task: reads and reports outputs of task_a through task_d."""
import subprocess
import sys
import os

TASK_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS = ["task_a.py", "task_b.py", "task_c.py", "task_d.py"]

results = {}

for task in TASKS:
    task_path = os.path.join(TASK_DIR, task)
    print(f"--- {task} ---")
    if not os.path.exists(task_path):
        print(f"  File not found: {task_path}")
        results[task] = "MISSING"
        continue
    try:
        proc = subprocess.run(
            [sys.executable, task_path],
            capture_output=True, text=True, timeout=30
        )
        results[task] = "PASS" if proc.returncode == 0 else "FAIL"
        print(f"  exit code: {proc.returncode}")
        if proc.stdout.strip():
            print(f"  stdout: {proc.stdout.strip()}")
        if proc.stderr.strip():
            print(f"  stderr: {proc.stderr.strip()}")
    except subprocess.TimeoutExpired:
        results[task] = "TIMEOUT"
        print(f"  TIMEOUT after 30s")
    print()

print("=== Summary ===")
for task, status in results.items():
    print(f"  {task}: {status}")

failed = sum(1 for s in results.values() if s != "PASS")
print(f"\n{failed}/{len(TASKS)} tasks failed (expected: all 4)")
