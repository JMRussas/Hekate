"""Critic layer — append-only caveats. Stub in v1 (no-op when disabled)."""

from __future__ import annotations

import logging
from typing import AsyncIterator

from gateway_client import GatewayClient, GatewayError
from json_extract import extract_json

logger = logging.getLogger("psyche.critic")


async def run(
    *,
    message: str,
    history: list[dict],
    doc_snapshot: dict,
    config: dict,
    gateway: GatewayClient,
) -> AsyncIterator[dict]:
    claim = doc_snapshot.get("claim", "")
    reasoning = doc_snapshot.get("reasoning", "")

    user_payload = (
        f"user: {message}\n\n"
        f"CURRENT_CLAIM: {claim}\n"
        f"CURRENT_REASONING: {reasoning}\n\n"
        f"Respond with the required JSON."
    )

    try:
        text = await gateway.one_shot(
            provider=config.get("provider", "claude"),
            system_prompt=config["system_prompt"],
            user_message=user_payload,
            model=config.get("model"),
            timeout_s=config.get("timeout_s", 60),
        )
    except GatewayError as e:
        logger.warning("critic gateway error: %s", e)
        return

    obj = extract_json(text)
    caveats = obj.get("caveats", []) or []
    for c in caveats:
        if isinstance(c, str) and c.strip():
            yield {"op": "append", "path": "/caveats", "value": c.strip()}
