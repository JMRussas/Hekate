"""Diagnosis + repair — responds to gate_failed events with fix strategies.

When a gate fails, the pipeline emits a gate_failed event. This module
provides handlers that diagnose the failure, pick a repair strategy,
and emit a repair event that the original handler can act on.

Repair strategies:
  - retry_with_fix: same handler, different prompt/approach
  - change_provider: switch to a different LLM provider
  - split_task: break the failing task into smaller pieces
  - escalate: route to human or smarter model with full context
  - skip: abandon this step, unblock downstream

The diagnosis uses:
  1. Pattern matching (fast, no LLM) — 30+ error signatures from dispatch.py
  2. LLM analysis (slow, expensive) — when pattern match confidence is low
  3. History awareness — avoids repeating the same failed fix

Flow:
    gate_failed
      → diagnose_handler picks it up
        → pattern match error → pick strategy
        → check history (did we already try this strategy?)
        → emit repair_command with strategy + context
      → repair_handler picks up repair_command
        → applies the fix (modify prompt, switch provider, etc.)
        → re-emits the original event with fix context
      → original handler runs again with fix applied
      → gate checks again
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from gods.pipeline import Event, Emit

logger = logging.getLogger("gods.diagnosis")


# ---------------------------------------------------------------------------
# Error patterns — imported from Odin dispatch.py
# ---------------------------------------------------------------------------

# (pattern_in_error, fix_type, root_cause_label)
ERROR_PATTERNS: list[tuple[str, str, str]] = [
    # Provider / rate limits
    ("rate limit", "change_provider", "Provider rate-limited"),
    ("quota exceeded", "change_provider", "Provider quota exhausted"),
    ("429", "change_provider", "HTTP 429 rate limit"),
    ("503", "retry_with_fix", "Provider temporarily unavailable"),
    ("timeout", "retry_with_fix", "Execution timed out"),
    ("timed out", "retry_with_fix", "Execution timed out"),

    # Auth
    ("unauthorized", "change_provider", "Authentication failure"),
    ("401", "change_provider", "HTTP 401 unauthorized"),
    ("credential", "change_provider", "Credential issue"),

    # Code quality
    ("syntax error", "retry_with_fix", "Generated code has syntax errors"),
    ("SyntaxError", "retry_with_fix", "Python syntax error"),
    ("IndentationError", "retry_with_fix", "Indentation error"),
    ("import error", "retry_with_fix", "Missing import"),
    ("ImportError", "retry_with_fix", "ImportError in generated code"),
    ("ModuleNotFoundError", "retry_with_fix", "Module not found"),
    ("NameError", "retry_with_fix", "Undefined variable/name"),
    ("TypeError", "retry_with_fix", "Type mismatch in generated code"),

    # Test failures
    ("test failed", "retry_with_fix", "Tests are failing"),
    ("AssertionError", "retry_with_fix", "Test assertion failed"),
    ("FAILED", "retry_with_fix", "Test suite failure"),

    # Git
    ("merge conflict", "retry_with_fix", "Git merge conflict"),
    ("worktree", "retry_with_fix", "Worktree setup issue"),

    # Resources
    ("CUDA out of memory", "change_provider", "GPU OOM"),
    ("OOM", "change_provider", "Out of memory"),
    ("disk space", "skip", "Disk space exhausted"),

    # Empty / hollow output
    ("empty output", "retry_with_fix", "Handler produced empty output"),
    ("whitespace-only", "retry_with_fix", "Handler produced whitespace-only output"),
    ("not parseable", "retry_with_fix", "Output couldn't be parsed"),

    # Plan quality
    ("no tasks", "retry_with_fix", "Plan has no tasks"),
    ("no phases", "retry_with_fix", "Plan missing phases"),
    ("Missing error handling", "retry_with_fix", "Plan missing error handling"),
    ("gaps", "retry_with_fix", "Plan has coverage gaps"),
]

# Provider fallback order
PROVIDER_FALLBACK = ["claude", "gemini", "ollama"]


# ---------------------------------------------------------------------------
# Diagnosis result
# ---------------------------------------------------------------------------

@dataclass
class Diagnosis:
    strategy: str  # retry_with_fix | change_provider | split_task | escalate | skip
    confidence: float
    root_cause: str
    fix_detail: dict[str, Any] = field(default_factory=dict)
    why_chain: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Diagnose function — pattern match + history check
# ---------------------------------------------------------------------------

def diagnose(
    handler_name: str,
    error_reason: str,
    attempt: int,
    max_attempts: int,
    current_provider: str | None = None,
    history: list[dict] | None = None,
) -> Diagnosis:
    """Diagnose a gate failure and recommend a repair strategy.

    Args:
        handler_name: which handler failed
        error_reason: the gate's failure reason
        attempt: which attempt this was
        max_attempts: how many attempts were allowed
        current_provider: LLM provider used (for change_provider strategy)
        history: previous repair attempts for this event
    """
    reason_lower = (error_reason or "").lower()
    why: list[str] = []
    past_strategies = {h.get("strategy") for h in (history or [])}

    # Check if we've exhausted attempts
    if attempt >= max_attempts:
        why.append(f"Attempt {attempt}/{max_attempts} — gate retries exhausted")

        # Did we already try changing provider?
        if "change_provider" in past_strategies:
            why.append("Already tried changing provider — escalating")
            return Diagnosis("escalate", 0.9, "All repair strategies exhausted",
                             why_chain=why)

        # Try changing provider as last resort before escalating
        if current_provider:
            new_provider = _pick_alternative_provider(current_provider)
            if new_provider:
                why.append(f"Trying provider change: {current_provider} → {new_provider}")
                return Diagnosis("change_provider", 0.7, "Retries exhausted, trying different provider",
                                 fix_detail={"new_provider": new_provider},
                                 why_chain=why)

        why.append("No provider alternatives — escalating to human")
        return Diagnosis("escalate", 0.9, "All strategies exhausted",
                         why_chain=why)

    # Pattern match
    for pattern, strategy, root_cause in ERROR_PATTERNS:
        if pattern.lower() in reason_lower:
            why.append(f"Error matches '{pattern}' → {strategy}: {root_cause}")

            # Don't repeat a strategy that already failed
            if strategy in past_strategies:
                why.append(f"Already tried '{strategy}' — escalating")
                next_strategy = _escalate_strategy(strategy)
                return Diagnosis(next_strategy, 0.6, root_cause,
                                 fix_detail=_build_fix_detail(next_strategy, current_provider, error_reason),
                                 why_chain=why)

            return Diagnosis(strategy, 0.8, root_cause,
                             fix_detail=_build_fix_detail(strategy, current_provider, error_reason),
                             why_chain=why)

    # No pattern match — default to retry_with_fix if first failure,
    # escalate if we've already retried
    if "retry_with_fix" in past_strategies:
        why.append("Unknown error + retry already attempted → escalating")
        return Diagnosis("escalate", 0.5, f"Unknown error: {error_reason[:100]}",
                         why_chain=why)

    why.append(f"No pattern match for: {error_reason[:80]}")
    why.append("Defaulting to retry_with_fix")
    return Diagnosis("retry_with_fix", 0.4, f"Unknown error: {error_reason[:100]}",
                     fix_detail={"guidance": f"Previous attempt failed: {error_reason}. Try a different approach."},
                     why_chain=why)


def _pick_alternative_provider(current: str) -> str | None:
    """Pick the next provider in the fallback chain."""
    try:
        idx = PROVIDER_FALLBACK.index(current)
        for alt in PROVIDER_FALLBACK[idx + 1:] + PROVIDER_FALLBACK[:idx]:
            return alt
    except ValueError:
        pass
    # Current not in fallback list — return first available
    return PROVIDER_FALLBACK[0] if PROVIDER_FALLBACK else None


def _escalate_strategy(failed_strategy: str) -> str:
    """When a strategy fails, what's the next escalation?"""
    escalation = {
        "retry_with_fix": "change_provider",
        "change_provider": "escalate",
        "split_task": "escalate",
    }
    return escalation.get(failed_strategy, "escalate")


def _build_fix_detail(strategy: str, current_provider: str | None, error: str) -> dict:
    """Build strategy-specific fix context."""
    detail: dict[str, Any] = {}

    if strategy == "change_provider" and current_provider:
        new = _pick_alternative_provider(current_provider)
        if new:
            detail["new_provider"] = new
            detail["old_provider"] = current_provider

    if strategy == "retry_with_fix":
        detail["guidance"] = _generate_prompt_guidance(error)

    if strategy == "split_task":
        detail["reason"] = "Task may be too complex for a single execution"

    return detail


def _generate_prompt_guidance(error: str) -> str:
    """Generate prompt guidance text from an error message."""
    error_lower = error.lower()

    if "syntax" in error_lower or "indent" in error_lower:
        return ("Your previous output had syntax errors. "
                "Double-check all indentation, brackets, and string literals. "
                "Use proper newline characters (\\n) not raw newlines in strings.")

    if "import" in error_lower or "module" in error_lower:
        return ("Your previous output had import errors. "
                "Verify all imports exist and are spelled correctly. "
                "Check that required packages are in requirements.txt.")

    if "test" in error_lower or "assert" in error_lower:
        return ("The tests are failing. Read the test file first to understand "
                "what's expected. Make sure your implementation matches the "
                "test's assertions exactly.")

    if "empty" in error_lower or "whitespace" in error_lower:
        return ("Your previous output was empty or whitespace-only. "
                "You must produce actual content. Show your work.")

    if "gap" in error_lower or "missing" in error_lower:
        return (f"Review feedback: {error}. "
                "Address each point specifically.")

    return f"Previous attempt failed: {error}. Try a different approach."


# ---------------------------------------------------------------------------
# Pipeline handlers
# ---------------------------------------------------------------------------

async def diagnose_handler(event: Event, db) -> list[Emit] | None:
    """Handle gate_failed events — diagnose and emit repair_command.

    Registered as: pipeline.register("gate_failed", diagnose_handler)
    """
    handler_name = event.payload.get("handler", "unknown")
    reason = event.payload.get("reason", "unknown failure")
    attempt = event.payload.get("attempt", 1)
    max_attempts = event.payload.get("max_attempts", 3)

    # Load repair history for this handler from recent events
    # (In a real system this would query the relay table. For now, use payload.)
    history = event.payload.get("_repair_history", [])
    current_provider = event.payload.get("provider")

    diag = diagnose(
        handler_name=handler_name,
        error_reason=reason,
        attempt=attempt,
        max_attempts=max_attempts,
        current_provider=current_provider,
        history=history,
    )

    logger.info(
        "[diagnosis] %s: %s (confidence=%.2f) → %s",
        handler_name, diag.root_cause, diag.confidence, diag.strategy,
    )
    for step in diag.why_chain:
        logger.debug("  why: %s", step)

    return [Emit(
        event_type="repair_command",
        payload={
            "handler": handler_name,
            "strategy": diag.strategy,
            "confidence": diag.confidence,
            "root_cause": diag.root_cause,
            "fix_detail": diag.fix_detail,
            "why_chain": diag.why_chain,
            "original_event_type": event.payload.get("event_type"),
            # Thread through the original event payload so repair can re-emit
            "original_payload": event.payload.get("original_payload", {}),
            "_repair_history": history + [{
                "strategy": diag.strategy,
                "reason": reason,
                "attempt": attempt,
            }],
        },
        source="diagnosis",
        severity="warning" if diag.strategy == "escalate" else "info",
    )]


async def escalation_handler(event: Event, db) -> list[Emit] | None:
    """Handle repair_command events where strategy is 'escalate'.

    Creates a checkpoint for human review.

    Registered as: pipeline.register("repair_command", escalation_handler,
        filter=lambda e: e.payload.get("strategy") == "escalate")
    """
    handler = event.payload.get("handler", "unknown")
    root_cause = event.payload.get("root_cause", "unknown")
    why_chain = event.payload.get("why_chain", [])

    logger.warning(
        "[escalation] %s needs human intervention: %s", handler, root_cause,
    )

    return [Emit(
        event_type="human_intervention_needed",
        payload={
            "handler": handler,
            "root_cause": root_cause,
            "why_chain": why_chain,
            "repair_history": event.payload.get("_repair_history", []),
            "original_payload": event.payload.get("original_payload", {}),
        },
        source="diagnosis",
        severity="error",
    )]
