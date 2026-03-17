"""Add ORCHESTRATION_DSN to HekateOrchestration NSSM env vars."""
import subprocess

result = subprocess.run(
    ["nssm", "get", "HekateOrchestration", "AppEnvironmentExtra"],
    capture_output=True, text=True,
)
current = result.stdout.strip()

if "ORCHESTRATION_DSN" in current:
    print("ORCHESTRATION_DSN already set")
else:
    lines = [l for l in current.split("\n") if l.strip()]
    lines.append("ORCHESTRATION_DSN=postgresql://postgres:postgres@localhost:5433/orchestration")
    proc = subprocess.run(
        ["nssm", "set", "HekateOrchestration", "AppEnvironmentExtra"] + lines,
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        print("ORCHESTRATION_DSN added successfully")
    else:
        print(f"Error: {proc.stderr}")

# Verify
result2 = subprocess.run(
    ["nssm", "get", "HekateOrchestration", "AppEnvironmentExtra"],
    capture_output=True, text=True,
)
print(result2.stdout)
