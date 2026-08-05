"""Feature flags — simple JSON-based toggle for gods pipeline.

Flags are loaded from flags.json (next to this file). The file is
re-read on every check so you can toggle handlers without restarting
the engine. Missing file or missing key = flag is ON (safe default).

Usage in registration.py:
    from gods.flags import is_enabled

    if is_enabled("athena_l0"):
        pipeline.register("project_created", athena_l0, ...)

Usage at runtime (hot-reload):
    from gods.flags import is_enabled, get_all, set_flag

    set_flag("hermes_async", False)   # writes to flags.json
    get_all()                          # {"athena_l0": True, ...}
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("gods.flags")

_FLAGS_PATH = Path(__file__).parent / "flags.json"
_cache: dict[str, Any] = {}
_cache_mtime: float = 0.0


def _load() -> dict[str, Any]:
    """Load flags from disk, with mtime-based caching."""
    global _cache, _cache_mtime

    if not _FLAGS_PATH.exists():
        return {}

    try:
        mtime = os.path.getmtime(_FLAGS_PATH)
        if mtime == _cache_mtime and _cache:
            return _cache

        with open(_FLAGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        _cache = data
        _cache_mtime = mtime
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("flags: failed to load %s: %s", _FLAGS_PATH, e)
        return _cache or {}


def is_enabled(flag_name: str, default: bool = True) -> bool:
    """Check if a flag is enabled. Missing flag = default (True)."""
    flags = _load()
    val = flags.get(flag_name, default)
    return bool(val)


def get_all() -> dict[str, Any]:
    """Return all flags (for API/introspection)."""
    return dict(_load())


def set_flag(flag_name: str, value: bool) -> None:
    """Set a flag and persist to disk."""
    flags = _load()
    flags[flag_name] = value

    try:
        with open(_FLAGS_PATH, "w", encoding="utf-8") as f:
            json.dump(flags, f, indent=2, sort_keys=True)
        logger.info("flags: set %s = %s", flag_name, value)
    except OSError as e:
        logger.error("flags: failed to write %s: %s", _FLAGS_PATH, e)

    global _cache, _cache_mtime
    _cache = flags
    _cache_mtime = time.time()


def flag_gate(flag_name: str):
    """Return a filter function that checks a flag at dispatch time.

    Use as a runtime gate (not registration-time) so flags can be
    toggled without restarting:

        pipeline.register("project_created", athena_l0,
                          filter=flag_gate("athena_l0"))
    """
    def _check(event) -> bool:
        return is_enabled(flag_name)
    return _check
