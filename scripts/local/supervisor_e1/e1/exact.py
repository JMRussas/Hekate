"""Exact JSON numbers for the plan-contract wire (plan 023 E1a; test-only). Pure: no I/O.

Python ints are arbitrary precision, but float/exponent tokens are REJECTED rather than
parsed into rounded floats, NaN/Infinity are rejected, and contract counters must be
non-bool ints within minimum..2^63-1.
"""

from __future__ import annotations

import json
from typing import Any

INT64_MAX = 2**63 - 1


class WireError(Exception):
    """A document that is not exact, well-formed contract JSON of the expected shape."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _reject_float(token: str) -> Any:
    raise WireError("unsupported_number", f"non-integer number token {token!r}")


def _reject_constant(token: str) -> Any:
    raise WireError("unsupported_number", f"non-JSON constant {token!r}")


INT64_MIN = -(2**63)


def _int_token(token: str) -> int:
    """EVERY integer token in the document (including one later shadowed by a duplicate key) must
    fit Int64: a raw-scan rule, distinct from semantic counter validation (counter())."""
    v = int(token)
    if not INT64_MIN <= v <= INT64_MAX:
        raise WireError("integer_out_of_range", f"integer token {token} outside Int64")
    return v


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for k, v in pairs:
        if k in seen:
            raise WireError("duplicate_key", f"duplicate object key {k!r}")
        seen[k] = v
    return seen


def loads_exact(raw: bytes | str) -> Any:
    """Exact JSON: float/exponent tokens and NaN/Infinity rejected, every integer token within
    Int64, duplicate keys rejected (the contract API never emits them)."""
    try:
        return json.loads(raw, parse_float=_reject_float, parse_int=_int_token, parse_constant=_reject_constant,
                          object_pairs_hook=_no_duplicate_keys)
    except WireError:
        raise
    except (ValueError, UnicodeDecodeError) as e:
        raise WireError("malformed_json", str(e)) from e


def counter(value: Any, what: str, *, minimum: int = 0, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise WireError("unexpected_shape", f"{what} must be an integer, got {type(value).__name__}")
    if value < minimum or value > INT64_MAX:
        raise WireError("unexpected_shape", f"{what}={value} outside {minimum}..2^63-1")
    return value
