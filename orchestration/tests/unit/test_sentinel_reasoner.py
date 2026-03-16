#  Sentinel Reasoner — Unit Tests
#
#  Tests the iterative 5-Whys SentinelReasoner: quick resolution,
#  multi-step chains, 5-step cap, knowledge gap escalation, LLM failure
#  mid-chain, data source failures, disabled mode, and model override.

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.anyio

from backend.services.sentinel.models import (
    ReasoningResult,
    SentinelObservation,
    Severity,
    WhyStep,
)
from backend.services.sentinel.plan_sentinel import PlanState, TaskState
from backend.services.sentinel.reasoner import (
    CONFIDENCE_THRESHOLD,
    MAX_WHY_STEPS,
    SentinelReasoner,
    _build_step_message,
    _format_evidence,
    _format_observation,
    _format_plan_state,
    _format_why_chain,
    _gather_evidence,
    _parse_step_response,
)
from backend.services.sentinel.reasoner_context import ReasonerContext


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


def _step_json(
    status: str = "root_cause_found",
    conclusion: str = "API timeout caused the stall",
    root_cause: str | None = "External API timeout",
    confidence: float = 0.85,
    recommended_action: str = "retry_task",
    next_question: str | None = None,
    sources_to_query: list[str] | None = None,
    knowledge_gaps: list[str] | None = None,
) -> str:
    """Build a valid LLM step response JSON string."""
    return json.dumps({
        "status": status,
        "conclusion": conclusion,
        "root_cause": root_cause,
        "confidence": confidence,
        "recommended_action": recommended_action,
        "next_question": next_question,
        "sources_to_query": sources_to_query or [],
        "knowledge_gaps": knowledge_gaps or [],
    })


def _mock_context() -> AsyncMock:
    """Create a mock ReasonerContext with all methods returning empty results."""
    ctx = AsyncMock(spec=ReasonerContext)
    ctx.query_task_history = AsyncMock(return_value={"task": None, "recent_failures": []})
    ctx.query_decision_history = AsyncMock(return_value=[])
    ctx.query_resource_health = AsyncMock(return_value=[])
    ctx.query_similar_incidents = AsyncMock(return_value=[])
    return ctx


# ---------------------------------------------------------------------------
# _parse_step_response
# ---------------------------------------------------------------------------

class TestParseStepResponse:

    def test_valid_json(self):
        data = _parse_step_response(_step_json())
        assert data is not None
        assert data["status"] == "root_cause_found"
        assert data["confidence"] == 0.85

    def test_json_with_markdown_fences(self):
        wrapped = f"```json\n{_step_json()}\n```"
        data = _parse_step_response(wrapped)
        assert data is not None
        assert data["status"] == "root_cause_found"

    def test_garbage_returns_none(self):
        assert _parse_step_response("not json at all") is None

    def test_empty_string_returns_none(self):
        assert _parse_step_response("") is None

    def test_array_returns_none(self):
        assert _parse_step_response("[1,2,3]") is None


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

    def test_format_why_chain_empty(self):
        text = _format_why_chain([])
        assert "No previous" in text

    def test_format_why_chain_with_steps(self):
        steps = [
            WhyStep(
                question="Why is the task stuck?",
                sources_queried=["task_history"],
                evidence_found="Task has 3 retries",
                conclusion="External API is flaky",
            ),
            WhyStep(
                question="Why is the API flaky?",
                sources_queried=["resource_health"],
                evidence_found="API latency 5000ms",
                conclusion="Upstream degradation",
            ),
        ]
        text = _format_why_chain(steps)
        assert "Step 1" in text
        assert "Step 2" in text
        assert "task_history" in text
        assert "External API is flaky" in text

    def test_format_evidence_empty(self):
        text = _format_evidence({})
        assert "No evidence" in text

    def test_format_evidence_with_data(self):
        evidence = {
            "task_history": {"task": {"id": "t1", "status": "failed"}, "recent_failures": []},
            "resource_health": [{"id": "api", "status": "degraded"}],
        }
        text = _format_evidence(evidence)
        assert "[task_history]" in text
        assert "[resource_health]" in text

    def test_build_step_message_all_sections(self):
        obs = _make_obs()
        state = _make_state()
        msg = _build_step_message(obs, state, [], {}, 1)
        assert "Original Observation" in msg
        assert "Plan State" in msg
        assert "Investigation Progress" in msg
        assert "Evidence Gathered" in msg


# ---------------------------------------------------------------------------
# _gather_evidence
# ---------------------------------------------------------------------------

class TestGatherEvidence:

    async def test_gathers_requested_sources(self):
        ctx = _mock_context()
        ctx.query_task_history.return_value = {"task": {"id": "t1"}, "recent_failures": []}
        ctx.query_resource_health.return_value = [{"id": "api", "status": "healthy"}]

        obs = _make_obs()
        evidence = await _gather_evidence(ctx, obs, ["task_history", "resource_health"])

        assert "task_history" in evidence
        assert "resource_health" in evidence
        ctx.query_task_history.assert_awaited_once()
        ctx.query_resource_health.assert_awaited_once()

    async def test_skips_unknown_sources(self):
        ctx = _mock_context()
        obs = _make_obs()
        evidence = await _gather_evidence(ctx, obs, ["nonexistent_source"])
        assert evidence == {}

    async def test_skips_task_history_without_project_id(self):
        ctx = _mock_context()
        obs = _make_obs(project_id=None)
        evidence = await _gather_evidence(ctx, obs, ["task_history"])
        assert "task_history" not in evidence
        ctx.query_task_history.assert_not_awaited()

    async def test_source_failure_returns_partial(self):
        ctx = _mock_context()
        ctx.query_task_history.side_effect = ConnectionError("db down")
        ctx.query_resource_health.return_value = [{"id": "api", "status": "healthy"}]

        obs = _make_obs()
        evidence = await _gather_evidence(ctx, obs, ["task_history", "resource_health"])

        # task_history failed, but resource_health still returned
        assert "task_history" not in evidence
        assert "resource_health" in evidence


# ---------------------------------------------------------------------------
# SentinelReasoner — Quick Resolution (1-2 steps)
# ---------------------------------------------------------------------------

class TestReasonerQuickResolution:

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_single_step_root_cause(self, mock_call_llm):
        """LLM identifies root cause on the first step."""
        mock_call_llm.return_value = _llm_response(_step_json(
            status="root_cause_found",
            conclusion="API timeout caused the stall",
            root_cause="External API timeout",
            confidence=0.9,
            recommended_action="retry_task",
        ))

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert len(result.why_chain) == 1
        assert result.root_cause == "External API timeout"
        assert result.confidence == 0.9
        assert result.recommended_action == "retry_task"
        assert result.escalation_reason is None
        mock_call_llm.assert_awaited_once()

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_two_step_resolution(self, mock_call_llm):
        """LLM asks one follow-up then finds root cause."""
        mock_call_llm.side_effect = [
            _llm_response(_step_json(
                status="continue",
                conclusion="Task is retrying but keeps failing",
                next_question="Why does the API keep timing out?",
                sources_to_query=["resource_health"],
                confidence=0.4,
            )),
            _llm_response(_step_json(
                status="root_cause_found",
                conclusion="Upstream service is degraded",
                root_cause="Upstream service degraded since 14:00 UTC",
                confidence=0.88,
                recommended_action="release_claim",
            )),
        ]

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert len(result.why_chain) == 2
        assert result.root_cause == "Upstream service degraded since 14:00 UTC"
        assert result.confidence == 0.88
        assert result.recommended_action == "release_claim"
        assert result.escalation_reason is None
        assert mock_call_llm.await_count == 2


# ---------------------------------------------------------------------------
# SentinelReasoner — 5-Step Cap
# ---------------------------------------------------------------------------

class TestReasonerStepCap:

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_exhausts_all_five_steps(self, mock_call_llm):
        """If LLM keeps saying 'continue', chain stops at MAX_WHY_STEPS."""
        continue_response = _llm_response(_step_json(
            status="continue",
            conclusion="Still investigating",
            next_question="Why deeper?",
            sources_to_query=["similar_incidents"],
            confidence=0.3,
        ))
        mock_call_llm.return_value = continue_response

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert len(result.why_chain) == MAX_WHY_STEPS
        assert result.recommended_action == "escalate"
        assert result.escalation_reason is not None
        assert "exhausted" in result.escalation_reason.lower() or str(MAX_WHY_STEPS) in result.escalation_reason
        assert result.confidence == 0.3  # low confidence terminal result
        assert mock_call_llm.await_count == MAX_WHY_STEPS

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_knowledge_gaps_accumulated_across_steps(self, mock_call_llm):
        """Knowledge gaps from all steps are collected in the final result."""
        mock_call_llm.side_effect = [
            _llm_response(_step_json(
                status="continue",
                conclusion="Partial info",
                next_question="Why?",
                sources_to_query=["task_history"],
                knowledge_gaps=["Missing deployment logs"],
                confidence=0.3,
            )),
            _llm_response(_step_json(
                status="root_cause_found",
                conclusion="Found it",
                root_cause="Config drift",
                confidence=0.8,
                recommended_action="retry_task",
                knowledge_gaps=["No rollback info available"],
            )),
        ]

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert "Missing deployment logs" in result.knowledge_gaps
        assert "No rollback info available" in result.knowledge_gaps


# ---------------------------------------------------------------------------
# SentinelReasoner — Knowledge Gap Escalation
# ---------------------------------------------------------------------------

class TestReasonerKnowledgeGap:

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_knowledge_gap_escalates(self, mock_call_llm):
        """LLM reports knowledge_gap → result has escalation_reason."""
        mock_call_llm.return_value = _llm_response(_step_json(
            status="knowledge_gap",
            conclusion="Cannot determine cause — conflicting evidence",
            confidence=0.2,
            knowledge_gaps=["No task logs", "Conflicting health reports"],
        ))

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert result.root_cause == "unknown"
        assert result.recommended_action == "escalate"
        assert result.escalation_reason is not None
        assert "knowledge gap" in result.escalation_reason.lower()
        assert len(result.knowledge_gaps) == 2

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_knowledge_gap_after_investigation(self, mock_call_llm):
        """Two steps of investigation, then knowledge gap."""
        mock_call_llm.side_effect = [
            _llm_response(_step_json(
                status="continue",
                conclusion="Task keeps failing with different errors",
                next_question="Why are the errors inconsistent?",
                sources_to_query=["decision_history"],
                confidence=0.35,
            )),
            _llm_response(_step_json(
                status="continue",
                conclusion="Past decisions show similar pattern but no resolution",
                next_question="Is there an infrastructure issue?",
                sources_to_query=["resource_health"],
                confidence=0.3,
            )),
            _llm_response(_step_json(
                status="knowledge_gap",
                conclusion="All resources healthy — no clear root cause",
                confidence=0.15,
                knowledge_gaps=["Cannot reproduce", "No error pattern"],
            )),
        ]

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert len(result.why_chain) == 3
        assert result.recommended_action == "escalate"
        assert result.escalation_reason is not None


# ---------------------------------------------------------------------------
# SentinelReasoner — Graceful Degradation
# ---------------------------------------------------------------------------

class TestReasonerGracefulDegradation:

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_disabled_returns_none(self, mock_call_llm):
        reasoner = SentinelReasoner(enabled=False)
        result = await reasoner.reason(_make_obs(), _make_state())
        assert result is None
        mock_call_llm.assert_not_awaited()

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_llm_failure_first_step_returns_empty_chain(self, mock_call_llm):
        """LLM fails on the very first step → returns terminal result with no steps."""
        mock_call_llm.side_effect = RuntimeError("LLM unavailable")

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert len(result.why_chain) == 0
        assert result.root_cause == "unknown"
        assert result.recommended_action == "escalate"
        assert result.escalation_reason is not None

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_llm_failure_mid_chain_returns_partial(self, mock_call_llm):
        """LLM succeeds once then fails → returns partial chain."""
        mock_call_llm.side_effect = [
            _llm_response(_step_json(
                status="continue",
                conclusion="Initial finding: task has 3 retries",
                next_question="Why does it keep retrying?",
                sources_to_query=["task_history"],
                confidence=0.4,
            )),
            RuntimeError("LLM went away"),
        ]

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert len(result.why_chain) == 1
        assert result.why_chain[0].conclusion == "Initial finding: task has 3 retries"
        assert result.recommended_action == "escalate"
        assert result.confidence == 0.3  # low confidence terminal

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_llm_bad_parse_stops_chain(self, mock_call_llm):
        """LLM returns unparseable response → chain stops."""
        mock_call_llm.return_value = _llm_response("I don't know what to say")

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert len(result.why_chain) == 0
        assert result.recommended_action == "escalate"

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_no_context_still_works(self, mock_call_llm):
        """Without a ReasonerContext, evidence is empty but chain proceeds."""
        mock_call_llm.return_value = _llm_response(_step_json(
            status="root_cause_found",
            root_cause="Obvious from observation alone",
            confidence=0.7,
            recommended_action="skip_task",
        ))

        reasoner = SentinelReasoner(context=None, enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert result.root_cause == "Obvious from observation alone"
        assert result.recommended_action == "skip_task"

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_data_source_failure_continues_chain(self, mock_call_llm):
        """If a data source raises, evidence gathering degrades but chain continues."""
        ctx = _mock_context()
        ctx.query_task_history.side_effect = ConnectionError("db down")
        ctx.query_similar_incidents.side_effect = TimeoutError("store slow")

        mock_call_llm.return_value = _llm_response(_step_json(
            status="root_cause_found",
            root_cause="Inferred from observation",
            confidence=0.6,
            recommended_action="retry_task",
        ))

        reasoner = SentinelReasoner(context=ctx, enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert result.root_cause == "Inferred from observation"
        # LLM was still called despite data source failures
        mock_call_llm.assert_awaited_once()


# ---------------------------------------------------------------------------
# SentinelReasoner — Model override & configuration
# ---------------------------------------------------------------------------

class TestReasonerConfiguration:

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_model_override_passed_to_llm(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_step_json())

        reasoner = SentinelReasoner(
            context=_mock_context(),
            enabled=True,
            model="claude-haiku-4-5-20251001",
        )
        await reasoner.reason(_make_obs(), _make_state())

        _, kwargs = mock_call_llm.call_args
        assert kwargs.get("model") == "claude-haiku-4-5-20251001"

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_no_model_override(self, mock_call_llm):
        mock_call_llm.return_value = _llm_response(_step_json())

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True, model=None)
        await reasoner.reason(_make_obs(), _make_state())

        _, kwargs = mock_call_llm.call_args
        assert "model" not in kwargs

    def test_enabled_property(self):
        reasoner = SentinelReasoner(enabled=False)
        assert reasoner.enabled is False
        reasoner.enabled = True
        assert reasoner.enabled is True

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_custom_confidence_threshold(self, mock_call_llm):
        """Confidence threshold is stored (used by callers, not the reasoner directly)."""
        reasoner = SentinelReasoner(
            context=_mock_context(),
            enabled=True,
            confidence_threshold=0.9,
        )
        assert reasoner._confidence_threshold == 0.9


# ---------------------------------------------------------------------------
# SentinelReasoner — Evidence and context wiring
# ---------------------------------------------------------------------------

class TestReasonerContextWiring:

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_initial_sources_include_similar_incidents(self, mock_call_llm):
        """First step always queries similar_incidents."""
        ctx = _mock_context()
        mock_call_llm.return_value = _llm_response(_step_json())

        reasoner = SentinelReasoner(context=ctx, enabled=True)
        await reasoner.reason(_make_obs(), _make_state())

        ctx.query_similar_incidents.assert_awaited_once()

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_initial_sources_include_task_history_when_project_id(self, mock_call_llm):
        """First step queries task_history when observation has project_id."""
        ctx = _mock_context()
        mock_call_llm.return_value = _llm_response(_step_json())

        reasoner = SentinelReasoner(context=ctx, enabled=True)
        await reasoner.reason(_make_obs(project_id="proj-001"), _make_state())

        ctx.query_task_history.assert_awaited_once()

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_resource_health_queried_for_health_category(self, mock_call_llm):
        """health_state_change observations query resource_health on first step."""
        ctx = _mock_context()
        mock_call_llm.return_value = _llm_response(_step_json())

        reasoner = SentinelReasoner(context=ctx, enabled=True)
        await reasoner.reason(
            _make_obs(category="health_state_change"),
            _make_state(),
        )

        ctx.query_resource_health.assert_awaited_once()

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_second_step_queries_sources_from_llm(self, mock_call_llm):
        """LLM's sources_to_query in step 1 are queried in step 2."""
        ctx = _mock_context()
        mock_call_llm.side_effect = [
            _llm_response(_step_json(
                status="continue",
                conclusion="Need resource health data",
                next_question="Are services healthy?",
                sources_to_query=["resource_health"],
                confidence=0.3,
            )),
            _llm_response(_step_json(
                status="root_cause_found",
                root_cause="Service down",
                confidence=0.9,
                recommended_action="release_claim",
            )),
        ]

        reasoner = SentinelReasoner(context=ctx, enabled=True)
        await reasoner.reason(_make_obs(), _make_state())

        # resource_health should have been called at least for step 2
        assert ctx.query_resource_health.await_count >= 1


# ---------------------------------------------------------------------------
# WhyStep independence
# ---------------------------------------------------------------------------

class TestWhyStepIndependence:

    @patch("backend.services.sentinel.reasoner.call_llm")
    async def test_each_step_has_own_question_and_conclusion(self, mock_call_llm):
        """Each WhyStep in the chain is self-contained."""
        mock_call_llm.side_effect = [
            _llm_response(_step_json(
                status="continue",
                conclusion="First finding: task retried 3 times",
                next_question="Why does the retry keep failing?",
                sources_to_query=["decision_history"],
                confidence=0.4,
            )),
            _llm_response(_step_json(
                status="root_cause_found",
                conclusion="Model consistently fails on this task type",
                root_cause="Model-task mismatch",
                confidence=0.85,
                recommended_action="skip_task",
            )),
        ]

        reasoner = SentinelReasoner(context=_mock_context(), enabled=True)
        result = await reasoner.reason(_make_obs(), _make_state())

        assert result is not None
        assert len(result.why_chain) == 2

        step1 = result.why_chain[0]
        assert "task retried 3 times" in step1.conclusion
        assert step1.question.startswith("Why")

        step2 = result.why_chain[1]
        assert "Model consistently" in step2.conclusion
        assert "retry" in step2.question.lower()
