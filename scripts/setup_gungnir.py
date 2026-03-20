"""Setup Gungnir for Hekate — run this ON Gungnir.

Sets up:
  1. Prometheus MCP (project management via Sisyphus API)
  2. Apollo MCP config placeholder (code analysis, local)
  3. Claude Code .mcp.json with both servers

Usage: python scripts/setup_gungnir.py
"""

import json
import os
import shutil
import sys

SISYPHUS_IP = "192.168.1.164"
HEKATE_ENGINE_URL = f"http://{SISYPHUS_IP}:5200"

# Find the Hekate repo on this machine
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

# Claude settings dir
CLAUDE_DIR = os.path.expanduser("~/.claude")
CLAUDE_PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")


def main():
    print("=== Gungnir Setup for Hekate ===")
    print(f"Repo: {REPO_ROOT}")
    print(f"Engine: {HEKATE_ENGINE_URL}")
    print()

    # 1. Verify Prometheus MCP exists
    prometheus_path = os.path.join(REPO_ROOT, "Odin", "gods", "prometheus_mcp.py")
    if not os.path.exists(prometheus_path):
        print(f"ERROR: {prometheus_path} not found")
        print("Run 'git pull' first to get the latest code")
        sys.exit(1)
    print(f"[1] Prometheus MCP: {prometheus_path}")

    # 2. Test connectivity to Sisyphus
    print(f"[2] Testing connection to {HEKATE_ENGINE_URL}...")
    try:
        import urllib.request
        r = urllib.request.urlopen(f"{HEKATE_ENGINE_URL}/api/health", timeout=5)
        data = json.loads(r.read())
        if data.get("status") == "ok":
            print(f"    Connected — engine healthy")
        else:
            print(f"    WARNING: unexpected response: {data}")
    except Exception as e:
        print(f"    WARNING: cannot reach Sisyphus ({e})")
        print(f"    Prometheus will fail until Sisyphus is reachable")

    # 3. Update root .mcp.json
    mcp_path = os.path.join(REPO_ROOT, ".mcp.json")
    if os.path.exists(mcp_path):
        with open(mcp_path) as f:
            mcp = json.load(f)
    else:
        mcp = {"mcpServers": {}}

    # Add/update prometheus
    mcp["mcpServers"]["prometheus"] = {
        "type": "stdio",
        "command": "python",
        "args": [os.path.join("Odin", "gods", "prometheus_mcp.py")],
        "env": {
            "HEKATE_ENGINE_URL": HEKATE_ENGINE_URL,
        },
    }

    with open(mcp_path, "w") as f:
        json.dump(mcp, f, indent=2)
    print(f"[3] Updated {mcp_path}")
    print(f"    Added: prometheus → {HEKATE_ENGINE_URL}")

    # 4. Show what's configured
    print()
    print("=== MCP Servers configured ===")
    for name, cfg in mcp["mcpServers"].items():
        print(f"  {name:20s} → {cfg.get('args', ['?'])[0][:50]}")

    print()
    print("=== Next Steps ===")
    print("1. Reload Claude Code (Ctrl+Shift+P → 'Reload Window')")
    print("2. Verify: prometheus tools should appear in tool list")
    print("3. Test: prometheus.list_projects()")
    print()
    print("For Apollo (code analysis), the existing hekate-mcp server")
    print("continues to work. Rename to apollo is a future step.")
    print()
    print("Done!")


if __name__ == "__main__":
    main()
