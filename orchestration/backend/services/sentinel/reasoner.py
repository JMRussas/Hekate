#  Sentinel Reasoner — 5-Whys Investigation Chain
#
#  Iterative LLM-powered root-cause analysis for sentinel observations.
#  Starts with a symptom, queries data sources for evidence, and asks
#  "why?" up to 5 times until it reaches a confident root cause or
#  identifies a knowledge gap.
#
#  Degrades gracefully: if the LLM call fails at any step, the chain
#  stops early and returns whatever it has so far. If reasoning is
#  disabled, returns None so callers fall back to rule-only logic.
#
#  Depends on: llm_router.call_llm, sentinel/models.py,
#              sentinel/reasoner_context.py

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from backend.services.llm_router import call_llm
from backend.utils.json_utils import extract_json_object

if TYPE_CHECKING:
    from backend.services.sentinel.plan_sentinel import PlanState
    from backend.services.sentinel.reasoner_context import ReasonerContext

from backend.services.sentinel.models import (
    ReasoningResult,
    SentinelObservation,
    WhyStep,
)

logger = logging.getLogger(__name__)

MAX_WHY_STEPS = 5
CONFIDENCE_THRESHOLD = 0.7

# Data sources the LLM can request at each step
AVAILABLE_SOURCES = [
    "task_history",
    "decision_history",
    "resource_health",
    "similar_incidents",
]

_SYSTEM_PROMPT = """\
You are a diagnostic reasoning engine for an AI task orchestration system.
You perform iterative root-cause analysis using the 5-Whys technique.

At each step you receive:
- The original observation (symptom)
- Previous investigation steps and their findings
- Evidence gathered for the current step

Based on the evidence, respond with a JSON object containing exactly these fields:

- "status": one of "root_cause_found", "continue", "knowledge_gap"
- "conclusion": what this step determined (1-2 sentences)
- "root_cause": if status is "root_cause_found", the identified root cause (otherwise null)
- "confidence": float 0.0-1.0 — how confident you are in the current conclusion
- "recommended_action": one of "retry_task", "release_claim", "skip_task", \
"reorder_wave", "escalate", "no_action"
- "next_question": if status is "continue", the next "why" question to investigate \
(otherwise null)
- "sources_to_query": if status is "continue", list of data sources to check next. \
Available: "task_history", "decision_history", "resource_health", "similar_incidents"
- "knowledge_gaps": list of things you couldn't determine or need more info about

Rules:
- If evidence clearly points to a root cause with high confidence, set status to \
"root_cause_found"
- If evidence is inconclusive but suggests a deeper cause, set status to "continue" \
and formulate the next "why" question
- If you've exhausted useful avenues or evidence conflicts, set status to "knowledge_gap"
- Each conclusion should be independently useful even if the chain stops here

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

    status_counts: dict[str, int] = {}
    for status in state.task_statuses.values():
        status_counts[status.value] = status_counts.get(status.value, 0) + 1
    if status_counts:
        lines.append(f"Task statuses: {json.dumps(status_counts)}")

    failing = {tid: c for tid, c in state.failure_counts.items() if c > 0}
    if failing:
        lines.append(f"Failure counts: {json.dumps(failing)}")

    retried = {tid: c for tid, c in state.retry_counts.items() if c > 0}
    if retried:
        lines.append(f"Retry counts: {json.dumps(retried)}")

    if state.budget_limit > 0:
        pct = (state.budget_spent / state.budget_limit * 100) if state.budget_limit else 0
        lines.append(f"Budget: ${state.budget_spent:.2f} / ${state.budget_limit:.2f} ({pct:.0f}%)")

    lines.append(f"Events processed: {state.events_processed}")
    return "\n".join(lines)


def _format_why_chain(steps: list[WhyStep]) -> str:
    """Format previous why-steps for the LLM prompt."""
    if not steps:
        return "No previous investigation steps."
    parts = []
    for i, step in enumerate(steps, 1):
        parts.append(f"Step {i}: {step.question}")
        parts.append(f"  Sources checked: {', '.join(step.sources_queried) or 'none'}")
        parts.append(f"  Evidence: {step.evidence_found or 'none'}")
        parts.append(f"  Conclusion: {step.conclusion}")
    return "\n".join(parts)


def _format_evidence(evidence: dict[str, Any]) -> str:
    """Format gathered evidence for the LLM prompt."""
    if not evidence:
        return "No evidence gathered for this step."
    parts = []
    for source, data in evidence.items():
        parts.append(f"[{source}]")
        if isinstance(data, list):
            if not data:
                parts.append("  (no results)")
            else:
                for item in data[:10]:  # cap display
                    parts.append(f"  - {json.dumps(item, default=str)[:300]}")
        elif isinstance(data, dict):
            parts.append(f"  {json.dumps(data, default=str)[:500]}")
        else:
            parts.append(f"  {str(data)[:500]}")
    return "\n".join(parts)


def _build_step_message(
    observation: SentinelObservation,
    state: PlanState,
    why_chain: list[WhyStep],
    evidence: dict[str, Any],
    step_number: int,
) -> str:
    """Build the user message for a single why-step."""
    sections = [
        "== Original Observation ==",
        _format_observation(observation),
        "",
        "== Plan State ==",
        _format_plan_state(state),
        "",
        f"== Investigation Progress (Step {step_number}/{MAX_WHY_STEPS}) ==",
        _format_why_chain(why_chain),
        "",
        "== Evidence Gathered This Step ==",
        _format_evidence(evidence),
    ]
    return "\n".join(sections)


def _parse_step_response(text: str) -> dict[str, Any] | None:
    """Parse LLM response for a single why-step."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = extract_json_object(text)

    if not data or not isinstance(data, dict):
        logger.warning("Reasoner step returned unparseable response: %s", text[:200])
        return None

    return data


async def _gather_evidence(
    ctx: ReasonerContext,
    observation: SentinelObservation,
    sources: list[str],
) -> dict[str, Any]:
    """Query requested data sources and return evidence dict."""
    evidence: dict[str, Any] = {}

    for source in sources:
        if source not in AVAILABLE_SOURCES:
            continue
        try:
            if source == "task_history" and observation.project_id:
                evidence["task_history"] = await ctx.query_task_history(
                    observation.project_id, observation.task_id
                )
            elif source == "decision_history" and observation.project_id:
                evidence["decision_history"] = await ctx.query_decision_history(
                    observation.project_id, observation.category
                )
            elif source == "resource_health":
                evidence["resource_health"] = await ctx.query_resource_health()
            elif source == "similar_incidents":
                evidence["similar_incidents"] = await ctx.query_similar_incidents(
                    observation
                )
        except Exception:
            logger.debug("Failed to gather evidence from %s", source, exc_info=True)

    return evidence


class SentinelReasoner:
    """LLM-powered 5-Whys reasoning engine for sentinel observations.

    Iteratively investigates anomalies by asking "why?", gathering
    evidence from data sources, and consulting the LLM until it
    reaches a root cause or knowledge gap.

    Falls back gracefully (returns None) if disabled, or returns
    a partial chain if the LLM fails mid-investigation.
    """

    def __init__(
        self,
        context: ReasonerContext | None = None,
        *,
        enabled: bool = True,
        model: str | None = None,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
    ) -> None:
        self._context = context
        self._enabled = enabled
        self._model = model
        self._confidence_threshold = confidence_threshold

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
        """Run the 5-Whys investigation chain on an observation.

        Returns a ReasoningResult with the full why-chain, or None
        if reasoning is disabled. Returns a partial result if the
        chain is interrupted by LLM failure.
        """
        if not self._enabled:
            logger.debug("Reasoner disabled, skipping LLM analysis")
            return None

        why_chain: list[WhyStep] = []
        knowledge_gaps: list[str] = []

        # Initial sources to query on first step
        sources_to_query = self._initial_sources(observation)
        next_question = f"Why is this happening: {observation.message}"

        for step_num in range(1, MAX_WHY_STEPS + 1):
            question = next_question

            # Gather evidence for this step
            evidence: dict[str, Any] = {}
            if self._context and sources_to_query:
                evidence = await _gather_evidence(
                    self._context, observation, sources_to_query
                )

            # Call LLM
            user_message = _build_step_message(
                observation, state, why_chain, evidence, step_num
            )

            try:
                llm_kwargs: dict[str, Any] = {"task_type": "simple"}
                if self._model:
                    llm_kwargs["model"] = self._model

                response = await call_llm(
                    _SYSTEM_PROMPT, user_message, **llm_kwargs
                )
            except Exception as exc:
                logger.warning("Reasoner LLM call failed at step %d: %s", step_num, exc)
                # Return what we have so far
                break

            parsed = _parse_step_response(response.text)
            if not parsed:
                logger.warning("Reasoner parse failed at step %d", step_num)
                break

            # Build the WhyStep
            step = WhyStep(
                question=question,
                sources_queried=list(evidence.keys()),
                evidence_found=_format_evidence(evidence)[:1000],
                conclusion=str(parsed.get("conclusion", "")),
            )
            why_chain.append(step)

            # Collect knowledge gaps from this step
            step_gaps = parsed.get("knowledge_gaps", [])
            if isinstance(step_gaps, list):
                knowledge_gaps.extend(str(g) for g in step_gaps)

            status = parsed.get("status", "continue")
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))

            if status == "root_cause_found":
                return ReasoningResult(
                    why_chain=why_chain,
                    root_cause=str(parsed.get("root_cause", "unknown")),
                    confidence=confidence,
                    recommended_action=str(parsed.get("recommended_action", "no_action")),
                    knowledge_gaps=knowledge_gaps,
                    escalation_reason=None,
                )

            if status == "knowledge_gap":
                return ReasoningResult(
                    why_chain=why_chain,
                    root_cause="unknown",
                    confidence=confidence,
                    recommended_action="escalate",
                    knowledge_gaps=knowledge_gaps,
                    escalation_reason="Investigation reached knowledge gap: "
                    + step.conclusion,
                )

            # status == "continue" — prepare next iteration
            next_question = str(
                parsed.get("next_question", f"Why: {step.conclusion}")
            )
            sources_to_query = parsed.get("sources_to_query", [])
            if not isinstance(sources_to_query, list):
                sources_to_query = []

        # Exhausted all steps or broke out due to error
        return self._build_terminal_result(why_chain, knowledge_gaps)

    def _initial_sources(self, observation: SentinelObservation) -> list[str]:
        """Determine which sources to query on the first step."""
        sources = ["similar_incidents"]
        if observation.project_id:
            sources.append("task_history")
            sources.append("decision_history")
        if observation.category in ("health_state_change", "resource_contention"):
            sources.append("resource_health")
        return sources

    def _build_terminal_result(
        self,
        why_chain: list[WhyStep],
        knowledge_gaps: list[str],
    ) -> ReasoningResult:
        """Build a result when the chain exhausts iterations or errors out."""
        if not why_chain:
            return ReasoningResult(
                why_chain=[],
                root_cause="unknown",
                confidence=0.0,
                recommended_action="escalate",
                knowledge_gaps=knowledge_gaps,
                escalation_reason="Reasoning chain produced no steps",
            )

        # Use the last step's conclusion as the best-effort root cause
        last = why_chain[-1]
        return ReasoningResult(
            why_chain=why_chain,
            root_cause=last.conclusion or "unknown",
            confidence=0.3,  # low confidence — didn't reach definitive conclusion
            recommended_action="escalate",
            knowledge_gaps=knowledge_gaps,
            escalation_reason=f"Investigation exhausted {len(why_chain)} steps "
            "without reaching a definitive root cause",
        )
