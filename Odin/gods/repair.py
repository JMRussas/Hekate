"""Repair handlers — act on repair_command events to fix failures.

Each handler corresponds to a repair strategy from diagnosis.py:
  - retry_with_fix: re-emit original event with guidance injected
  - change_provider: re-emit original event with different provider
  - escalate: emit human_intervention_needed (in diagnosis.py)

The make_repair_router() convenience registers all handlers on a pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from gods.pipeline import Event, Emit, Pipeline

logger = logging.getLogger("gods.repair")


# ---------------------------------------------------------------------------
# retry_with_fix — re-emit original event with prompt guidance
# ---------------------------------------------------------------------------

async def retry_with_fix_handler(event: Event, db) -> list[Emit] | None:
    """Re-emit the original event with fix guidance injected.

    The guidance goes into _fix_guidance in the payload so the
    original handler can read it and modify its behavior.
    """
    original_type = event.payload.get("original_event_type")
    if not original_type:
        logger.warning("retry_with_fix: no original_event_type in payload")
        return None

    original_payload = dict(event.payload.get("original_payload", {}))
    fix_detail = event.payload.get("fix_detail", {})

    original_payload["_fix_guidance"] = fix_detail.get("guidance", "")
    original_payload["_repair_attempt"] = True

    logger.info(
        "retry_with_fix: re-emitting %s with guidance",
        original_type,
    )

    return [Emit(
        event_type=original_type,
        payload=original_payload,
        source="repair",
    )]


# ---------------------------------------------------------------------------
# change_provider — re-emit with a different LLM provider
# ---------------------------------------------------------------------------

async def change_provider_handler(event: Event, db) -> list[Emit] | None:
    """Re-emit the original event with a different provider."""
    original_type = event.payload.get("original_event_type")
    if not original_type:
        logger.warning("change_provider: no original_event_type")
        return None

    fix_detail = event.payload.get("fix_detail", {})
    new_provider = fix_detail.get("new_provider")
    if not new_provider:
        logger.warning("change_provider: no new_provider in fix_detail")
        return None

    original_payload = dict(event.payload.get("original_payload", {}))
    original_payload["provider"] = new_provider
    original_payload["_repair_attempt"] = True

    logger.info(
        "change_provider: re-emitting %s with provider=%s",
        original_type, new_provider,
    )

    return [Emit(
        event_type=original_type,
        payload=original_payload,
        source="repair",
    )]


# ---------------------------------------------------------------------------
# make_repair_router — register all repair handlers on a pipeline
# ---------------------------------------------------------------------------

def make_repair_router(pipeline: Pipeline):
    """Register all repair strategy handlers on the pipeline.

    Call this once during pipeline setup. It registers:
      - retry_with_fix_handler for strategy="retry_with_fix"
      - change_provider_handler for strategy="change_provider"
      - escalation_handler for strategy="escalate"
    """
    from gods.diagnosis import escalation_handler

    pipeline.register(
        "repair_command",
        retry_with_fix_handler,
        name="repair:retry_with_fix",
        filter=lambda e: e.payload.get("strategy") == "retry_with_fix",
    )

    pipeline.register(
        "repair_command",
        change_provider_handler,
        name="repair:change_provider",
        filter=lambda e: e.payload.get("strategy") == "change_provider",
    )

    pipeline.register(
        "repair_command",
        escalation_handler,
        name="repair:escalate",
        filter=lambda e: e.payload.get("strategy") == "escalate",
    )

    logger.info("Repair router registered (3 strategy handlers)")
