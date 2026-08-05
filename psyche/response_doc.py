"""Response document with per-layer ownership rules.

The response doc is a JSON object that layers patch as they produce output.
Each layer may only modify fields it owns. Violations raise OwnershipError.

Ownership rules (v1):
  reflex:      writes `claim`, `confidence`, `status`
  deliberate:  may REPLACE `claim`, writes `reasoning`, updates `confidence`
  critic:      append-only to `caveats`
  recall:      append-only to `citations`
  pipeline:    writes `status`, appends to `layers_fired`

Patches follow a subset of RFC 6902 JSON Patch: {op, path, value}.
Supported ops: add, replace, append (append is non-standard, for arrays).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class OwnershipError(Exception):
    """Raised when a layer attempts to modify a field it does not own."""


# Maps layer name to (owned_fields_replace, owned_fields_append)
# replace = layer may `add` or `replace` these fields
# append  = layer may only `append` to these array fields
_OWNERSHIP: dict[str, tuple[set[str], set[str]]] = {
    "reflex": ({"claim", "confidence", "status"}, set()),
    "deliberate": ({"claim", "reasoning", "confidence"}, set()),
    "critic": (set(), {"caveats"}),
    "recall": (set(), {"citations"}),
    "pipeline": ({"status"}, {"layers_fired"}),
}


@dataclass
class ResponseDoc:
    claim: str = ""
    confidence: float = 0.0
    reasoning: str = ""
    caveats: list[str] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    layers_fired: list[str] = field(default_factory=list)
    status: str = "thinking"  # thinking | done | error

    def snapshot(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "caveats": list(self.caveats),
            "citations": list(self.citations),
            "layers_fired": list(self.layers_fired),
            "status": self.status,
        }

    def apply(self, layer: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a patch on behalf of `layer`. Returns the normalized patch
        (with op rewritten if needed) so the caller can emit it over SSE.

        Raises OwnershipError on violation.
        """
        if layer not in _OWNERSHIP:
            raise OwnershipError(f"unknown layer: {layer}")

        replace_fields, append_fields = _OWNERSHIP[layer]
        op = patch.get("op")
        path = patch.get("path", "")
        value = patch.get("value")

        if not path.startswith("/"):
            raise OwnershipError(f"path must start with /: {path}")
        field_name = path[1:].split("/", 1)[0]

        if op in ("add", "replace"):
            if field_name not in replace_fields:
                raise OwnershipError(
                    f"layer '{layer}' cannot {op} field '{field_name}'"
                )
            setattr(self, field_name, value)
            return {"op": op, "path": f"/{field_name}", "value": value}

        elif op == "append":
            if field_name not in append_fields:
                raise OwnershipError(
                    f"layer '{layer}' cannot append to field '{field_name}'"
                )
            current = getattr(self, field_name)
            current.append(value)
            # Normalize to JSON-patch-compatible add at index
            return {
                "op": "add",
                "path": f"/{field_name}/-",
                "value": value,
            }

        else:
            raise OwnershipError(f"unsupported op: {op}")
