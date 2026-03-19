"""Tests for gods/review.py — plan self-review.

RED PHASE: These tests define the contract for review_plan().
They should FAIL until review.py is properly implemented.
"""

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from gods.review import review_plan, review_plan_with_threshold, PlanReview


# ---------------------------------------------------------------------------
# Mock LLM — simulates call_llm responses
# ---------------------------------------------------------------------------

def _make_llm_mock(response_json: dict) -> AsyncMock:
    """Create a mock call_llm that returns structured JSON."""
    @dataclass
    class FakeResponse:
        text: str
        provider: str = "test"
        model: str = "test-model"

    mock = AsyncMock(return_value=FakeResponse(text=json.dumps(response_json)))
    return mock


# ---------------------------------------------------------------------------
# review_plan — core function
# ---------------------------------------------------------------------------

class TestReviewPlan:
    @pytest.mark.asyncio
    async def test_returns_plan_review(self):
        mock_llm = _make_llm_mock({
            "has_gaps": False,
            "confidence": 0.9,
            "gaps": [],
            "suggestions": [],
            "overall_assessment": "Plan looks solid.",
        })

        review = await review_plan(
            plan={"summary": "test", "tasks": [{"title": "t1"}]},
            requirements="Build a thing",
            call_llm=mock_llm,
        )

        assert isinstance(review, PlanReview)
        assert review.has_gaps is False
        assert review.confidence == 0.9
        assert len(review.gaps) == 0

    @pytest.mark.asyncio
    async def test_detects_gaps(self):
        mock_llm = _make_llm_mock({
            "has_gaps": True,
            "confidence": 0.4,
            "gaps": ["No error handling tasks", "Missing database migration"],
            "suggestions": ["Add error handling task", "Add migration task"],
            "overall_assessment": "Plan has significant gaps.",
        })

        review = await review_plan(
            plan={"summary": "test", "tasks": []},
            requirements="Build auth system",
            call_llm=mock_llm,
        )

        assert review.has_gaps is True
        assert review.confidence == 0.4
        assert len(review.gaps) == 2
        assert "error handling" in review.gaps[0].lower()

    @pytest.mark.asyncio
    async def test_feedback_formatted_for_planner(self):
        mock_llm = _make_llm_mock({
            "has_gaps": True,
            "confidence": 0.5,
            "gaps": ["Missing tests"],
            "suggestions": ["Add test task"],
            "overall_assessment": "Needs tests.",
        })

        review = await review_plan(
            plan={"tasks": []},
            requirements="Build X",
            call_llm=mock_llm,
        )

        # Feedback should be formatted text usable as planner comment
        assert "Missing tests" in review.feedback
        assert "Add test task" in review.feedback
        assert len(review.feedback) > 0

    @pytest.mark.asyncio
    async def test_passes_provider_to_llm(self):
        mock_llm = _make_llm_mock({"has_gaps": False, "confidence": 0.9, "gaps": [], "suggestions": []})

        await review_plan(
            plan={"tasks": []},
            requirements="X",
            call_llm=mock_llm,
            provider="gemini",
            model="flash",
        )

        # Verify provider was passed through
        call_kwargs = mock_llm.call_args
        assert call_kwargs.kwargs.get("provider") == "gemini"
        assert call_kwargs.kwargs.get("model") == "flash"

    @pytest.mark.asyncio
    async def test_malformed_json_response(self):
        """LLM returns non-JSON — should not crash."""
        @dataclass
        class FakeResponse:
            text: str = "This is not JSON at all. The plan looks fine to me."
            provider: str = "test"
            model: str = "test"

        mock_llm = AsyncMock(return_value=FakeResponse())

        review = await review_plan(
            plan={"tasks": []},
            requirements="X",
            call_llm=mock_llm,
        )

        # Should return a permissive result, not crash
        assert isinstance(review, PlanReview)
        assert review.confidence <= 0.5  # low confidence on parse failure

    @pytest.mark.asyncio
    async def test_empty_response(self):
        @dataclass
        class FakeResponse:
            text: str = ""
            provider: str = "test"
            model: str = "test"

        mock_llm = AsyncMock(return_value=FakeResponse())

        review = await review_plan(
            plan={"tasks": []},
            requirements="X",
            call_llm=mock_llm,
        )

        assert isinstance(review, PlanReview)

    @pytest.mark.asyncio
    async def test_llm_exception_doesnt_crash(self):
        mock_llm = AsyncMock(side_effect=RuntimeError("Gateway down"))

        review = await review_plan(
            plan={"tasks": []},
            requirements="X",
            call_llm=mock_llm,
        )

        # Should return permissive result on failure
        assert isinstance(review, PlanReview)
        assert review.has_gaps is False  # don't block on review failure

    @pytest.mark.asyncio
    async def test_missing_fields_in_response(self):
        """LLM returns partial JSON — missing some expected fields."""
        mock_llm = _make_llm_mock({
            "confidence": 0.7,
            # missing: has_gaps, gaps, suggestions, overall_assessment
        })

        review = await review_plan(
            plan={"tasks": []},
            requirements="X",
            call_llm=mock_llm,
        )

        assert isinstance(review, PlanReview)
        assert review.confidence == 0.7
        # has_gaps should default based on empty gaps list
        assert review.has_gaps is False

    @pytest.mark.asyncio
    async def test_raw_response_preserved(self):
        mock_llm = _make_llm_mock({"has_gaps": False, "confidence": 0.8, "gaps": [], "suggestions": []})

        review = await review_plan(
            plan={"tasks": []},
            requirements="X",
            call_llm=mock_llm,
        )

        assert len(review.raw_response) > 0


# ---------------------------------------------------------------------------
# review_plan_with_threshold
# ---------------------------------------------------------------------------

class TestReviewWithThreshold:
    @pytest.mark.asyncio
    async def test_high_confidence_no_regenerate(self):
        mock_llm = _make_llm_mock({
            "has_gaps": False,
            "confidence": 0.9,
            "gaps": [],
            "suggestions": [],
        })

        review, should_regen = await review_plan_with_threshold(
            plan={"tasks": [{"title": "t1"}]},
            requirements="X",
            call_llm=mock_llm,
            confidence_threshold=0.7,
        )

        assert should_regen is False

    @pytest.mark.asyncio
    async def test_low_confidence_with_gaps_triggers_regenerate(self):
        mock_llm = _make_llm_mock({
            "has_gaps": True,
            "confidence": 0.3,
            "gaps": ["Missing auth"],
            "suggestions": ["Add auth task"],
        })

        review, should_regen = await review_plan_with_threshold(
            plan={"tasks": []},
            requirements="X",
            call_llm=mock_llm,
            confidence_threshold=0.7,
        )

        assert should_regen is True

    @pytest.mark.asyncio
    async def test_gaps_but_high_confidence_no_regenerate(self):
        """Reviewer found minor gaps but is confident overall."""
        mock_llm = _make_llm_mock({
            "has_gaps": True,
            "confidence": 0.85,
            "gaps": ["Minor: could add more comments"],
            "suggestions": [],
        })

        review, should_regen = await review_plan_with_threshold(
            plan={"tasks": []},
            requirements="X",
            call_llm=mock_llm,
            confidence_threshold=0.7,
        )

        assert should_regen is False  # confidence above threshold

    @pytest.mark.asyncio
    async def test_custom_threshold(self):
        mock_llm = _make_llm_mock({
            "has_gaps": True,
            "confidence": 0.6,
            "gaps": ["Some gap"],
            "suggestions": [],
        })

        _, regen_strict = await review_plan_with_threshold(
            plan={"tasks": []}, requirements="X",
            call_llm=mock_llm, confidence_threshold=0.9,
        )
        assert regen_strict is True

        _, regen_lax = await review_plan_with_threshold(
            plan={"tasks": []}, requirements="X",
            call_llm=mock_llm, confidence_threshold=0.3,
        )
        assert regen_lax is False
