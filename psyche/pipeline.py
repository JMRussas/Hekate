"""Psyche pipeline — fall-through layer orchestrator.

Runs reflex → deliberate → (critic) → (recall), short-circuiting when
reflex confidence exceeds the configured threshold AND the message class
does not force deeper layers.

Each layer yields patches. The pipeline applies patches to the shared
ResponseDoc, emits pipeline-level events, and yields every event out
to the caller.

Failure modes handled:
  - Reflex emits {"op":"error"}: abort the chain, no deeper layers run.
  - A layer crashes mid-stream: roll back the doc to the pre-layer
    snapshot before continuing to the next layer (so partial damage
    cannot leak into a subsequent layer's view).
  - Caller disconnects (cancelled callback returns True): break out of
    the loop after the current layer's next yield boundary.
"""

from __future__ import annotations

import logging
import time
from typing import AsyncIterator, Awaitable, Callable, Optional

from gateway_client import GatewayClient
from layers import router, reflex, deliberate, critic, recall
from response_doc import ResponseDoc, OwnershipError

logger = logging.getLogger("psyche.pipeline")


_LAYER_RUNNERS = {
    "reflex": reflex.run,
    "deliberate": deliberate.run,
    "critic": critic.run,
    "recall": recall.run,
}

# Layers that come before this one are responsible for short-circuit checks.
# After reflex completes, ALL subsequent layers are subject to the gate.
_REFLEX = "reflex"


CancelledFn = Callable[[], Awaitable[bool]]


async def _never_cancelled() -> bool:
    return False


def _restore_doc(doc: ResponseDoc, snapshot: dict) -> None:
    """Mutate doc in place to match the given snapshot."""
    doc.claim = snapshot["claim"]
    doc.confidence = snapshot["confidence"]
    doc.reasoning = snapshot["reasoning"]
    doc.caveats = list(snapshot["caveats"])
    doc.citations = list(snapshot["citations"])
    doc.layers_fired = list(snapshot["layers_fired"])
    doc.status = snapshot["status"]


async def run_chat(
    *,
    message: str,
    history: list[dict],
    config: dict,
    gateway: GatewayClient,
    cancelled: Optional[CancelledFn] = None,
    suppressed_layers: Optional[set[str]] = None,
) -> AsyncIterator[dict]:
    """Orchestrate the fall-through cognitive pipeline.

    Args:
      message: user input
      history: prior turns [{role, content}, ...]
      config: full psyche config dict
      gateway: long-lived gateway client
      cancelled: async callable returning True if the caller has gone away
      suppressed_layers: layer names disabled by an explicit caller cap
        (max_layers). These get a `suppressed` event so the caller knows
        the cap, not the routing, dropped them.
    """
    cancelled = cancelled or _never_cancelled
    suppressed_layers = suppressed_layers or set()

    doc = ResponseDoc()
    layers_cfg = config.get("layers", {})
    routing = config.get("routing", {})
    enabled = {name: cfg.get("enabled", False) for name, cfg in layers_cfg.items()}

    msg_class = router.classify(message)
    plan = router.plan_layers(
        msg_class,
        enabled,
        routing.get("force_deliberate_classes", []),
        routing.get("reflex_only_classes", []),
    )
    force_deliberate = router.forces_deliberate(
        msg_class, routing.get("force_deliberate_classes", [])
    )
    short_circuit_at = float(routing.get("short_circuit_confidence", 0.85))

    yield {
        "event": "meta",
        "data": {
            "message_class": msg_class,
            "plan": plan,
            "force_deliberate": force_deliberate,
            "short_circuit_confidence": short_circuit_at,
            "suppressed_layers": sorted(suppressed_layers),
        },
    }

    reflex_complete = False
    chain_aborted = False

    for layer_name in plan:
        # Caller went away?
        if await cancelled():
            yield {"event": "cancelled", "data": {"at_layer": layer_name}}
            chain_aborted = True
            break

        # Suppressed by max_layers cap — emit signal so the caller can tell
        # this is intentional and not a routing decision.
        if layer_name in suppressed_layers:
            yield {
                "event": "suppressed",
                "data": {
                    "layer": layer_name,
                    "reason": "max_layers cap",
                    "would_have_been_forced": (
                        force_deliberate and layer_name == "deliberate"
                    ),
                },
            }
            continue

        # Short-circuit gate: applies to ANY layer after reflex.
        # Skipped only when the message class explicitly forces deeper thought.
        if reflex_complete and not force_deliberate:
            if doc.confidence >= short_circuit_at:
                yield {
                    "event": "short_circuit",
                    "data": {
                        "skipped_from": layer_name,
                        "reason": (
                            f"reflex confidence {doc.confidence:.2f} "
                            f">= {short_circuit_at}"
                        ),
                    },
                }
                break

        layer_cfg = layers_cfg.get(layer_name, {})
        if not layer_cfg.get("enabled"):
            continue

        runner = _LAYER_RUNNERS.get(layer_name)
        if runner is None:
            continue

        yield {"event": "layer_start", "data": {"layer": layer_name}}
        t0 = time.monotonic()

        # Snapshot for rollback if this layer crashes mid-stream.
        pre_snapshot = doc.snapshot()

        kwargs = {
            "message": message,
            "history": history,
            "config": layer_cfg,
            "gateway": gateway,
        }
        if layer_name != _REFLEX:
            kwargs["doc_snapshot"] = pre_snapshot

        layer_errored = False
        try:
            async for patch in runner(**kwargs):
                # Sentinel from reflex: gateway error → abort chain.
                if patch.get("op") == "error":
                    yield {
                        "event": "layer_error",
                        "data": {
                            "layer": layer_name,
                            "error": patch.get("value", "unknown error"),
                            "abort_chain": True,
                        },
                    }
                    layer_errored = True
                    chain_aborted = True
                    break

                try:
                    normalized = doc.apply(layer_name, patch)
                except OwnershipError as e:
                    logger.warning(
                        "ownership violation from %s: %s", layer_name, e
                    )
                    continue

                yield {
                    "event": "patch",
                    "data": {"layer": layer_name, **normalized},
                }
        except Exception as e:
            logger.exception("layer %s crashed", layer_name)
            # Roll back any partial changes from this layer.
            _restore_doc(doc, pre_snapshot)
            yield {
                "event": "layer_error",
                "data": {
                    "layer": layer_name,
                    "error": str(e),
                    "rolled_back": True,
                },
            }
            layer_errored = True

        # Audit trail (only if the layer actually ran something useful)
        if not layer_errored:
            try:
                normalized = doc.apply(
                    "pipeline",
                    {"op": "append", "path": "/layers_fired", "value": layer_name},
                )
                yield {"event": "patch", "data": {"layer": "pipeline", **normalized}}
            except OwnershipError:
                pass

        yield {
            "event": "layer_done",
            "data": {
                "layer": layer_name,
                "elapsed_s": round(time.monotonic() - t0, 3),
                "errored": layer_errored,
            },
        }

        if layer_name == _REFLEX:
            reflex_complete = True

        if chain_aborted:
            break

    # Status transition via apply() so a UI tracking patches sees it.
    final_status = "error" if chain_aborted else "done"
    try:
        normalized = doc.apply(
            "pipeline", {"op": "replace", "path": "/status", "value": final_status}
        )
        yield {"event": "patch", "data": {"layer": "pipeline", **normalized}}
    except OwnershipError:
        doc.status = final_status

    yield {"event": "done", "data": {"doc": doc.snapshot()}}
