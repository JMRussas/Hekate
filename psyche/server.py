#!/usr/bin/env python3
"""Psyche — multi-layer cognitive chat service.

Port: 5214 (default, overridable via config.json or PSYCHE_PORT env var)

Endpoints:
  GET  /ping
  GET  /health
  GET  /config
  POST /config/reload
  POST /chat/stream    — SSE stream of pipeline events

The pipeline is stateless in v1: callers pass full history each turn.
A `conversation_id` field is accepted (and echoed) but not yet used; it
exists so callers can adopt the field shape now and have it become
load-bearing when server-side state lands in v2.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# Local imports — add current dir to path so `layers.` / `gateway_client`
# resolve without a package installer step.
sys.path.insert(0, str(Path(__file__).parent))

from gateway_client import GatewayClient  # noqa: E402
from pipeline import run_chat  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("psyche")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent / "config.json"


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise RuntimeError(f"psyche config not found: {_CONFIG_PATH}")
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = _load_config()
PORT = int(os.environ.get("PSYCHE_PORT", CONFIG.get("port", 5214)))
GATEWAY_URL = os.environ.get(
    "LLM_GATEWAY_URL", CONFIG.get("gateway_url", "http://localhost:5210")
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class HistoryMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[HistoryMessage] = []
    max_layers: Optional[str] = None  # "reflex" | "deliberate" | "all"
    conversation_id: Optional[str] = None  # reserved for v2 server-side state


# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------

_gateway: Optional[GatewayClient] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _gateway
    _gateway = GatewayClient(GATEWAY_URL)
    logger.info("Psyche ready on port %d, gateway=%s", PORT, GATEWAY_URL)
    try:
        yield
    finally:
        if _gateway is not None:
            await _gateway.aclose()
        logger.info("Psyche shutting down")


app = FastAPI(title="Psyche", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/ping")
async def ping():
    return {"message": "pong"}


@app.get("/health")
async def health():
    return {"status": "ok", "gateway": GATEWAY_URL, "port": PORT}


@app.get("/config")
async def get_config():
    """Return the currently loaded config (useful for debugging)."""
    return CONFIG


@app.post("/config/reload")
async def reload_config():
    """Hot-reload config.json without restarting the service.

    NOTE: unauthenticated. Safe for loopback only — do not expose Psyche
    beyond localhost without adding auth on this endpoint.
    """
    global CONFIG
    try:
        CONFIG = _load_config()
    except Exception as e:
        raise HTTPException(500, f"reload failed: {e}")
    return {"status": "reloaded"}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _suppression_set(max_layers: Optional[str]) -> set[str]:
    """Return the set of layer names suppressed by the caller's max_layers cap.

    Suppression is a runtime signal — the pipeline emits a `suppressed` event
    for each capped layer rather than silently dropping it.
    """
    if not max_layers or max_layers == "all":
        return set()
    if max_layers == "reflex":
        return {"deliberate", "critic", "recall"}
    if max_layers == "deliberate":
        return {"critic", "recall"}
    return set()


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    """Stream the cognitive pipeline as SSE.

    Events emitted:
      meta          — classification + plan + suppressed_layers
      layer_start   — a layer is about to run
      patch         — a field update from a layer
      layer_done    — layer finished (errored: bool)
      layer_error   — layer crashed or aborted the chain
      short_circuit — confidence gate skipped remaining layers
      suppressed    — caller's max_layers cap dropped this layer
      cancelled     — client disconnected
      done          — final doc snapshot
    """
    history = [m.model_dump() for m in req.history]
    suppressed = _suppression_set(req.max_layers)

    async def is_disconnected() -> bool:
        try:
            return await request.is_disconnected()
        except Exception:
            return False

    async def generate():
        try:
            async for event in run_chat(
                message=req.message,
                history=history,
                config=CONFIG,
                gateway=_gateway,
                cancelled=is_disconnected,
                suppressed_layers=suppressed,
            ):
                yield _sse(event["event"], event["data"])
        except Exception as e:
            logger.exception("pipeline crashed")
            yield _sse("error", {"message": str(e)})
            yield _sse("done", {"doc": {}})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
