"""Tyche handler — budget tracking and cost gating.

Functions:
  - tyche_check_budget: gate check before dispatch (is budget sufficient?)
  - tyche_record_spend: record cost after worker_event:completed
"""

from __future__ import annotations

import logging
import time
from typing import Any

from gods.pipeline import Event, Emit

logger = logging.getLogger("gods.handlers.tyche")


# ---------------------------------------------------------------------------
# Budget check (used as a gate or callable)
# ---------------------------------------------------------------------------

async def tyche_check_budget(
    event: Event,
    db: Any,
    budget_remaining: float = float("inf"),
) -> dict:
    """Check if budget allows dispatch.

    Returns {allowed: bool, reason: str}.
    """
    estimated_cost = event.payload.get("estimated_cost", 0.0)

    if budget_remaining <= 0:
        return {
            "allowed": False,
            "reason": f"Budget exhausted (remaining: ${budget_remaining:.2f})",
        }

    if estimated_cost > budget_remaining:
        return {
            "allowed": False,
            "reason": f"Budget insufficient: estimated ${estimated_cost:.2f}, "
                      f"remaining ${budget_remaining:.2f}",
        }

    return {
        "allowed": True,
        "reason": f"Budget OK: ${budget_remaining:.2f} remaining",
    }


# ---------------------------------------------------------------------------
# Record spend after completion
# ---------------------------------------------------------------------------

async def tyche_record_spend(event: Event, db: Any) -> list[Emit] | None:
    """Record cost after worker_event:completed.

    Emits budget_spent event with cost details.
    """
    status = event.payload.get("status")
    if status != "completed":
        return None

    cost_usd = event.payload.get("cost_usd", 0.0)
    task_id = event.payload.get("task_id")
    project_id = event.payload.get("project_id")

    if cost_usd <= 0:
        return None

    logger.info("Tyche: task %s cost $%.4f", (task_id or "?")[:8], cost_usd)

    return [Emit("budget_spent", {
        "task_id": task_id,
        "project_id": project_id,
        "cost_usd": cost_usd,
        "model_used": event.payload.get("model_used"),
    }, source="tyche")]
