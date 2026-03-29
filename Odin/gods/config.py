"""Hekate service URLs — resolved from environment variables.

Every service URL is configurable per-machine via env vars.
Defaults are localhost for single-machine dev/production on Sisyphus.
Set env vars in NSSM AppEnvironmentExtra for each machine.

Usage:
    from gods.config import GATEWAY_URL, CONTEXT_STORE_URL, MCP_URL, ENGINE_URL
"""

import os

ENGINE_URL = os.environ.get("HEKATE_ENGINE_URL", "http://localhost:5200")
GATEWAY_URL = os.environ.get("HEKATE_GATEWAY_URL", "http://localhost:5210")
CONTEXT_STORE_URL = os.environ.get("HEKATE_CONTEXT_STORE_URL", "http://localhost:5102")
MCP_URL = os.environ.get("HEKATE_MCP_URL", "http://localhost:5110")
