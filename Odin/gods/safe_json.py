"""Safe JSON operations. Never throws.

Use these instead of json.loads/json.dumps throughout the gods pipeline.
Every call returns a default on failure instead of crashing.
"""

import json
import logging

logger = logging.getLogger("gods.safe_json")


def loads(text, default=None):
    """Parse JSON. Returns default on any error. Never throws."""
    if text is None:
        return default
    if not isinstance(text, str):
        return text  # Already parsed
    text = text.strip()
    if not text:
        return default
    try:
        result = json.loads(text)
        return result
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.debug("safe_json.loads failed: %s (input: %s)", e, text[:100])
        return default


def loads_dict(text, default=None):
    """Parse JSON, ensure result is a dict. Never throws."""
    if default is None:
        default = {}
    result = loads(text, default=default)
    if not isinstance(result, dict):
        return default
    return result


def loads_list(text, default=None):
    """Parse JSON, ensure result is a list. Never throws."""
    if default is None:
        default = []
    result = loads(text, default=default)
    if not isinstance(result, list):
        return default
    return result


def dumps(obj, default="{}"):
    """Serialize to JSON. Returns default string on any error. Never throws."""
    if obj is None:
        return default
    try:
        return json.dumps(obj)
    except (TypeError, ValueError, OverflowError) as e:
        logger.debug("safe_json.dumps failed: %s", e)
        return default
