"""Mock-gateway pipeline tests covering the fixes from the v1 review.

Run from psyche/ directory:
  python tests/test_pipeline.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Make psyche/ importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from gateway_client import GatewayError
from pipeline import run_chat


CONFIG = {
    "layers": {
        "reflex": {
            "enabled": True, "provider": "claude", "model": "x",
            "timeout_s": 10, "system_prompt": "p",
        },
        "deliberate": {
            "enabled": True, "provider": "claude", "model": "x",
            "timeout_s": 10, "system_prompt": "p",
        },
        "critic": {
            "enabled": False, "provider": "claude", "model": "x",
            "timeout_s": 10, "system_prompt": "p",
        },
        "recall": {
            "enabled": False, "provider": "claude", "model": "x",
            "timeout_s": 10, "system_prompt": "p",
        },
    },
    "routing": {
        "short_circuit_confidence": 0.85,
        "force_deliberate_classes": ["code", "math", "factual", "multi_step"],
        "reflex_only_classes": ["greeting", "chitchat", "opinion"],
    },
}


class MockGateway:
    """Returns canned JSON responses in order."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    async def one_shot(self, **kw):
        i = self.calls
        self.calls += 1
        return json.dumps(self.responses[i])


async def collect(gen):
    out = []
    async for ev in gen:
        out.append(ev)
    return out


def get_done(events):
    return next(e for e in events if e["event"] == "done")["data"]["doc"]


async def test_short_circuit_chitchat():
    """High confidence on chitchat: reflex only, no deliberate even though enabled."""
    gw = MockGateway([{"claim": "hi back", "confidence": 0.95}])
    events = await collect(run_chat(
        message="just saying hi today",
        history=[], config=CONFIG, gateway=gw,
    ))
    assert gw.calls == 1, f"expected 1 call, got {gw.calls}"
    final = get_done(events)
    assert final["status"] == "done"
    assert final["layers_fired"] == ["reflex"]
    print("OK test_short_circuit_chitchat")


async def test_force_deliberate_factual():
    """Factual class forces deliberate even at high reflex confidence."""
    gw = MockGateway([
        {"claim": "capital is paris", "confidence": 0.99},
        {"verdict": "confirm", "claim": "capital is paris",
         "reasoning": "correct", "confidence": 0.99},
    ])
    events = await collect(run_chat(
        message="what is the capital of france",
        history=[], config=CONFIG, gateway=gw,
    ))
    assert gw.calls == 2, f"expected 2 calls, got {gw.calls}"
    final = get_done(events)
    assert final["layers_fired"] == ["reflex", "deliberate"]
    assert final["status"] == "done"
    # status patch should appear
    status_patches = [
        e for e in events
        if e["event"] == "patch" and e["data"].get("path") == "/status"
    ]
    assert len(status_patches) == 1, status_patches
    print("OK test_force_deliberate_factual")


async def test_reflex_error_aborts_chain():
    """Gateway error in reflex must NOT trigger deliberate on garbage input."""

    class ErrGW:
        def __init__(self):
            self.calls = 0

        async def one_shot(self, **kw):
            self.calls += 1
            raise GatewayError("boom")

    gw = ErrGW()
    events = await collect(run_chat(
        message="what is rayleigh scattering",
        history=[], config=CONFIG, gateway=gw,
    ))
    assert gw.calls == 1, f"expected 1 call (no deliberate), got {gw.calls}"
    types = [e["event"] for e in events]
    assert "layer_error" in types
    final = get_done(events)
    assert final["status"] == "error"
    assert "deliberate" not in final["layers_fired"]
    print("OK test_reflex_error_aborts_chain")


async def test_layer_crash_rollback():
    """A layer crashing mid-stream rolls back the doc to its pre-layer snapshot."""

    class CrashGW:
        def __init__(self):
            self.calls = 0

        async def one_shot(self, **kw):
            i = self.calls
            self.calls += 1
            if i == 0:
                return json.dumps({"claim": "good answer", "confidence": 0.5})
            raise RuntimeError("mid-stream blowup")

    gw = CrashGW()
    events = await collect(run_chat(
        message="walk me through how to design a system",
        history=[], config=CONFIG, gateway=gw,
    ))
    final = get_done(events)
    # claim should still be reflex's value
    assert final["claim"] == "good answer", final
    assert "deliberate" not in final["layers_fired"], final
    assert "reflex" in final["layers_fired"]
    rollback = [
        e for e in events
        if e["event"] == "layer_error" and e["data"].get("rolled_back")
    ]
    assert len(rollback) == 1, rollback
    print("OK test_layer_crash_rollback")


async def test_cancellation_between_layers():
    """Caller disconnect check fires between layers, halts deeper execution."""
    gw = MockGateway([
        {"claim": "first", "confidence": 0.5},
        {"verdict": "confirm", "claim": "first",
         "reasoning": "ok", "confidence": 0.5},
    ])
    state = {"count": 0}

    async def cancel_after_reflex():
        state["count"] += 1
        # First poll (before reflex) returns False; second (before deliberate) True
        return state["count"] >= 2

    events = await collect(run_chat(
        message="what is x",
        history=[], config=CONFIG, gateway=gw,
        cancelled=cancel_after_reflex,
    ))
    types = [e["event"] for e in events]
    assert "cancelled" in types
    final = get_done(events)
    assert final["status"] == "error"
    assert gw.calls == 1, f"expected 1 call, got {gw.calls}"
    print("OK test_cancellation_between_layers")


async def test_suppression_signaled():
    """max_layers cap emits a suppressed event, doesn't silently drop."""
    gw = MockGateway([{"claim": "paris", "confidence": 0.9}])
    events = await collect(run_chat(
        message="what is the capital of france",
        history=[], config=CONFIG, gateway=gw,
        suppressed_layers={"deliberate", "critic", "recall"},
    ))
    types = [e["event"] for e in events]
    assert "suppressed" in types
    sup = next(e for e in events if e["event"] == "suppressed")["data"]
    assert sup["layer"] == "deliberate"
    assert sup["would_have_been_forced"] is True
    assert gw.calls == 1
    print("OK test_suppression_signaled")


async def main():
    await test_short_circuit_chitchat()
    await test_force_deliberate_factual()
    await test_reflex_error_aborts_chain()
    await test_layer_crash_rollback()
    await test_cancellation_between_layers()
    await test_suppression_signaled()
    print()
    print("all pipeline tests passed")


if __name__ == "__main__":
    asyncio.run(main())
