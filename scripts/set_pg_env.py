"""Add ORCHESTRATION_DSN to NSSM env vars for orchestration services."""
import subprocess
import sys

DSN = "ORCHESTRATION_DSN=postgresql://postgres:postgres@localhost:5433/orchestration"

SERVICES = ["HekateOrchestration", "HekateEngine"]


def set_pg_env(service: str) -> None:
    result = subprocess.run(
        ["nssm", "get", service, "AppEnvironmentExtra"],
        capture_output=True, text=True,
    )
    current = result.stdout.strip()

    if "ORCHESTRATION_DSN" in current:
        print(f"  {service}: ORCHESTRATION_DSN already set")
        return

    lines = [l for l in current.split("\n") if l.strip()]
    lines.append(DSN)
    proc = subprocess.run(
        ["nssm", "set", service, "AppEnvironmentExtra"] + lines,
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        print(f"  {service}: ORCHESTRATION_DSN added")
    else:
        print(f"  {service}: Error — {proc.stderr}", file=sys.stderr)


for svc in SERVICES:
    set_pg_env(svc)
