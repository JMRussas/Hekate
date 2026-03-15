#  Sentinel Reasoner — Unit Tests
#
#  Tests SentinelReasoner: LLM call mocking, response parsing,
#  graceful degradation (disabled, LLM failure, bad parse),
#  similar-observation fetch, confidence scoring, and model override.

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.anyio

from backend.services.sentinel.models import SentinelObservation, Severity
from backend.services.sentinel.plan_sentinel import PlanState, TaskState
from backend.services.sentinel.reasoner import (
    ReasoningResult,
    SentinelReasoner,
    _build_user_message,
    _format_observation,
    _format_plan_state,
    _format_similar,
    _parse_result,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_obs(**overrides) -> SentinelObservation:
    defaults = dict(
        observation_id="obs-aaa111",
        category="task_stuck",
        message="Task abc12345 has had no progress for 400s",
        severity=Severity.WARNING,
        project_id="proj-001",
        task_id="task-001",
        details={"rule": "task_stuck", "elapsed_secs": 400},
    )
    defaults.update(overrides)
    return SentinelObservation(**defaults)


def _make_state(**overrides) -> PlanState:
    state = PlanState()
    state.current_wave = 1
    state.task_statuses = {"task-001": TaskState.RUNNING}
    state.failure_counts = {}
    state.retry_counts = {}
    state.events_processed = 42
    for k, v in overrides.items():
        setattr(state, k, v)
    return state


def _llm_response(text: str):
    """Create a mock LLMResponse with the given text."""
    resp = MagicMock()
    resp.text = text
    return resp


_GOOD_JSON = json.dumps({
    "diagnosis": "Task is stuck due to external API timeout",
    "recommended_action": "retry_task",
    "confidence": 0.85,
    "supporting_evidence": ["No progress for 400s", "API known flaky"],
})

_LOW_CONFIDENCE_JSON = json.dumps({
    "diagnosis": "Unclear root cause",
    "recommended_action": "no_action",
    "confidence": 0.3,
    "supporting_evidence": ["Insufficient data"],
})


# ---------------------------------------------------------------------------
# _parse_result unit tests
# ---------------------------------------------------------------------------

class TestParseResult:

    def test_valid_json(self):
        result = _parse_result(_GOOD_JSON)
        assert result is not None
        assert result.diagnosis == "Task is stuck due to external API timeout"
        assert result.recommended_action == "retry_task"
        assert result.confidence == 0.85
        assert len(result.supporting_evidence) == 2

    def test_json_with_markdown_fences(self):
        wrapped = f"```json\n{_GOOD_JSON}\n```"
        result = _parse_result(wrapped)
        assert result is not None
        assert result.recommended_action == "retry_task"

    def test_confidence_clamped_high(self):
        data = json.dumps({"diagnosis": "x", "recommended_action": "y", "confidence": 1.5})
        result = _parse_result(data)
        assert result is not None
        assert result.confidence == 1.0

    def test_confidence_clamped_low(self):
        data = json.dumps({"diagnosis": "x", "recommended_action": "y", "confidence": -0.5})
        result = _parse_result(data)
        assert result is not None
        assert result.confidence == 0.0

    def test_missing_confidence_defaults_zero(self):
        data = json.dumps({"diagnosis": "x", "recommended_action": "y"})
        result = _parse_result(data)
        assert result is not None
        assert result.confidence == 0.0

    def test_missing_action_defaults_no_action(self):
        data = json.dumps({"diagnosis": "x", "confidence": 0.5})
        result = _parse_result(data)
        assert result is not None
        assert result.recommended_action == "no_action"

    def test_evidence_not_a_list(self):
        data = json.dumps({
            "diagnosis": "x",
            "recommended_action": "y",
            "confidence": 0.5,
            "supporting_evidence": "single string",
        })
        result = _parse_result(data)
        assert result is not None
        assert result.supporting_evidence == ["single string"]

    def test_garbage_returns_none(self):
        assert _parse_result("not json at all") is None

    def test_empty_string_returns_none(self):
        assert _parse_result("") is None

    def test_array_returns_none(self):
        assert _parse_result("[1,2,3]") is None


# ---------------------------------------------------------------------------
# Prompt formatting helpers
# ---------------------------------------------------------------------------

class TestFormatHelpers:

    def test_format_observation_basic(self):
        obs = _make_obs()
        text = _format_observation(obs)
        assert "task_stuck" in text
        assert "warning" in text
        assert "Task abc12345" in text
        assert "task-001" in text

    def test_format_observation_no_task_id(self):
        obs = _make_obs(task_id=None)
        text = _format_observation(obs)
        assert "Task ID" not in text

    def test_format_plan_state(self):
        state = _make_state(budget_spent=80.0, budget_limit=100.0)
        text = _format_plan_state(state)
        assert "Current wave: 1" in text
        assert "Budget:" in text
        assert "80%" in text
        assert "Events processed: 42" in text

    def test_format_plan_state_no_budget(self):
        state = _make_state(budget_limit=0)
        text = _format_plan_state(state)
        assert "Budget" not in text

    def test_format_similar_empty(self):
        assert "No similar" in _format_similar([])

    def test_format_similar_with_nodes(self):
        nodes = [
            {"attributes": {"severity": "warning", "category": "task_stuck", "message": "old stuck"}},
            {"attributes": {"severity": "critical", "category": "cascade_failure", "message": "old cascade"}},
        ]
        text = _format_similar(nodes)
        assert "old stuck" in text
        assert "old cascade" in text
        assert "1." in text
        assert "2." in text

    def test_build_user_message_all_sections(self):
        obs = _make_obs()
        state = _make_state()
        history = [_make_obs(observation_id="hist-1", message="earlier obs")]
        similar = [{"attributes": {"severity": "info", "category": "test", "message": "past"}}]
        msg = _build_user_message(obs, state, history, similar)
        assert "Current Observation" in msg
        assert "Plan State" in msg
        assert "Recent Observation History" in msg
        assert "Precedent Cases" in msg
        assert "earlier obs" in msg
        assert "past" in msg


# ---------------------------------------------------------------------------
# SentinelReasoner.reason()
# ---------------------------------------------------------------------------

class TestSentinelReasoner:

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_reason_returns_result(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_GOOD_JSON)
        reasoner = SentinelReasoner(enabled=True)

        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert result.diagnosis == "Task is stuck due to external API timeout"
        assert result.recommended_action == "retry_task"
        assert result.confidence == 0.85
        mock_call_llm.assert_awaited_once()

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_reason_disabled_returns_none(self, mock_call_llm):
        reasoner = SentinelReasoner(enabled=False)
        result = await reasoner.reason(_make_obs(), _make_state())
        assert result is None
        mock_call_llm.assert_not_awaited()

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_reason_llm_failure_returns_none(self, mock_call_llm):
        mock_call_llm.side_effect = RuntimeError("LLM unavailable")
        reasoner = SentinelReasoner(enabled=True)

        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is None

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_reason_bad_llm_output_returns_none(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response("I don't know what to say")
        reasoner = SentinelReasoner(enabled=True)

        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is None

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_reason_with_model_override(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_GOOD_JSON)
        reasoner = SentinelReasoner(enabled=True, model="claude-haiku-4-5-20251001")

        await reasoner.reason(_make_obs(), _make_state())

        _, kwargs = mock_call_llm.call_args
        assert kwargs.get("model") == "claude-haiku-4-5-20251001"

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_reason_no_model_override(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_GOOD_JSON)
        reasoner = SentinelReasoner(enabled=True, model=None)

        await reasoner.reason(_make_obs(), _make_state())

        _, kwargs = mock_call_llm.call_args
        assert "model" not in kwargs

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_reason_with_history(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_GOOD_JSON)
        reasoner = SentinelReasoner(enabled=True)
        history = [_make_obs(observation_id=f"h-{i}") for i in range(3)]

        await reasoner.reason(_make_obs(), _make_state(), history=history)

        # Verify history was included in the prompt
        call_args = mock_call_llm.call_args
        user_msg = call_args[0][1]  # second positional arg
        assert "Recent Observation History" in user_msg

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_reason_fetches_similar_observations(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_GOOD_JSON)

        mock_ctx = AsyncMock()
        mock_ctx.search_similar_observations = AsyncMock(return_value=[
            {"attributes": {"severity": "warning", "category": "task_stuck", "message": "past stuck"}},
        ])
        reasoner = SentinelReasoner(context_client=mock_ctx, enabled=True)

        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        mock_ctx.search_similar_observations.assert_awaited_once()
        # Verify the similar observations made it into the prompt
        user_msg = mock_call_llm.call_args[0][1]
        assert "past stuck" in user_msg

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_reason_similar_search_failure_continues(self, mock_call_llm):
        """If context client fails, reasoning should still proceed."""
        mock_call_llm.return_value = _llm_response(_GOOD_JSON)

        mock_ctx = AsyncMock()
        mock_ctx.search_similar_observations = AsyncMock(
            side_effect=ConnectionError("store down")
        )
        reasoner = SentinelReasoner(context_client=mock_ctx, enabled=True)

        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None  # still returns a result
        mock_call_llm.assert_awaited_once()

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_reason_no_context_client(self, mock_call_llm):
        """Without a context client, skip similar search and still succeed."""
        mock_call_llm.return_value = _llm_response(_GOOD_JSON)
        reasoner = SentinelReasoner(context_client=None, enabled=True)

        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        user_msg = mock_call_llm.call_args[0][1]
        assert "No similar" in user_msg

    def test_enabled_property(self):
        reasoner = SentinelReasoner(enabled=False)
        assert reasoner.enabled is False
        reasoner.enabled = True
        assert reasoner.enabled is True

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_low_confidence_result(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_LOW_CONFIDENCE_JSON)
        reasoner = SentinelReasoner(enabled=True)

        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert result.confidence == 0.3
        assert result.recommended_action == "no_action"
