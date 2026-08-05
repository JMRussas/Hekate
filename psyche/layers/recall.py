"""Recall layer — append-only citations. Stub in v1 (no-op when disabled).

Future: query Agent Context MCP (port 5213) instead of calling an LLM.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from gateway_client import GatewayClient, GatewayError
from json_extract import extract_json

logger = logging.getLogger("psyche.recall")


async def run(
    *,
    message: str,
    history: list[dict],
    doc_snapshot: dict,
    config: dict,
    gateway: GatewayClient,
) -> AsyncIterator[dict]:
    claim = doc_snapshot.get("claim", "")

    user_payload = (
        f"user: {message}\n\n"
        f"CURRENT_CLAIM: {claim}\n\n"
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
        logger.warning("recall gateway error: %s", e)
        return

    obj = extract_json(text)
    citations = obj.get("citations", []) or []
    for c in citations:
        if isinstance(c, dict) and c.get("source"):
            yield {"op": "append", "path": "/citations", "value": c}
