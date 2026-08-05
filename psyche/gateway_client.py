"""Thin client for the LLM Gateway (port 5210).

Holds a long-lived httpx.AsyncClient for connection reuse. Lifecycle is
managed by the FastAPI app: call `await client.aclose()` on shutdown.

Two call styles:
  - one_shot()   — POST /v1/chat, returns full text when done
  - stream()     — POST /v1/conversation/stream, yields token events
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger("psyche.gateway_client")


class GatewayError(Exception):
    pass


class GatewayClient:
    def __init__(self, base_url: str):
        self._base = base_url.rstrip("/")
        # Long-lived client. Per-call timeouts override the default.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def one_shot(
        self,
        *,
        provider: str,
        system_prompt: str,
        user_message: str,
        model: Optional[str] = None,
        timeout_s: int = 120,
    ) -> str:
        """POST /v1/chat and return the text response."""
        payload = {
            "provider": provider,
            "system_prompt": system_prompt,
            "user_message": user_message,
            "model": model,
            "timeout": timeout_s,
        }
        try:
            r = await self._client.post(
                f"{self._base}/v1/chat",
                json=payload,
                timeout=float(timeout_s + 30),
            )
        except httpx.HTTPError as e:
            raise GatewayError(f"gateway request failed: {e}") from e
        if r.status_code != 200:
            raise GatewayError(
                f"gateway returned {r.status_code}: {r.text[:200]}"
            )
        data = r.json()
        return data.get("text", "")

    async def stream(
        self,
        *,
        message: str,
        model: str = "claude-sonnet-4-6",
        conversation_id: Optional[str] = None,
        timeout_s: int = 300,
    ) -> AsyncIterator[dict]:
        """POST /v1/conversation/stream and yield parsed SSE events.

        Each yielded value is a dict: {"event": str, "data": dict}.
        Terminates after a 'done' event.
        """
        payload = {
            "message": message,
            "conversation_id": conversation_id,
            "model": model,
        }
        async with self._client.stream(
            "POST",
            f"{self._base}/v1/conversation/stream",
            json=payload,
            timeout=float(timeout_s),
        ) as r:
            if r.status_code != 200:
                body = await r.aread()
                raise GatewayError(
                    f"gateway stream returned {r.status_code}: {body[:200]!r}"
                )
            event_name = ""
            data_lines: list[str] = []
            async for line in r.aiter_lines():
                if line == "":
                    if event_name or data_lines:
                        try:
                            data = json.loads("\n".join(data_lines)) if data_lines else {}
                        except json.JSONDecodeError:
                            data = {"raw": "\n".join(data_lines)}
                        yield {"event": event_name, "data": data}
                        if event_name == "done":
                            return
                    event_name = ""
                    data_lines = []
                elif line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
