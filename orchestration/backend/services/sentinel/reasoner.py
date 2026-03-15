#  Sentinel Reasoner
#
#  LLM-powered reasoning for sentinel observations. Takes an observation,
#  plan state, recent history, and similar past observations to produce a
#  structured diagnosis with recommended action and confidence score.
#
#  Degrades gracefully: if the LLM call fails or reasoning is disabled,
#  returns None so callers fall back to rule-only logic.
#
#  Depends on: llm_router.call_llm, sentinel/models.py, sentinel/context_client.py

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.services.llm_router import call_llm
from backend.utils.json_utils import extract_json_object

if TYPE_CHECKING:
    from backend.services.sentinel.context_client import SentinelContextClient
    from backend.services.sentinel.plan_sentinel import PlanState

from backend.services.sentinel.models import SentinelObservation

logger = logging.getLogger(__name__)


@dataclass
class ReasoningResult:
    """Structured output from the sentinel reasoner."""

    diagnosis: str
    recommended_action: str
    confidence: float  # 0.0–1.0
    supporting_evidence: list[str] = field(default_factory=list)


_SYSTEM_PROMPT = """\
You are a diagnostic reasoning engine for an AI task orchestration system.
You receive a sentinel observation (an anomaly detected during plan execution),
the current plan state, recent observation history, and similar past incidents.

Analyze the situation and return a JSON object with exactly these fields:
- "diagnosis": A concise explanation of why this issue occurred (1-3 sentences).
- "recommended_action": One of: "retry_task", "release_claim", "skip_task", "reorder_wave", "no_action". Pick the most appropriate intervention.
- "confidence": A float 0.0–1.0 indicating how confident you are in your diagnosis.
- "supporting_evidence": A list of 1-4 short strings citing specific facts that support your diagnosis.

Respond with ONLY the JSON object, no markdown fences or extra text."""


def _format_observation(obs: SentinelObservation) -> str:
    """Format a single observation for the prompt."""
    parts = [
        f"Category: {obs.category}",
        f"Severity: {obs.severity.value}",
        f"Message: {obs.message}",
    ]
    if obs.task_id:
        parts.append(f"Task ID: {obs.task_id}")
    if obs.project_id:
        parts.append(f"Project ID: {obs.project_id}")
    if obs.details:
        parts.append(f"Details: {json.dumps(obs.details, default=str)}")
    return "\n".join(parts)


def _format_plan_state(state: PlanState) -> str:
    """Summarize plan state for the prompt."""
    lines = [f"Current wave: {state.current_wave}"]

    # Task status summary
    status_counts: dict[str, int] = {}
    for status in state.task_statuses.values():
        status_counts[status.value] = status_counts.get(status.value, 0) + 1
    if status_counts:
        lines.append(f"Task statuses: {json.dumps(status_counts)}")

    # Failure counts for tasks with failures
    failing = {tid: c for tid, c in state.failure_counts.items() if c > 0}
    if failing:
        lines.append(f"Failure counts: {json.dumps(failing)}")

    # Retry counts
    retried = {tid: c for tid, c in state.retry_counts.items() if c > 0}
    if retried:
        lines.append(f"Retry counts: {json.dumps(retried)}")

    # Budget
    if state.budget_limit > 0:
        pct = (state.budget_spent / state.budget_limit * 100) if state.budget_limit else 0
        lines.append(f"Budget: ${state.budget_spent:.2f} / ${state.budget_limit:.2f} ({pct:.0f}%)")

    lines.append(f"Events processed: {state.events_processed}")
    return "\n".join(lines)


def _format_similar(similar: list[dict]) -> str:
    """Format similar past observations as precedent cases."""
    if not similar:
        return "No similar past observations found."
    parts = []
    for i, node in enumerate(similar, 1):
        attrs = node.get("attributes", node)
        parts.append(
            f"  {i}. [{attrs.get('severity', '?')}] {attrs.get('category', '?')}: "
            f"{attrs.get('message', 'N/A')}"
        )
    return "Similar past observations:\n" + "\n".join(parts)


def _build_user_message(
    observation: SentinelObservation,
    state: PlanState,
    history: list[SentinelObservation],
    similar: list[dict],
) -> str:
    """Assemble the full user message for the LLM."""
    sections = [
        "== Current Observation ==",
        _format_observation(observation),
        "",
        "== Plan State ==",
        _format_plan_state(state),
    ]

    if history:
        sections.append("")
        sections.append("== Recent Observation History ==")
        for i, obs in enumerate(history[-10:], 1):  # cap at 10 most recent
            sections.append(f"  {i}. [{obs.severity.value}] {obs.category}: {obs.message}")

    sections.append("")
    sections.append("== Precedent Cases ==")
    sections.append(_format_similar(similar))

    return "\n".join(sections)


def _parse_result(text: str) -> ReasoningResult | None:
    """Parse LLM response into a ReasoningResult."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = extract_json_object(text)

    if not data or not isinstance(data, dict):
        logger.warning("Reasoner returned unparseable response: %s", text[:200])
        return None

    try:
        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))  # clamp

        evidence = data.get("supporting_evidence", [])
        if not isinstance(evidence, list):
            evidence = [str(evidence)]

        return ReasoningResult(
            diagnosis=str(data.get("diagnosis", "")),
            recommended_action=str(data.get("recommended_action", "no_action")),
            confidence=confidence,
            supporting_evidence=[str(e) for e in evidence],
        )
    except (ValueError, TypeError) as exc:
        logger.warning("Failed to construct ReasoningResult: %s", exc)
        return None


class SentinelReasoner:
    """LLM-powered reasoning engine for sentinel observations.

    Consults the LLM to diagnose anomalies and recommend interventions.
    Falls back gracefully (returns None) if disabled, the LLM is
    unavailable, or parsing fails.
    """

    def __init__(
        self,
        context_client: SentinelContextClient | None = None,
        *,
        enabled: bool = True,
        model: str | None = None,
    ) -> None:
        self._context_client = context_client
        self._enabled = enabled
        self._model = model

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    async def reason(
        self,
        observation: SentinelObservation,
        state: PlanState,
        history: list[SentinelObservation] | None = None,
    ) -> ReasoningResult | None:
        """Analyze an observation and return a structured diagnosis.

        Returns None if reasoning is disabled, the LLM call fails,
        or the response cannot be parsed — callers should fall back
        to rule-only logic.
        """
        if not self._enabled:
            logger.debug("Reasoner disabled, skipping LLM analysis")
            return None

        # Fetch similar past observations for context
        similar: list[dict] = []
        if self._context_client:
            try:
                similar = await self._context_client.search_similar_observations(
                    observation, max_results=5
                )
            except Exception:
                logger.debug("Failed to fetch similar observations, continuing without")

        user_message = _build_user_message(
            observation, state, history or [], similar
        )

        try:
            llm_kwargs: dict[str, Any] = {
                "task_type": "simple",
            }
            if self._model:
                llm_kwargs["model"] = self._model

            response = await call_llm(
                _SYSTEM_PROMPT,
                user_message,
                **llm_kwargs,
            )
        except Exception as exc:
            logger.warning("Reasoner LLM call failed: %s", exc)
            return None

        result = _parse_result(response.text)
        if result:
            logger.info(
                "Reasoner diagnosis for %s [%s]: action=%s confidence=%.2f",
                observation.category,
                observation.observation_id[:8],
                result.recommended_action,
                result.confidence,
            )
        return result
