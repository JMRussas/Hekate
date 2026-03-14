#  Orchestration Engine - Verifier Tests
#
#  Tests for the output verification service. Verifier now routes through
#  call_llm (CLI/Ollama) instead of Anthropic SDK.
#
#  Depends on: backend/services/verifier.py, backend/services/llm_router.py
#  Used by:    pytest

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.models.enums import VerificationResult
from backend.services.llm_router import LLMResponse
from backend.services.verifier import verify_output


def _make_llm_response(verdict: str, notes: str = "test notes"):
    """Build a mock LLMResponse returning a verification JSON verdict."""
    text = json.dumps({"verdict": verdict, "notes": notes})
    return LLMResponse(text=text, provider="test", model="test-model")


class TestVerifyOutput:

    @patch("backend.services.verifier.call_llm", new_callable=AsyncMock)
    async def test_passed_verdict(self, mock_call_llm):
        mock_call_llm.return_value = _make_llm_response("passed", "Output looks good")
        budget = AsyncMock()

        result = await verify_output(
            task_title="Build widget",
            task_description="Create a reusable widget component",
            output_text="Here is the widget implementation...",
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.PASSED
        assert result["notes"] == "Output looks good"
        budget.record_spend.assert_awaited_once()

    @patch("backend.services.verifier.call_llm", new_callable=AsyncMock)
    async def test_gaps_found_verdict(self, mock_call_llm):
        mock_call_llm.return_value = _make_llm_response("gaps_found", "Output is just a placeholder")
        budget = AsyncMock()

        result = await verify_output(
            task_title="Build widget",
            task_description="Create a component",
            output_text="TODO: implement this",
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.GAPS_FOUND
        assert "placeholder" in result["notes"]

    @patch("backend.services.verifier.call_llm", new_callable=AsyncMock)
    async def test_human_needed_verdict(self, mock_call_llm):
        mock_call_llm.return_value = _make_llm_response("human_needed", "Requirements are ambiguous")
        budget = AsyncMock()

        result = await verify_output(
            task_title="Design API",
            task_description="Design the API",
            output_text="I'm not sure what format you want...",
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.HUMAN_NEEDED
        assert "ambiguous" in result["notes"]

    @patch("backend.services.verifier.call_llm", new_callable=AsyncMock)
    async def test_unparseable_response_escalates_to_human(self, mock_call_llm):
        """If the LLM returns non-JSON, verification escalates to human review."""
        mock_call_llm.return_value = LLMResponse(
            text="This isn't JSON!", provider="test", model="test-model",
        )
        budget = AsyncMock()

        result = await verify_output(
            task_title="Test",
            task_description="Test task",
            output_text="Some output",
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.HUMAN_NEEDED

    @patch("backend.services.verifier.call_llm", new_callable=AsyncMock)
    async def test_unknown_verdict_escalates_to_human(self, mock_call_llm):
        """Unknown verdict string from the LLM escalates to human review."""
        mock_call_llm.return_value = _make_llm_response("some_unknown_verdict", "whatever")
        budget = AsyncMock()

        result = await verify_output(
            task_title="Test",
            task_description="Test task",
            output_text="Output",
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.HUMAN_NEEDED

    @patch("backend.services.verifier.call_llm", new_callable=AsyncMock)
    async def test_empty_output_sent_as_empty_marker(self, mock_call_llm):
        """Empty output should be sent as '(empty)' in the prompt."""
        mock_call_llm.return_value = _make_llm_response("gaps_found", "Output is empty")
        budget = AsyncMock()

        await verify_output(
            task_title="Test",
            task_description="Test task",
            output_text="",
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        # Check the user message sent to call_llm includes "(empty)"
        user_msg = mock_call_llm.call_args[0][1]
        assert "(empty)" in user_msg

    @patch("backend.services.verifier.call_llm", new_callable=AsyncMock)
    async def test_cost_recorded_to_budget(self, mock_call_llm):
        mock_call_llm.return_value = _make_llm_response("passed")
        budget = AsyncMock()

        await verify_output(
            task_title="Test",
            task_description="Test",
            output_text="Output",
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        budget.record_spend.assert_awaited_once()
        call_kwargs = budget.record_spend.call_args.kwargs
        assert call_kwargs["purpose"] == "verification"
        assert call_kwargs["project_id"] == "proj1"
        assert call_kwargs["task_id"] == "task1"

    @patch("backend.services.verifier.call_llm", new_callable=AsyncMock)
    async def test_uses_simple_task_type(self, mock_call_llm):
        """Verifier should route through call_llm with task_type='simple'
        to prefer cheap providers (Gemini > Ollama > Codex)."""
        mock_call_llm.return_value = _make_llm_response("passed")
        budget = AsyncMock()

        await verify_output(
            task_title="Test",
            task_description="Test",
            output_text="Output",
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert mock_call_llm.call_args.kwargs["task_type"] == "simple"

    @patch("backend.services.verifier.call_llm", new_callable=AsyncMock)
    async def test_all_providers_fail_returns_human_needed(self, mock_call_llm):
        """When all LLM providers fail, verifier returns HUMAN_NEEDED instead of crashing."""
        mock_call_llm.side_effect = RuntimeError("All providers failed")
        budget = AsyncMock()

        result = await verify_output(
            task_title="Test",
            task_description="Test",
            output_text="Output",
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.HUMAN_NEEDED
        assert "unavailable" in result["notes"]
        assert result["cost_usd"] == 0.0

    async def test_budget_exhausted_skips_verification(self):
        """When budget is exhausted, verification is skipped (not errored)."""
        budget = AsyncMock()
        budget.can_spend = AsyncMock(return_value=False)

        result = await verify_output(
            task_title="Test",
            task_description="Test",
            output_text="Output",
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.SKIPPED

    @patch("backend.services.verifier.call_llm", new_callable=AsyncMock)
    async def test_backward_compat_client_arg_ignored(self, mock_call_llm):
        """The deprecated client arg is accepted but ignored."""
        mock_call_llm.return_value = _make_llm_response("passed")
        budget = AsyncMock()
        fake_client = object()  # Not a real Anthropic client

        result = await verify_output(
            task_title="Test",
            task_description="Test",
            output_text="Output",
            client=fake_client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.PASSED


class TestClaudeCodeExecutorAllowedTools:
    """Tests that the Claude Code executor passes --allowedTools."""

    def test_allowed_tools_in_cmd_args(self):
        """The executor must pass --allowedTools so Claude Code can write files."""
        from backend.services.claude_code_executor import CLAUDE_CODE_ALLOWED_TOOLS
        # Verify the config value exists and includes write-capable tools
        assert "Edit" in CLAUDE_CODE_ALLOWED_TOOLS
        assert "Write" in CLAUDE_CODE_ALLOWED_TOOLS
        assert "Bash" in CLAUDE_CODE_ALLOWED_TOOLS


class TestVerificationGate:
    """Tests that verification failure blocks dependent tasks."""

    async def test_verification_error_marks_needs_review(self):
        """When verify_output raises, the task should be NEEDS_REVIEW (not COMPLETED)."""
        from unittest.mock import MagicMock
        from backend.models.enums import TaskStatus
        from backend.services.task_lifecycle import verify_task_output

        task_row = MagicMock()
        task_row.__getitem__ = lambda self, key: {
            "title": "Test Task", "description": "Do something",
            "retry_count": 0, "max_retries": 3, "context_json": "[]",
        }[key]

        db = AsyncMock()
        client = AsyncMock()
        budget = AsyncMock()
        progress = AsyncMock()

        # Make verify_output raise (simulating provider auth failure).
        # verify_output is imported inside verify_task_output, so patch at source.
        with patch("backend.services.verifier.verify_output",
                   side_effect=Exception("Auth error")):
            overridden = await verify_task_output(
                task_row=task_row,
                output_text="some output",
                project_id="proj1",
                task_id="task1",
                db=db,
                client=client,
                budget=budget,
                progress=progress,
            )

        assert overridden is True

        # Verify task was set to NEEDS_REVIEW (blocking dependents)
        update_call = db.execute_write.call_args
        assert TaskStatus.NEEDS_REVIEW in update_call[0][1]

        # Verify progress event was pushed
        progress.push_event.assert_awaited_once()
