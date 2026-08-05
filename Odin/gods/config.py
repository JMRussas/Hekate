"""Hekate service URLs and rate limits — resolved from environment variables.

Every service URL is configurable per-machine via env vars.
Defaults are localhost for single-machine dev/production on Sisyphus.
Set env vars in NSSM AppEnvironmentExtra for each machine.

Usage:
    from gods.config import GATEWAY_URL, CONTEXT_STORE_URL, MCP_URL, ENGINE_URL
    from gods.config import RATE_LIMITS, parse_rate_limit_config
"""

import os

ENGINE_URL = os.environ.get("HEKATE_ENGINE_URL", "http://localhost:5200")
GATEWAY_URL = os.environ.get("HEKATE_GATEWAY_URL", "http://localhost:5210")
CONTEXT_STORE_URL = os.environ.get("HEKATE_CONTEXT_STORE_URL", "http://localhost:5102")
MCP_URL = os.environ.get("HEKATE_MCP_URL", "http://localhost:5110")

# --- Rate limits ---

_RATE_LIMIT_DEFAULTS = {
    "claude_code": {"rate": 10, "window_seconds": 60, "burst": 12},
    "ollama": {"rate": 20, "window_seconds": 60, "burst": 25},
}


def parse_rate_limit_config(env_value: str, defaults: dict) -> dict:
    """Parse a rate limit env var in 'rate/window' format (e.g. '10/60').

    Burst defaults to rate * 1.2 (rounded up) if not specified as 'rate/window/burst'.
    Returns a dict with keys: rate, window_seconds, burst.
    Falls back to *defaults* on any parse error.
    """
    if not env_value:
        return dict(defaults)
    parts = env_value.strip().split("/")
    try:
        rate = int(parts[0])
        window = int(parts[1]) if len(parts) > 1 else defaults["window_seconds"]
        burst = int(parts[2]) if len(parts) > 2 else -(-rate * 12 // 10)  # ceil(rate*1.2)
        return {"rate": rate, "window_seconds": window, "burst": burst}
    except (ValueError, IndexError):
        return dict(defaults)


RATE_LIMITS = {
    provider: parse_rate_limit_config(
        os.environ.get(f"HEKATE_RATE_LIMIT_{provider.upper()}"),
        defaults,
    )
    for provider, defaults in _RATE_LIMIT_DEFAULTS.items()
}
