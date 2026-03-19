"""Language detection and tooling availability.

Detects project language from file extensions, checks which analysis
services are running, and provides tooling flags for suggest_target_level().

Services:
  - C#: Roslyn via HekateServer (port 5110)
  - Python: Jedi via HekatePythonWorker (port 9200)
  - TypeScript: TS Compiler via HekateTypeScriptWorker (port 9202)
  - C++: HekateCppWorker (port 9201) — partial, L3 max

See gods/TOOLING.md for full matrix.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("gods.tooling")

# Extension → language mapping
_EXT_MAP: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".cs": "csharp",
    ".csx": "csharp",
    ".csproj": "csharp",
    ".sln": "csharp",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".c": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
}

# Language → which tooling service matters
_LANG_TOOLING: dict[str, str] = {
    "python": "has_jedi",
    "csharp": "has_roslyn",
    "typescript": "has_ts_compiler",
    "javascript": "has_ts_compiler",  # TS compiler also handles JS
}

# Service health check URLs
_SERVICE_URLS: dict[str, str] = {
    "has_roslyn": "http://localhost:5110/health",
    "has_jedi": "http://localhost:9200/health",
    "has_ts_compiler": "http://localhost:9202/health",
}


# ---------------------------------------------------------------------------
# ToolingInfo
# ---------------------------------------------------------------------------

@dataclass
class ToolingInfo:
    has_roslyn: bool = False
    has_jedi: bool = False
    has_ts_compiler: bool = False


# ---------------------------------------------------------------------------
# detect_languages — from file paths
# ---------------------------------------------------------------------------

def detect_languages(files: list[str]) -> dict[str, int]:
    """Detect languages present in a project from file paths.

    Returns: {language: file_count} sorted by count descending.
    """
    counts: dict[str, int] = {}
    for f in files:
        _, ext = os.path.splitext(f)
        ext = ext.lower()
        lang = _EXT_MAP.get(ext)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))


# ---------------------------------------------------------------------------
# check_tooling_availability — ping services
# ---------------------------------------------------------------------------

async def _check_service(url: str) -> bool:
    """Check if a service is responding."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            return resp.status_code < 500
    except Exception:
        return False


async def check_tooling_availability() -> ToolingInfo:
    """Check which analysis services are running."""
    results = {}
    for key, url in _SERVICE_URLS.items():
        results[key] = await _check_service(url)
    return ToolingInfo(**results)


# ---------------------------------------------------------------------------
# get_tooling_flags — combines detection + availability
# ---------------------------------------------------------------------------

async def get_tooling_flags(files: list[str]) -> dict[str, bool]:
    """Get tooling flags relevant to a project's languages.

    Only returns True for tooling that is both:
    1. Relevant to the project's language(s)
    2. Actually available (service is running)

    Returns dict compatible with suggest_target_level() kwargs:
      {"has_roslyn": bool, "has_jedi": bool, "has_ts_compiler": bool}
    """
    langs = detect_languages(files)
    availability = await check_tooling_availability()

    flags = {
        "has_roslyn": False,
        "has_jedi": False,
        "has_ts_compiler": False,
    }

    for lang in langs:
        tooling_key = _LANG_TOOLING.get(lang)
        if tooling_key and getattr(availability, tooling_key, False):
            flags[tooling_key] = True

    return flags
