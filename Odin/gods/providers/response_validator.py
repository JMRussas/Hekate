"""Response validation for LLM outputs.

Validates and normalizes responses from any LLM provider.
Handles: malformed JSON, arrays instead of objects, markdown fencing,
prose responses, missing required fields.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("gods.providers.response_validator")


def extract_json(text: str) -> Any | None:
    """Extract JSON from LLM response text.

    Handles:
      - Clean JSON: {"key": "value"}
      - Markdown fenced: ```json\n{...}\n```
      - JSON embedded in prose: "Here's the result: {...}"
      - Arrays: [{"key": "value"}]
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown fencing
    fenced = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Find JSON object in text
    obj_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if obj_match:
        try:
            return json.loads(obj_match.group())
        except json.JSONDecodeError:
            pass

    # Find JSON array in text
    arr_match = re.search(r'\[.*\]', text, re.DOTALL)
    if arr_match:
        try:
            return json.loads(arr_match.group())
        except json.JSONDecodeError:
            pass

    return None


def validate_response(
    text: str,
    required_fields: list[str] | None = None,
    expected_type: type = dict,
) -> tuple[Any, list[str]]:
    """Validate and normalize an LLM response.

    Returns: (parsed_data, errors)
      - parsed_data: the validated data, or None if unparseable
      - errors: list of validation error strings (empty if valid)
    """
    errors: list[str] = []

    parsed = extract_json(text)

    if parsed is None:
        errors.append(f"Could not extract JSON from response ({len(text)} chars)")
        return None, errors

    # Normalize: array → first dict element
    if isinstance(parsed, list):
        if len(parsed) == 0:
            errors.append("Response is an empty array")
            return None, errors
        if isinstance(parsed[0], dict):
            logger.debug("Normalized array response to first element")
            parsed = parsed[0]
        else:
            errors.append(f"Array contains non-dict elements: {type(parsed[0]).__name__}")
            return None, errors

    # Type check
    if not isinstance(parsed, expected_type):
        errors.append(f"Expected {expected_type.__name__}, got {type(parsed).__name__}")
        return None, errors

    # Required fields check
    if required_fields and isinstance(parsed, dict):
        missing = [f for f in required_fields if f not in parsed]
        if missing:
            errors.append(f"Missing required fields: {missing}")

    return parsed, errors


def validate_verdict(text: str) -> dict:
    """Validate a verification verdict response.

    Always returns a valid dict with {verdict, confidence, feedback}.
    Falls back to prose analysis if JSON parsing fails.
    """
    parsed, errors = validate_response(
        text,
        required_fields=["verdict"],
        expected_type=dict,
    )

    if parsed and not errors:
        # Normalize verdict values
        verdict = parsed.get("verdict", "human_needed")
        if verdict not in ("passed", "gaps_found", "human_needed"):
            # Map common variations
            v_lower = verdict.lower()
            if v_lower in ("pass", "approved", "success", "ok", "satisfied"):
                verdict = "passed"
            elif v_lower in ("fail", "failed", "rejected", "gaps"):
                verdict = "gaps_found"
            else:
                verdict = "human_needed"
            parsed["verdict"] = verdict

        parsed.setdefault("confidence", 0.5)
        parsed.setdefault("feedback", "")
        return parsed

    if parsed and errors:
        # Has some data but missing fields — fill defaults
        parsed.setdefault("verdict", "human_needed")
        parsed.setdefault("confidence", 0.3)
        parsed.setdefault("feedback", "; ".join(errors))
        return parsed

    # No JSON at all — analyze prose
    lower = (text or "").lower()
    if any(w in lower for w in ["passed", "satisf", "correct", "done", "complet", "approved", "looks good"]):
        return {"verdict": "passed", "confidence": 0.6, "feedback": text[:200]}
    elif any(w in lower for w in ["fail", "gap", "missing", "incorrect", "wrong", "error"]):
        return {"verdict": "gaps_found", "confidence": 0.6, "feedback": text[:200]}
    else:
        return {"verdict": "human_needed", "confidence": 0.0, "feedback": text[:200] if text else "Empty response"}


def validate_review(text: str) -> dict:
    """Validate a code review response.

    Always returns {verdict: "approved"|"changes_requested", feedback}.
    """
    parsed, errors = validate_response(
        text,
        required_fields=["verdict"],
        expected_type=dict,
    )

    if parsed and isinstance(parsed, dict):
        verdict = parsed.get("verdict", "approved")
        if verdict not in ("approved", "changes_requested"):
            v_lower = verdict.lower()
            if v_lower in ("pass", "passed", "ok", "lgtm", "approved"):
                verdict = "approved"
            else:
                verdict = "changes_requested"
            parsed["verdict"] = verdict
        parsed.setdefault("feedback", "")
        return parsed

    lower = (text or "").lower()
    # Check rejection keywords — but "no issues" is approval, not rejection
    rejection_words = ["reject", "change request", "need to be fixed", "needs fix", "wrong", "must change", "issues that need", "several issues"]
    approval_words = ["looks good", "no issues", "approved", "lgtm", "ship it", "good to go"]
    if any(w in lower for w in approval_words):
        return {"verdict": "approved", "feedback": text[:200]}
    if any(w in lower for w in rejection_words):
        return {"verdict": "changes_requested", "feedback": text[:200]}
    return {"verdict": "approved", "feedback": text[:200] if text else ""}
