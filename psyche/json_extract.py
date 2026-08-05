"""Robust JSON object extractor for LLM responses.

Models occasionally wrap JSON in markdown fences, prepend chatter, or trail
explanatory text. This module finds the first balanced top-level object and
returns it parsed.
"""

from __future__ import annotations

import json
import re
from typing import Any


_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"\s*```\s*$")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_OPEN.sub("", text)
        text = _FENCE_CLOSE.sub("", text)
    return text


def _find_balanced_object(text: str) -> str | None:
    """Return the first top-level balanced {...} substring, or None.

    Honors string literals and escapes so braces inside strings don't count.
    """
    depth = 0
    start = -1
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue

        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]

    return None


def extract_json(text: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from model output.

    Returns {} if no valid object can be parsed.
    """
    if not text:
        return {}

    cleaned = _strip_fences(text)

    # Fast path: the whole thing parses
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Slow path: find a balanced object substring
    candidate = _find_balanced_object(cleaned)
    if candidate:
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return {}
