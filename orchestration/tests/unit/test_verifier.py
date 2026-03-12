#  Orchestration Engine - Verifier Tests
#
#  Tests for the output verification service and executor integration.
#
#  Depends on: backend/services/verifier.py, backend/services/executor.py
#  Used by:    pytest

import json
from unittest.mock import AsyncMock, MagicMock


from backend.models.enums import VerificationResult
from backend.services.verifier import verify_output


def _make_mock_client(verdict: str, notes: str = "test notes"):
    """Create a mock Anthropic client that returns a specific verification verdict."""
    response = MagicMock()
    response.content = [
        MagicMock(
            type="text",
            text=json.dumps({"verdict": verdict, "notes": notes}),
        )
    ]
    response.usage = MagicMock(input_tokens=100, output_tokens=50)

    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


class TestVerifyOutput:
    async def test_passed_verdict(self):
        client = _make_mock_client("passed", "Output looks good")
        budget = AsyncMock()
        budget.record_spend = AsyncMock()

        result = await verify_output(
            task_title="Build widget",
            task_description="Create a reusable widget component",
            output_text="Here is the widget implementation...",
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.PASSED
        assert result["notes"] == "Output looks good"
        assert result["cost_usd"] >= 0
        budget.record_spend.assert_awaited_once()

    async def test_gaps_found_verdict(self):
        client = _make_mock_client("gaps_found", "Output is just a placeholder")
        budget = AsyncMock()
        budget.record_spend = AsyncMock()

        result = await verify_output(
            task_title="Build widget",
            task_description="Create a component",
            output_text="TODO: implement this",
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.GAPS_FOUND
        assert "placeholder" in result["notes"]

    async def test_human_needed_verdict(self):
        client = _make_mock_client("human_needed", "Requirements are ambiguous")
        budget = AsyncMock()
        budget.record_spend = AsyncMock()

        result = await verify_output(
            task_title="Design API",
            task_description="Design the API",
            output_text="I'm not sure what format you want...",
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.HUMAN_NEEDED
        assert "ambiguous" in result["notes"]

    async def test_unparseable_response_escalates_to_human(self):
        """If the model returns non-JSON, verification escalates to human review."""
        response = MagicMock()
        response.content = [MagicMock(type="text", text="This isn't JSON!")]
        response.usage = MagicMock(input_tokens=50, output_tokens=20)

        client = AsyncMock()
        client.messages.create = AsyncMock(return_value=response)
        budget = AsyncMock()
        budget.record_spend = AsyncMock()

        result = await verify_output(
            task_title="Test",
            task_description="Test task",
            output_text="Some output",
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.HUMAN_NEEDED

    async def test_unknown_verdict_escalates_to_human(self):
        """Unknown verdict string from the LLM escalates to human review."""
        client = _make_mock_client("some_unknown_verdict", "whatever")
        budget = AsyncMock()
        budget.record_spend = AsyncMock()

        result = await verify_output(
            task_title="Test",
            task_description="Test task",
            output_text="Output",
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        assert result["result"] == VerificationResult.HUMAN_NEEDED

    async def test_empty_output_sent_as_empty_marker(self):
        """Empty output should be sent as '(empty)' in the prompt."""
        client = _make_mock_client("gaps_found", "Output is empty")
        budget = AsyncMock()
        budget.record_spend = AsyncMock()

        await verify_output(
            task_title="Test",
            task_description="Test task",
            output_text="",
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        # Check the prompt sent to the model includes "(empty)"
        call_args = client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "(empty)" in user_msg

    async def test_cost_recorded_to_budget(self):
        client = _make_mock_client("passed")
        budget = AsyncMock()
        budget.record_spend = AsyncMock()

        await verify_output(
            task_title="Test",
            task_description="Test",
            output_text="Output",
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
        )

        budget.record_spend.assert_awaited_once()
        call_kwargs = budget.record_spend.call_args.kwargs
        assert call_kwargs["purpose"] == "verification"
        assert call_kwargs["project_id"] == "proj1"
        assert call_kwargs["task_id"] == "task1"
