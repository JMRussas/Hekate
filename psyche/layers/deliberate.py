"""Deliberate layer — verifies or replaces the reflex claim.

Reads the current response doc, asks a mid-tier model to verdict on it,
emits patches for claim/reasoning/confidence.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from gateway_client import GatewayClient, GatewayError
from json_extract import extract_json

logger = logging.getLogger("psyche.deliberate")


async def run(
    *,
    message: str,
    history: list[dict],
    doc_snapshot: dict,
    config: dict,
    gateway: GatewayClient,
) -> AsyncIterator[dict]:
    """Yield patches for the deliberate layer."""
    history_str = ""
    if history:
        lines = [f"{m.get('role','user')}: {m.get('content','')}" for m in history[-6:]]
        history_str = "\n".join(lines) + "\n\n"

    reflex_claim = doc_snapshot.get("claim", "")
    reflex_confidence = doc_snapshot.get("confidence", 0.0)

    user_payload = (
        f"{history_str}user: {message}\n\n"
        f"REFLEX_CLAIM: {reflex_claim}\n"
        f"REFLEX_CONFIDENCE: {reflex_confidence}\n\n"
        f"Verdict the reflex claim and respond in the required JSON format."
    )

    try:
        text = await gateway.one_shot(
            provider=config.get("provider", "claude"),
            system_prompt=config["system_prompt"],
            user_message=user_payload,
            model=config.get("model"),
            timeout_s=config.get("timeout_s", 180),
        )
    except GatewayError as e:
        logger.error("deliberate gateway error: %s", e)
        yield {"op": "add", "path": "/reasoning", "value": f"[deliberate error: {e}]"}
        return

    obj = extract_json(text)
    verdict = obj.get("verdict", "confirm")
    new_claim = obj.get("claim", reflex_claim)
    reasoning = obj.get("reasoning", "")
    try:
        confidence = float(obj.get("confidence", reflex_confidence))
    except (TypeError, ValueError):
        confidence = reflex_confidence
    confidence = max(0.0, min(1.0, confidence))

    # Claim is only updated if deliberate changed it (avoid noise patches)
    if verdict in ("refine", "replace") and new_claim and new_claim != reflex_claim:
        yield {"op": "replace", "path": "/claim", "value": new_claim}

    if reasoning:
        yield {"op": "add", "path": "/reasoning", "value": reasoning}

    if abs(confidence - reflex_confidence) > 0.01:
        yield {"op": "replace", "path": "/confidence", "value": confidence}
