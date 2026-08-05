"""Tyche handler — budget tracking and cost gating.

Functions:
  - tyche_check_budget: gate check before dispatch (is budget sufficient?)
  - tyche_record_spend: record cost after worker_event:completed
  - make_tyche_rate_gate: gate that enforces provider rate limits before dispatch
"""

from __future__ import annotations

import logging
import time
from typing import Any, TYPE_CHECKING

from gods.pipeline import Event, Emit, GateResult

if TYPE_CHECKING:
    from gods.rate_limit import ProviderRateLimiter

logger = logging.getLogger("gods.handlers.tyche")


# ---------------------------------------------------------------------------
# Budget gate factory — enforces spend limits before dispatch
# ---------------------------------------------------------------------------

def _parse_payload(row) -> dict:
    """Extract payload dict from a relay event row."""
    import json
    raw = row["payload"] if isinstance(row, dict) else row[0]
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _extract_cost(row) -> float:
    """Extract cost_usd from a relay event row's payload."""
    p = _parse_payload(row)
    try:
        return float(p.get("cost_usd", 0.0))
    except (TypeError, ValueError):
        return 0.0


def make_tyche_budget_gate(budget_config: dict):
    """Create a gate that enforces budget limits on dispatch.

    Reads budget_spent events from the relay table to compute current spend,
    then blocks dispatch if daily or per-project limits are exceeded.

    Args:
        budget_config: {daily_limit_usd: float, per_project_limit_usd: float}
    """
    daily_limit = budget_config.get("daily_limit_usd", 5.0)
    project_limit = budget_config.get("per_project_limit_usd", float("inf"))

    async def tyche_budget_gate(event: Event, emits: list[Emit], db) -> GateResult:
        from gods.flags import is_enabled
        if not is_enabled("tyche_budget", default=True):
            return GateResult(True, "Budget gate disabled by flag")

        project_id = event.payload.get("project_id")
        now = time.time()
        day_start = now - (now % 86400)  # midnight UTC

        # Sum daily spend from budget_spent events
        rows = await db.fetchall(
            "SELECT payload FROM god_relay_events "
            "WHERE event_type = $1 AND created_at >= $2",
            ("budget_spent", day_start),
        )
        daily_spent = sum(_extract_cost(r) for r in rows)

        # Sum per-project spend
        project_spent = 0.0
        if project_id:
            for r in rows:
                p = _parse_payload(r)
                if p.get("project_id") == project_id:
                    project_spent += p.get("cost_usd", 0.0)

        daily_remaining = daily_limit - daily_spent
        project_remaining = project_limit - project_spent
        remaining = min(daily_remaining, project_remaining)

        result = await tyche_check_budget(event, db, budget_remaining=remaining)
        if not result["allowed"]:
            return GateResult(False, result["reason"], {
                "daily_spent": round(daily_spent, 4),
                "project_spent": round(project_spent, 4),
            })
        return GateResult(True, result["reason"])

    tyche_budget_gate.__name__ = "tyche_budget_gate"
    return tyche_budget_gate


# ---------------------------------------------------------------------------
# Rate-limit gate factory — enforces provider call rates before dispatch
# ---------------------------------------------------------------------------

def make_tyche_rate_gate(limiter: ProviderRateLimiter):
    """Create a gate that enforces provider rate limits on dispatch.

    Checks limiter.try_acquire(provider) for the provider specified in
    the dispatch_command event payload. If no tokens are available, the
    gate blocks with retry_after info.

    Args:
        limiter: A ProviderRateLimiter instance (from gods.rate_limit).
    """

    async def tyche_rate_gate(event: Event, emits: list[Emit], db) -> GateResult:
        from gods.flags import is_enabled
        if not is_enabled("tyche_rate_limit", default=True):
            return GateResult(True, "Rate gate disabled by flag")

        provider = event.payload.get("provider")
        if not provider:
            return GateResult(True, "No provider in payload, skipping rate check")

        if limiter.try_acquire(provider):
            return GateResult(True, f"Rate limit OK for {provider}")

        retry_after = limiter.time_until_available(provider)
        bucket = limiter._buckets.get(provider)
        window_seconds = bucket.window_seconds if bucket else 0
        return GateResult(
            False,
            f"Rate limit exceeded for {provider}, retry after {retry_after:.1f}s",
            details={
                "retry_after": retry_after,
                "provider": provider,
                "window_seconds": window_seconds,
            },
        )

    tyche_rate_gate.__name__ = "tyche_rate_gate"
    return tyche_rate_gate


# ---------------------------------------------------------------------------
# Budget check (used by gate or directly)
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

async def tyche_retry_rate_limited(event: Event, db: Any) -> list[Emit] | None:
    """Re-queue rate-limited dispatches by setting retry_after on the task.

    Listens for gate_failed events from the rate gate. When detected,
    sets retry_after in the task's context_json so odin_dispatch will
    pick it up on the next tick after the cooldown expires, then resets
    the task status to pending so it re-enters the dispatch queue.
    """
    import json
    from gods import safe_json

    # Only handle rate-limit gate failures (rate gate puts retry_after in details)
    retry_after_delay = event.payload.get("retry_after")
    if not retry_after_delay:
        return None

    original = event.payload.get("original_payload") or {}
    task_id = original.get("task_id")
    if not task_id:
        return None

    # Compute absolute timestamp for retry
    retry_at = time.time() + max(float(retry_after_delay), 1.0)

    # Load existing context_json, merge retry_after
    ctx_row = await db.fetchone(
        "SELECT context_json FROM tasks WHERE id = $1", (task_id,)
    )
    ctx_str = (ctx_row.get("context_json") if ctx_row else None) or "{}"
    ctx = safe_json.loads_dict(ctx_str) if isinstance(ctx_str, str) else (ctx_str or {})
    ctx["retry_after"] = retry_at

    # Reset task to pending with retry_after so odin_dispatch picks it up next tick
    await db.execute_write(
        "UPDATE tasks SET status = $1, context_json = $2 WHERE id = $3",
        ("pending", json.dumps(ctx), task_id),
    )

    provider = event.payload.get("provider", "unknown")
    project_id = original.get("project_id")
    window_seconds = event.payload.get("window_seconds", 0)

    logger.info(
        "Tyche: rate-limited task %s (%s) — retry after %.1fs",
        task_id[:8], provider, float(retry_after_delay),
    )

    return [Emit("rate_limit_hit", {
        "provider": provider,
        "task_id": task_id,
        "project_id": project_id,
        "tokens_remaining": 0,
        "retry_after_seconds": round(float(retry_after_delay), 1),
        "window_seconds": window_seconds,
    }, source="tyche")]


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
