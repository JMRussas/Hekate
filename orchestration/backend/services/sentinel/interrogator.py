#  Self-Interrogator — Structured Decision Gate
#
#  Before every significant decision, the system asks itself 6 questions.
#  If all are answered with confidence → proceed autonomously.
#  If any answer reveals a knowledge gap → escalate.
#
#  Context-aware: questions are framed differently for coding tasks
#  (breaking APIs, introducing bugs) vs non-coding tasks (wrong analysis,
#  wasted budget, misunderstood requirements).
#
#  Reusable across: sentinel interventions, verification verdicts,
#  code review verdicts, wave reassessment, plan approval.
#
#  Depends on: llm_router.call_llm, sentinel/models.py

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.services.llm_router import call_llm
from backend.services.sentinel.models import (
    InterrogationAnswer,
    InterrogationResult,
)
from backend.utils.json_utils import extract_json_object

logger = logging.getLogger(__name__)


class DecisionContext(Enum):
    """What kind of decision is being made — drives question framing."""
    SENTINEL_INTERVENTION = "sentinel_intervention"
    VERIFICATION_VERDICT = "verification_verdict"
    CODE_REVIEW_VERDICT = "code_review_verdict"
    WAVE_REASSESSMENT = "wave_reassessment"
    PLAN_APPROVAL = "plan_approval"
    TASK_DISPATCH = "task_dispatch"


# The 6 questions, with coding and non-coding framings.
# Each tuple: (question_id, base_question, coding_elaboration, non_coding_elaboration)
_QUESTIONS = [
    (
        "goal",
        "What am I trying to achieve?",
        "Trace this action back to the original requirement or task goal. "
        "Does this code change directly serve that goal, or has the scope drifted?",
        "Trace this action back to the original requirement or project goal. "
        "Does this decision directly serve that goal, or has the scope drifted?",
    ),
    (
        "consequences",
        "What are the consequences?",
        "What are the short-term and long-term consequences of this code change? "
        "Could it break existing APIs, introduce regressions, or create tech debt? "
        "Is this change reversible (e.g., can it be reverted cleanly)?",
        "What are the short-term and long-term consequences of this decision? "
        "Does it commit resources, close off options, or create dependencies? "
        "Is this decision reversible?",
    ),
    (
        "architecture",
        "What does the architecture say?",
        "Does this code follow existing conventions, patterns, and naming? "
        "Does it respect module boundaries, dependency directions, and the "
        "project's established architecture? Are there style or structural "
        "violations?",
        "Does this decision align with the project's established approach, "
        "methodology, and constraints? Does it respect existing boundaries "
        "and conventions?",
    ),
    (
        "precedent",
        "Have I seen this before?",
        "Has a similar code change or fix been attempted before? What was the "
        "outcome? Are there past decisions, failed attempts, or known gotchas "
        "that are relevant here?",
        "Has a similar decision been made before in this project? What was the "
        "outcome? Are there past incidents, failed approaches, or lessons "
        "learned that apply?",
    ),
    (
        "blind_spots",
        "What am I not seeing?",
        "What assumptions am I making? Are there edge cases, error paths, "
        "concurrency issues, or platform-specific behaviors I haven't "
        "considered? What context might I be missing?",
        "What assumptions am I making? What context might I be missing? "
        "Are there stakeholder concerns, timeline pressures, or dependencies "
        "I haven't considered?",
    ),
    (
        "blast_radius",
        "If I'm wrong, what's the blast radius?",
        "If this code change is wrong, what breaks? How many files, tests, "
        "or downstream consumers are affected? Can it be undone quickly, or "
        "does it cascade?",
        "If this decision is wrong, what's the impact? How much time, budget, "
        "or progress is lost? Can it be undone, or does it cascade into "
        "dependent work?",
    ),
]


_SYSTEM_PROMPT = """\
You are a decision-quality gate for an AI task orchestration system.

Before the system takes an action, you must evaluate it by answering 6 \
structured self-interrogation questions. Your role is to surface knowledge \
gaps and prevent the system from acting on incomplete understanding.

For each question, respond with:
- "answer": Your honest assessment (1-3 sentences)
- "confident": true if you can answer with reasonable certainty, \
false if there's a genuine knowledge gap or critical uncertainty

The system will ONLY proceed autonomously if ALL 6 questions are answered \
with confidence. A single "confident: false" triggers escalation to a human.

This is NOT a rubber stamp. Your job is to catch the cases where the system \
is about to act without sufficient understanding. Be genuinely critical.

Respond with ONLY a JSON object containing:
{
  "answers": [
    {"question_id": "goal", "answer": "...", "confident": true/false},
    {"question_id": "consequences", "answer": "...", "confident": true/false},
    {"question_id": "architecture", "answer": "...", "confident": true/false},
    {"question_id": "precedent", "answer": "...", "confident": true/false},
    {"question_id": "blind_spots", "answer": "...", "confident": true/false},
    {"question_id": "blast_radius", "answer": "...", "confident": true/false}
  ],
  "overall_assessment": "Brief summary of whether to proceed or escalate"
}

No markdown fences or extra text."""


@dataclass
class InterrogationInput:
    """Everything the interrogator needs to evaluate a decision."""
    decision_context: DecisionContext
    proposed_action: str  # what the system wants to do
    reasoning: str  # why it wants to do it (from reasoner, verifier, etc.)
    is_coding_task: bool  # drives question framing
    # Optional context enrichment
    task_description: str = ""
    task_output: str = ""
    error_text: str = ""
    project_summary: str = ""
    decision_history: str = ""  # past decisions for precedent
    world_state: str = ""  # current system state


def _build_user_message(inp: InterrogationInput) -> str:
    """Build the user message with all context and the 6 questions."""
    sections = [
        f"== Decision Context: {inp.decision_context.value} ==",
        f"Proposed action: {inp.proposed_action}",
        f"Reasoning: {inp.reasoning}",
    ]

    if inp.task_description:
        sections.append(f"\n== Task Description ==\n{inp.task_description[:2000]}")
    if inp.task_output:
        sections.append(f"\n== Task Output ==\n{inp.task_output[:3000]}")
    if inp.error_text:
        sections.append(f"\n== Error / File Context ==\n{inp.error_text[:1000]}")
    if inp.project_summary:
        sections.append(f"\n== Project Summary ==\n{inp.project_summary[:1000]}")
    if inp.decision_history:
        sections.append(f"\n== Relevant Decision History ==\n{inp.decision_history[:1500]}")
    if inp.world_state:
        sections.append(f"\n== Current State ==\n{inp.world_state[:2000]}")

    sections.append("\n== Questions to Answer ==")
    for qid, base, coding_elab, non_coding_elab in _QUESTIONS:
        elab = coding_elab if inp.is_coding_task else non_coding_elab
        sections.append(f"\n[{qid}] {base}\n{elab}")

    return "\n".join(sections)


def _parse_response(text: str) -> InterrogationResult:
    """Parse the LLM response into an InterrogationResult."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        data = extract_json_object(text)

    if not data or not isinstance(data, dict):
        logger.warning("Interrogator: unparseable response: %s", text[:200])
        return InterrogationResult(
            answers=[],
            proceed=False,
            escalation_trigger="unparseable_response",
            overall_confidence=0.0,
        )

    answers: list[InterrogationAnswer] = []
    raw_answers = data.get("answers", [])

    # Map question IDs to full question text
    qid_to_text = {qid: base for qid, base, _, _ in _QUESTIONS}

    for raw in raw_answers:
        qid = raw.get("question_id", "")
        question_text = qid_to_text.get(qid, qid)
        answers.append(InterrogationAnswer(
            question=question_text,
            answer=str(raw.get("answer", "")),
            confident=bool(raw.get("confident", False)),
        ))

    # The core logic: proceed ONLY if ALL questions answered with confidence
    all_confident = len(answers) == 6 and all(a.confident for a in answers)

    # Find the first knowledge gap (if any)
    escalation_trigger = None
    if not all_confident:
        for a in answers:
            if not a.confident:
                escalation_trigger = a.question
                break
        if not escalation_trigger and len(answers) < 6:
            escalation_trigger = "incomplete_answers"

    # Aggregate confidence: fraction of questions answered confidently
    confident_count = sum(1 for a in answers if a.confident)
    overall_confidence = confident_count / 6.0 if answers else 0.0

    return InterrogationResult(
        answers=answers,
        proceed=all_confident,
        escalation_trigger=escalation_trigger,
        overall_confidence=overall_confidence,
    )


class SelfInterrogator:
    """Structured self-interrogation gate for autonomous decisions.

    Before the system takes any significant action, it must answer 6
    questions. If all are answered with confidence → proceed. If any
    reveals a knowledge gap → escalate.

    This is NOT a permission system. It's a reasoning quality gate.
    The escalation trigger is not "you need permission" — it's
    "you don't have enough information to act confidently."
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        model: str | None = None,
    ) -> None:
        self._enabled = enabled
        self._model = model

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    async def interrogate(
        self,
        inp: InterrogationInput,
    ) -> InterrogationResult | None:
        """Run the 6-question self-interrogation on a proposed action.

        Returns InterrogationResult, or None if disabled.
        Falls back to "do not proceed" on LLM failure.
        """
        if not self._enabled:
            logger.debug("Self-interrogation disabled, skipping")
            return None

        user_message = _build_user_message(inp)

        try:
            llm_kwargs: dict[str, Any] = {"task_type": "simple"}
            if self._model:
                llm_kwargs["model"] = self._model

            response = await call_llm(
                _SYSTEM_PROMPT, user_message, **llm_kwargs
            )
        except Exception as exc:
            logger.warning("Self-interrogation LLM call failed: %s", exc)
            return InterrogationResult(
                answers=[],
                proceed=False,
                escalation_trigger=f"llm_failure: {exc}",
                overall_confidence=0.0,
            )

        return _parse_response(response.text)
