#  Orchestration Engine - Code Reviewer Tests
#
#  Tests for review_code() verdict parsing and format_review_feedback() output.
#
#  Depends on: backend/services/code_reviewer.py
#  Used by:    pytest

from unittest.mock import AsyncMock, patch

import pytest

from backend.services.code_reviewer import format_review_feedback, review_code
from backend.services.llm_router import LLMResponse


def _make_llm_response(payload: str) -> LLMResponse:
    return LLMResponse(text=payload, provider="mock")


class TestReviewCodeApproved:

    @pytest.mark.asyncio
    async def test_approved_verdict(self):
        """Clean code gets approved."""
        resp = _make_llm_response('{"verdict": "approved", "issues": [], "summary": "Looks good"}')
        with patch("backend.services.code_reviewer.call_llm", new_callable=AsyncMock, return_value=resp):
            result = await review_code("Add login", "Implement login form", "def login(): pass")
        assert result["verdict"] == "approved"
        assert result["issues"] == []
        assert result["summary"] == "Looks good"

    @pytest.mark.asyncio
    async def test_approved_when_only_warnings(self):
        """changes_requested with only warnings downgrades to approved."""
        resp = _make_llm_response(
            '{"verdict": "changes_requested", "issues": [{"severity": "warning", "file": "a.py", "description": "Consider renaming"}], "summary": "Minor nits"}'
        )
        with patch("backend.services.code_reviewer.call_llm", new_callable=AsyncMock, return_value=resp):
            result = await review_code("Fix typo", "Fix typo in docs", "fixed text")
        assert result["verdict"] == "approved"
        assert len(result["issues"]) == 1


class TestReviewCodeChangesRequested:

    @pytest.mark.asyncio
    async def test_changes_requested_with_errors(self):
        """Error-severity issues keep changes_requested verdict."""
        resp = _make_llm_response(
            '{"verdict": "changes_requested", "issues": [{"severity": "error", "file": "auth.py", "description": "SQL injection"}], "summary": "Security bug"}'
        )
        with patch("backend.services.code_reviewer.call_llm", new_callable=AsyncMock, return_value=resp):
            result = await review_code("Auth endpoint", "Add auth", "code with vuln")
        assert result["verdict"] == "changes_requested"
        assert result["issues"][0]["severity"] == "error"

    @pytest.mark.asyncio
    async def test_mixed_severities_with_error_keeps_changes_requested(self):
        """Mix of error and warning issues keeps changes_requested."""
        resp = _make_llm_response(
            '{"verdict": "changes_requested", "issues": ['
            '{"severity": "error", "file": "a.py", "description": "Bug"},'
            '{"severity": "warning", "file": "b.py", "description": "Style"}'
            '], "summary": "Has a bug"}'
        )
        with patch("backend.services.code_reviewer.call_llm", new_callable=AsyncMock, return_value=resp):
            result = await review_code("Feature", "Build feature", "code")
        assert result["verdict"] == "changes_requested"
        assert len(result["issues"]) == 2


class TestReviewCodeFallbacks:

    @pytest.mark.asyncio
    async def test_llm_failure_returns_approved(self):
        """When all LLM providers fail, review is skipped (approved)."""
        with patch("backend.services.code_reviewer.call_llm", new_callable=AsyncMock, side_effect=RuntimeError("no providers")):
            result = await review_code("Task", "Desc", "output")
        assert result["verdict"] == "approved"
        assert "unavailable" in result["summary"]

    @pytest.mark.asyncio
    async def test_unparseable_response_returns_approved(self):
        """Garbage LLM output is treated as approved (skip)."""
        resp = _make_llm_response("I'm not JSON at all, just some random text.")
        with patch("backend.services.code_reviewer.call_llm", new_callable=AsyncMock, return_value=resp):
            result = await review_code("Task", "Desc", "output")
        assert result["verdict"] == "approved"
        assert "unparseable" in result["summary"]

    @pytest.mark.asyncio
    async def test_invalid_verdict_defaults_to_approved(self):
        """Unknown verdict string falls back to approved."""
        resp = _make_llm_response('{"verdict": "maybe", "issues": [], "summary": "Unsure"}')
        with patch("backend.services.code_reviewer.call_llm", new_callable=AsyncMock, return_value=resp):
            result = await review_code("Task", "Desc", "output")
        assert result["verdict"] == "approved"

    @pytest.mark.asyncio
    async def test_diff_text_preferred_over_output(self):
        """When diff_text is provided, it's sent to the LLM instead of output_text."""
        resp = _make_llm_response('{"verdict": "approved", "issues": [], "summary": "OK"}')
        mock_call = AsyncMock(return_value=resp)
        with patch("backend.services.code_reviewer.call_llm", mock_call):
            await review_code("Task", "Desc", "raw output", diff_text="--- a/f.py\n+++ b/f.py")
        user_msg = mock_call.call_args[0][1]
        assert "git diff" in user_msg
        assert "raw output" not in user_msg


class TestFormatReviewFeedback:

    def test_format_with_issues(self):
        """Issues are formatted as severity-tagged bullet points."""
        review = {
            "summary": "Found problems",
            "issues": [
                {"severity": "error", "file": "auth.py", "description": "SQL injection"},
                {"severity": "warning", "file": "utils.py", "description": "Unused import"},
            ],
        }
        output = format_review_feedback(review)
        assert "## Code Review: Found problems" in output
        assert "**[ERROR]** auth.py: SQL injection" in output
        assert "**[WARNING]** utils.py: Unused import" in output

    def test_format_no_issues(self):
        """No issues produces header only."""
        review = {"summary": "All good", "issues": []}
        output = format_review_feedback(review)
        assert "## Code Review: All good" in output
        assert "**[" not in output

    def test_format_missing_file_ref(self):
        """Issue with no file field still formats correctly."""
        review = {
            "summary": "Minor",
            "issues": [{"severity": "warning", "description": "Could be cleaner"}],
        }
        output = format_review_feedback(review)
        assert "**[WARNING]** : Could be cleaner" in output
