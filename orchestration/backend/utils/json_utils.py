#  Orchestration Engine - JSON Utilities
#
#  Defensive JSON parsing for LLM output: trailing comma cleanup,
#  balanced-brace extraction, and requirement text splitting.
#
#  Depends on: (none)
#  Used by:    services/planner.py, services/verifier.py,
#              services/knowledge_extractor.py

import json
import re


def strip_trailing_commas(json_str: str) -> str:
    """Remove trailing commas before ] and } that LLMs commonly produce.

    Operates outside of string literals to avoid corrupting values.
    E.g. [{"a":1},] -> [{"a":1}] and {"a":1,} -> {"a":1}
    """
    return re.sub(r',\s*([}\]])', r'\1', json_str)


def extract_json_object(text: str) -> dict | None:
    """Extract the first balanced JSON object from text.

    Uses brace-counting instead of a greedy regex to avoid capturing
    past the actual closing brace when Claude wraps JSON in explanation.
    Falls back to stripping trailing commas if strict parsing fails.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = text[start:i + 1]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    # Retry after stripping trailing commas
                    try:
                        return json.loads(strip_trailing_commas(raw))
                    except json.JSONDecodeError:
                        return None
    return None


def parse_requirements(text: str) -> list[str]:
    """Split requirements into blocks separated by blank lines.

    Single-line requirements (the common case) are unaffected.
    Multi-line requirements separated by blank lines produce one
    block per paragraph, preserving internal newlines.
    """
    paragraphs = re.split(r'\n\s*\n', text.strip())
    return [p.strip() for p in paragraphs if p.strip()]
