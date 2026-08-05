"""Reflex layer — fast first-pass answer + predicted confidence.

Calls the gateway with a small model, parses a JSON response, emits
patches to claim/confidence/status.

On gateway error, yields a special `error` field via the sentinel
{"op":"error", "value": "..."}. The pipeline interprets this as
"abort the chain" — deeper layers should not run on top of a
broken reflex.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from gateway_client import GatewayClient, GatewayError
from json_extract import extract_json

logger = logging.getLogger("psyche.reflex")


async def run(
    *,
    message: str,
    history: list[dict],
    config: dict,
    gateway: GatewayClient,
) -> AsyncIterator[dict]:
    """Yield patches for the reflex layer.

    Patches are layer-scoped: {"op","path","value"}. The pipeline is
    responsible for calling ResponseDoc.apply(layer="reflex", patch).
    """
    history_str = ""
    if history:
        lines = [f"{m.get('role','user')}: {m.get('content','')}" for m in history[-6:]]
        history_str = "\n".join(lines) + "\n\n"

    user_payload = f"{history_str}user: {message}"

    try:
        text = await gateway.one_shot(
            provider=config.get("provider", "claude"),
            system_prompt=config["system_prompt"],
            user_message=user_payload,
            model=config.get("model"),
            timeout_s=config.get("timeout_s", 60),
        )
    except GatewayError as e:
        logger.error("reflex gateway error: %s", e)
        # Sentinel: pipeline interprets {"op":"error"} as abort-chain.
        yield {"op": "error", "value": f"reflex gateway error: {e}"}
        return

    obj = extract_json(text)
    claim = obj.get("claim") or text.strip()
    try:
        confidence = float(obj.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    yield {"op": "add", "path": "/claim", "value": claim}
    yield {"op": "add", "path": "/confidence", "value": confidence}
