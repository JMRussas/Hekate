#  Self-Interrogator — Unit Tests
#
#  Tests the 6-question structured self-interrogation gate:
#  parsing, question framing (code vs non-code), proceed/escalate logic,
#  LLM failure handling, disabled mode, and DecisionContext variants.

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.anyio

from backend.services.sentinel.interrogator import (
    DecisionContext,
    InterrogationInput,
    SelfInterrogator,
    _build_user_message,
    _parse_response,
    _QUESTIONS,
)
from backend.services.sentinel.models import (
    InterrogationAnswer,
    InterrogationResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _llm_response(text: str):
    resp = MagicMock()
    resp.text = text
    return resp


def _all_confident_response() -> str:
    """Build a response where all 6 questions are answered with confidence."""
    answers = []
    for qid, _, _, _ in _QUESTIONS:
        answers.append({
            "question_id": qid,
            "answer": f"Confident answer for {qid}",
            "confident": True,
        })
    return json.dumps({
        "answers": answers,
        "overall_assessment": "All clear, proceed.",
    })


def _one_gap_response(gap_question: str = "blind_spots") -> str:
    """Build a response where one question has a knowledge gap."""
    answers = []
    for qid, _, _, _ in _QUESTIONS:
        answers.append({
            "question_id": qid,
            "answer": f"Answer for {qid}",
            "confident": qid != gap_question,
        })
    return json.dumps({
        "answers": answers,
        "overall_assessment": f"Knowledge gap on {gap_question}.",
    })


def _make_input(**overrides) -> InterrogationInput:
    defaults = dict(
        decision_context=DecisionContext.SENTINEL_INTERVENTION,
        proposed_action="retry_task on task abc123",
        reasoning="Root cause: API timeout",
        is_coding_task=True,
    )
    defaults.update(overrides)
    return InterrogationInput(**defaults)


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

class TestParseResponse:

    def test_all_confident_proceeds(self):
        result = _parse_response(_all_confident_response())
        assert result.proceed is True
        assert result.escalation_trigger is None
        assert result.overall_confidence == 1.0
        assert len(result.answers) == 6

    def test_one_gap_blocks(self):
        result = _parse_response(_one_gap_response("blast_radius"))
        assert result.proceed is False
        assert result.escalation_trigger is not None
        assert "blast radius" in result.escalation_trigger.lower() or "wrong" in result.escalation_trigger.lower()
        assert result.overall_confidence == 5 / 6

    def test_multiple_gaps(self):
        answers = []
        for qid, _, _, _ in _QUESTIONS:
            answers.append({
                "question_id": qid,
                "answer": f"Answer for {qid}",
                "confident": qid in ("goal", "consequences"),  # only 2 confident
            })
        response = json.dumps({"answers": answers})
        result = _parse_response(response)
        assert result.proceed is False
        assert result.overall_confidence == 2 / 6

    def test_empty_answers_does_not_proceed(self):
        result = _parse_response(json.dumps({"answers": []}))
        assert result.proceed is False
        assert result.escalation_trigger == "incomplete_answers"
        assert result.overall_confidence == 0.0

    def test_missing_answers_field(self):
        result = _parse_response(json.dumps({"foo": "bar"}))
        assert result.proceed is False

    def test_garbage_input(self):
        result = _parse_response("not json at all")
        assert result.proceed is False
        assert result.escalation_trigger == "unparseable_response"

    def test_json_with_markdown_fences(self):
        wrapped = f"```json\n{_all_confident_response()}\n```"
        result = _parse_response(wrapped)
        # Should still parse via extract_json_object
        assert result.proceed is True or len(result.answers) == 6

    def test_partial_answers(self):
        """Only 4 of 6 questions answered — should not proceed."""
        answers = [
            {"question_id": qid, "answer": "ok", "confident": True}
            for qid, _, _, _ in _QUESTIONS[:4]
        ]
        result = _parse_response(json.dumps({"answers": answers}))
        assert result.proceed is False
        assert result.escalation_trigger == "incomplete_answers"


# ---------------------------------------------------------------------------
# _build_user_message — question framing
# ---------------------------------------------------------------------------

class TestBuildUserMessage:

    def test_coding_framing(self):
        inp = _make_input(is_coding_task=True)
        msg = _build_user_message(inp)
        assert "APIs" in msg or "code" in msg.lower()
        assert "regressions" in msg or "bugs" in msg

    def test_non_coding_framing(self):
        inp = _make_input(is_coding_task=False)
        msg = _build_user_message(inp)
        assert "budget" in msg or "resources" in msg.lower()
        assert "stakeholder" in msg or "timeline" in msg

    def test_all_context_included(self):
        inp = _make_input(
            task_description="Build a REST API",
            task_output="def handler(): ...",
            error_text="ConnectionError",
            project_summary="E-commerce platform",
            decision_history="Previously retried twice",
            world_state="Wave 2, 3 tasks running",
        )
        msg = _build_user_message(inp)
        assert "Build a REST API" in msg
        assert "def handler" in msg
        assert "ConnectionError" in msg
        assert "E-commerce" in msg
        assert "Previously retried" in msg
        assert "Wave 2" in msg

    def test_all_6_questions_present(self):
        inp = _make_input()
        msg = _build_user_message(inp)
        for qid, base, _, _ in _QUESTIONS:
            assert f"[{qid}]" in msg
            assert base in msg

    def test_decision_context_in_message(self):
        for ctx in DecisionContext:
            inp = _make_input(decision_context=ctx)
            msg = _build_user_message(inp)
            assert ctx.value in msg


# ---------------------------------------------------------------------------
# SelfInterrogator — Proceed / Escalate
# ---------------------------------------------------------------------------

class TestInterrogatorProceed:

    @patch("backend.services.sentinel.interrogator.call_llm")
    async def test_all_confident_returns_proceed(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_all_confident_response())
        interrogator = SelfInterrogator(enabled=True)
        result = await interrogator.interrogate(_make_input())
        assert result is not None
        assert result.proceed is True
        assert result.overall_confidence == 1.0
        mock_call_llm.assert_awaited_once()

    @patch("backend.services.sentinel.interrogator.call_llm")
    async def test_one_gap_returns_escalate(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(
            _one_gap_response("consequences")
        )
        interrogator = SelfInterrogator(enabled=True)
        result = await interrogator.interrogate(_make_input())
        assert result is not None
        assert result.proceed is False
        assert result.escalation_trigger is not None

    @patch("backend.services.sentinel.interrogator.call_llm")
    async def test_all_gaps_low_confidence(self, mock_call_llm):
        answers = [
            {"question_id": qid, "answer": "dunno", "confident": False}
            for qid, _, _, _ in _QUESTIONS
        ]
        mock_call_llm.return_value = _llm_response(json.dumps({"answers": answers}))
        interrogator = SelfInterrogator(enabled=True)
        result = await interrogator.interrogate(_make_input())
        assert result is not None
        assert result.proceed is False
        assert result.overall_confidence == 0.0


# ---------------------------------------------------------------------------
# SelfInterrogator — Disabled / Failure
# ---------------------------------------------------------------------------

class TestInterrogatorGracefulDegradation:

    @patch("backend.services.sentinel.interrogator.call_llm")
    async def test_disabled_returns_none(self, mock_call_llm):
        interrogator = SelfInterrogator(enabled=False)
        result = await interrogator.interrogate(_make_input())
        assert result is None
        mock_call_llm.assert_not_awaited()

    @patch("backend.services.sentinel.interrogator.call_llm")
    async def test_llm_failure_returns_do_not_proceed(self, mock_call_llm):
        mock_call_llm.side_effect = RuntimeError("LLM down")
        interrogator = SelfInterrogator(enabled=True)
        result = await interrogator.interrogate(_make_input())
        assert result is not None
        assert result.proceed is False
        assert "llm_failure" in result.escalation_trigger

    @patch("backend.services.sentinel.interrogator.call_llm")
    async def test_unparseable_response_does_not_proceed(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response("I refuse to answer in JSON")
        interrogator = SelfInterrogator(enabled=True)
        result = await interrogator.interrogate(_make_input())
        assert result is not None
        assert result.proceed is False

    def test_enabled_property(self):
        interrogator = SelfInterrogator(enabled=False)
        assert interrogator.enabled is False
        interrogator.enabled = True
        assert interrogator.enabled is True


# ---------------------------------------------------------------------------
# SelfInterrogator — Model override
# ---------------------------------------------------------------------------

class TestInterrogatorConfiguration:

    @patch("backend.services.sentinel.interrogator.call_llm")
    async def test_model_override_passed_to_llm(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_all_confident_response())
        interrogator = SelfInterrogator(enabled=True, model="claude-haiku-4-5-20251001")
        await interrogator.interrogate(_make_input())
        _, kwargs = mock_call_llm.call_args
        assert kwargs.get("model") == "claude-haiku-4-5-20251001"

    @patch("backend.services.sentinel.interrogator.call_llm")
    async def test_no_model_override(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_all_confident_response())
        interrogator = SelfInterrogator(enabled=True, model=None)
        await interrogator.interrogate(_make_input())
        _, kwargs = mock_call_llm.call_args
        assert "model" not in kwargs


# ---------------------------------------------------------------------------
# DecisionContext variants — ensure all contexts work
# ---------------------------------------------------------------------------

class TestDecisionContexts:

    @patch("backend.services.sentinel.interrogator.call_llm")
    async def test_each_context_type_works(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_all_confident_response())
        interrogator = SelfInterrogator(enabled=True)

        for ctx in DecisionContext:
            inp = _make_input(decision_context=ctx)
            result = await interrogator.interrogate(inp)
            assert result is not None
            assert result.proceed is True
