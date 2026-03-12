#  Orchestration Engine - Backend Logic Bug Fix Tests
#
#  Tests for PR 3 findings: message pruning, context forwarding race,
#  budget leak, verification feedback cap, cancel_project, review_task
#  retry limit, paragraph-based requirements, Ollama budget skip,
#  verifier truncation, and shared JSON utilities.
#
#  Depends on: conftest.py, backend services
#  Used by:    CI

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import create_test_project, create_test_task


# ---------------------------------------------------------------------------
# JSON utils (#37)
# ---------------------------------------------------------------------------

class TestJsonUtils:
    def test_extract_json_from_markdown_fences(self):
        from backend.utils.json_utils import extract_json_object
        text = '```json\n{"key": "value"}\n```'
        assert extract_json_object(text) == {"key": "value"}

    def test_extract_json_with_trailing_comma(self):
        from backend.utils.json_utils import extract_json_object
        text = '{"a": 1, "b": 2,}'
        assert extract_json_object(text) == {"a": 1, "b": 2}

    def test_extract_json_with_preamble(self):
        from backend.utils.json_utils import extract_json_object
        text = 'Here is the plan:\n\n{"summary": "test"}\n\nDone.'
        assert extract_json_object(text) == {"summary": "test"}

    def test_extract_json_returns_none_for_no_json(self):
        from backend.utils.json_utils import extract_json_object
        assert extract_json_object("no json here") is None

    def test_strip_trailing_commas(self):
        from backend.utils.json_utils import strip_trailing_commas
        assert strip_trailing_commas('[1, 2,]') == '[1, 2]'
        assert strip_trailing_commas('{"a": 1,}') == '{"a": 1}'

    def test_parse_requirements_single_lines(self):
        from backend.utils.json_utils import parse_requirements
        text = "Build auth\nAdd tests\nDeploy"
        result = parse_requirements(text)
        assert result == ["Build auth\nAdd tests\nDeploy"]

    def test_parse_requirements_paragraphs(self):
        from backend.utils.json_utils import parse_requirements
        text = "Build auth system\n\nAdd unit tests\n\nDeploy to production"
        result = parse_requirements(text)
        assert result == ["Build auth system", "Add unit tests", "Deploy to production"]

    def test_parse_requirements_mixed_whitespace(self):
        from backend.utils.json_utils import parse_requirements
        text = "First requirement\n  \n  \nSecond requirement"
        result = parse_requirements(text)
        assert result == ["First requirement", "Second requirement"]

    def test_parse_requirements_empty(self):
        from backend.utils.json_utils import parse_requirements
        assert parse_requirements("") == []
        assert parse_requirements("   ") == []

    def test_parse_requirements_multiline_blocks(self):
        from backend.utils.json_utils import parse_requirements
        text = "Build auth system\nwith OAuth support\n\nAdd tests\nfor all endpoints"
        result = parse_requirements(text)
        assert len(result) == 2
        assert "OAuth support" in result[0]
        assert "all endpoints" in result[1]


# ---------------------------------------------------------------------------
# Message pruning (#8)
# ---------------------------------------------------------------------------

class TestMessagePruning:
    @pytest.mark.asyncio
    async def test_messages_bounded_after_threshold(self):
        """Verify message list doesn't grow without bound during tool loops."""
        from backend.services.claude_agent import run_claude_task

        # Create a mock that first returns tool_use 8 times, then returns text
        call_count = 0

        def make_response(has_tool=True):
            resp = MagicMock()
            resp.usage = MagicMock(input_tokens=100, output_tokens=50)
            if has_tool:
                tool_block = MagicMock()
                tool_block.type = "tool_use"
                tool_block.name = "read_file"
                tool_block.input = {"path": "test.py"}
                tool_block.id = f"tool_{call_count}"
                resp.content = [tool_block]
            else:
                text_block = MagicMock()
                text_block.type = "text"
                text_block.text = "Final output"
                resp.content = [text_block]
            resp.stop_reason = "end_turn" if not has_tool else "tool_use"
            return resp

        async def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            # After 8 tool rounds, return text to end the loop
            if call_count >= 9:
                return make_response(has_tool=False)
            # Track message count at each round
            msg_count = len(kwargs.get("messages", []))
            # With MAX_HISTORY_ROUNDS=4 and 1 initial message,
            # max should be 1 + 4*2 = 9 messages
            assert msg_count <= 9, f"Messages grew to {msg_count}, expected <= 9"
            return make_response(has_tool=True)

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=mock_create)

        mock_tool = MagicMock()
        mock_tool.name = "read_file"
        mock_tool.execute = AsyncMock(return_value="file contents")
        mock_tool.to_claude_tool.return_value = {"name": "read_file"}

        mock_registry = MagicMock()
        mock_registry.get_many.return_value = [mock_tool]

        mock_budget = MagicMock()
        mock_budget.record_spend = AsyncMock()
        mock_budget.can_spend = AsyncMock(return_value=True)

        mock_progress = MagicMock()
        mock_progress.push_event = AsyncMock()

        task_row = {
            "id": "task1",
            "project_id": "proj1",
            "model_tier": "haiku",
            "context_json": "[]",
            "system_prompt": "You are a test.",
            "tools_json": '["read_file"]',
            "description": "Do something",
            "max_tokens": 4096,
        }

        with patch("backend.services.claude_agent.MAX_HISTORY_ROUNDS", 4):
            result = await run_claude_task(
                task_row=task_row, client=mock_client,
                tool_registry=mock_registry, budget=mock_budget,
                progress=mock_progress,
            )

        assert result["output"] == "Final output"
        assert call_count >= 2  # At least 2 rounds happened


# ---------------------------------------------------------------------------
# Context forwarding race (#9)
# ---------------------------------------------------------------------------

class TestContextForwardingRace:
    @pytest.mark.asyncio
    async def test_concurrent_forward_context_both_entries_present(self, tmp_db):
        """Two upstream tasks completing simultaneously should both inject context."""
        from backend.services.task_lifecycle import forward_context

        await create_test_project(tmp_db)

        now = time.time()
        # Create three tasks: A and B are upstream, C depends on both
        for tid in ["taskA", "taskB", "taskC"]:
            await tmp_db.execute_write(
                "INSERT INTO tasks (id, project_id, plan_id, title, description, "
                "task_type, priority, status, model_tier, context_json, wave, "
                "retry_count, max_retries, created_at, updated_at) "
                "VALUES (?, 'proj1', 'plan_proj1', ?, 'desc', 'code', 0, 'pending', "
                "'haiku', '[]', 0, 0, 5, ?, ?)",
                (tid, f"Task {tid}", now, now),
            )

        # C depends on A and B
        await tmp_db.execute_write(
            "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
            ("taskC", "taskA"),
        )
        await tmp_db.execute_write(
            "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
            ("taskC", "taskB"),
        )

        task_a = await tmp_db.fetchone("SELECT * FROM tasks WHERE id = 'taskA'")
        task_b = await tmp_db.fetchone("SELECT * FROM tasks WHERE id = 'taskB'")

        # Forward both concurrently
        await asyncio.gather(
            forward_context(completed_task=task_a, output_text="Output A", db=tmp_db),
            forward_context(completed_task=task_b, output_text="Output B", db=tmp_db),
        )

        # Verify both contexts are present
        task_c = await tmp_db.fetchone("SELECT context_json FROM tasks WHERE id = 'taskC'")
        ctx = json.loads(task_c["context_json"])
        sources = {e["source_task_id"] for e in ctx}
        assert "taskA" in sources, "Missing context from task A"
        assert "taskB" in sources, "Missing context from task B"


# ---------------------------------------------------------------------------
# Budget leak on plan parse failure (#11)
# ---------------------------------------------------------------------------

class TestBudgetLeak:
    @pytest.mark.asyncio
    async def test_budget_recorded_on_parse_failure(self, tmp_db):
        """API cost should be recorded even when plan JSON parsing fails."""
        from backend.services.planner import PlannerService

        await create_test_project(tmp_db)

        mock_budget = MagicMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.release_reservation = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        # Mock response with valid usage but unparseable text
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="This is not JSON at all", type="text")]
        mock_response.usage = MagicMock(input_tokens=500, output_tokens=300)

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_client.close = AsyncMock()

        planner = PlannerService(db=tmp_db, budget=mock_budget)

        with pytest.raises(Exception):  # PlanParseError
            await planner.generate("proj1", client=mock_client)

        # Budget should have been recorded despite parse failure
        mock_budget.record_spend.assert_called_once()
        call_kwargs = mock_budget.record_spend.call_args
        assert call_kwargs.kwargs["prompt_tokens"] == 500
        assert call_kwargs.kwargs["completion_tokens"] == 300
        assert call_kwargs.kwargs["purpose"] == "planning"


# ---------------------------------------------------------------------------
# Verification feedback cap (#17)
# ---------------------------------------------------------------------------

class TestVerificationFeedbackCap:
    @pytest.mark.asyncio
    async def test_feedback_sliding_window_keeps_latest(self, tmp_db):
        """Feedback entries should use a sliding window, not remove-all-then-add."""
        from backend.services.task_lifecycle import verify_task_output
        from backend.models.enums import VerificationResult

        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task1")

        # Pre-seed 3 feedbacks (the cap)
        existing_feedbacks = [
            {"type": "verification_feedback", "content": f"Feedback {i}"}
            for i in range(3)
        ]
        other_ctx = [{"type": "dependency_output", "content": "upstream data"}]
        ctx = other_ctx + existing_feedbacks

        await tmp_db.execute_write(
            "UPDATE tasks SET context_json = ?, retry_count = 0, max_retries = 5 WHERE id = 'task1'",
            (json.dumps(ctx),),
        )

        task_row = await tmp_db.fetchone("SELECT * FROM tasks WHERE id = 'task1'")

        mock_budget = MagicMock()
        mock_budget.record_spend = AsyncMock()
        mock_budget.can_spend = AsyncMock(return_value=True)
        mock_progress = MagicMock()
        mock_progress.push_event = AsyncMock()

        # Mock the verifier module's verify_output function
        mock_verification = {
            "result": VerificationResult.GAPS_FOUND,
            "notes": "New gap found",
            "cost_usd": 0.001,
        }

        with patch("backend.services.verifier.verify_output", new_callable=AsyncMock,
                    return_value=mock_verification):
            overridden = await verify_task_output(
                task_row=task_row, output_text="some output",
                project_id="proj1", task_id="task1",
                db=tmp_db, client=MagicMock(), budget=mock_budget,
                progress=mock_progress,
            )

        assert overridden is True

        # Check the updated context
        updated = await tmp_db.fetchone("SELECT context_json FROM tasks WHERE id = 'task1'")
        new_ctx = json.loads(updated["context_json"])
        feedbacks = [e for e in new_ctx if e.get("type") == "verification_feedback"]
        non_feedbacks = [e for e in new_ctx if e.get("type") != "verification_feedback"]

        # Should have exactly 3 feedbacks (cap), not 4
        assert len(feedbacks) == 3
        # The newest should be the one we just added
        assert "New gap" in feedbacks[-1]["content"]
        # The oldest (Feedback 0) should have been dropped
        assert "Feedback 0" not in str(feedbacks)
        # Non-feedback context should be preserved
        assert len(non_feedbacks) == 1
        assert non_feedbacks[0]["type"] == "dependency_output"


# ---------------------------------------------------------------------------
# cancel_project includes RUNNING (#18)
# ---------------------------------------------------------------------------

class TestCancelProjectRunning:
    @pytest.mark.asyncio
    async def test_cancel_cancels_running_tasks(self, authed_client, tmp_db):
        """cancel_project should cancel RUNNING tasks too."""
        # Create project
        resp = await authed_client.post("/api/projects", json={
            "name": "Test", "requirements": "test",
        })
        pid = resp.json()["id"]

        # Create a running task directly in DB
        now = time.time()
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
            (f"plan_{pid}", pid, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, max_retries, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Running Task', 'test', 'code', 0, 'running', 'haiku', 0, 0, 5, ?, ?)",
            ("running_task", pid, f"plan_{pid}", now, now),
        )

        # Cancel project
        resp = await authed_client.post(f"/api/projects/{pid}/cancel")
        assert resp.status_code == 200

        # Verify the running task was cancelled
        task = await tmp_db.fetchone("SELECT status FROM tasks WHERE id = 'running_task'")
        assert task["status"] == "cancelled"


# ---------------------------------------------------------------------------
# review_task retry respects MAX_TASK_RETRIES (#19)
# ---------------------------------------------------------------------------

class TestReviewRetryLimit:
    @pytest.mark.asyncio
    async def test_review_retry_rejects_at_max_retries(self, authed_client, tmp_db):
        """Review-retry should fail when max retries already reached."""
        # Create project
        resp = await authed_client.post("/api/projects", json={
            "name": "Test", "requirements": "test",
        })
        pid = resp.json()["id"]

        now = time.time()
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
            (f"plan_{pid}", pid, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, max_retries, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Review Task', 'test', 'code', 0, 'needs_review', 'haiku', 0, 5, 5, ?, ?)",
            ("review_task", pid, f"plan_{pid}", now, now),
        )

        resp = await authed_client.post("/api/tasks/review_task/review", json={
            "action": "retry",
            "feedback": "Try harder",
        })
        assert resp.status_code == 400
        assert "retry limit" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Paragraph-based requirement numbering (#21)
# ---------------------------------------------------------------------------

class TestParagraphRequirements:
    @pytest.mark.asyncio
    async def test_coverage_uses_paragraph_splitting(self, authed_client, tmp_db):
        """Coverage endpoint should use paragraph-based requirement splitting."""
        # Create project with multi-paragraph requirements
        resp = await authed_client.post("/api/projects", json={
            "name": "Test",
            "requirements": "Build auth system\nwith OAuth support\n\nAdd unit tests\n\nDeploy to production",
        })
        pid = resp.json()["id"]

        resp = await authed_client.get(f"/api/projects/{pid}/coverage")
        assert resp.status_code == 200
        data = resp.json()
        # Should be 3 requirements (paragraphs), not 3+ lines
        assert data["total_requirements"] == 3
        assert data["requirements"][0]["id"] == "R1"
        assert "OAuth support" in data["requirements"][0]["text"]


# ---------------------------------------------------------------------------
# Budget check skips Ollama-only projects (#23)
# ---------------------------------------------------------------------------

class TestOllamaBudgetSkip:
    @pytest.mark.asyncio
    async def test_ollama_only_project_not_paused(self, tmp_db):
        """Projects with only Ollama tasks should not be paused by budget check."""
        from backend.services.executor import Executor

        await create_test_project(tmp_db)

        # Set project to executing
        await tmp_db.execute_write(
            "UPDATE projects SET status = 'executing' WHERE id = 'proj1'",
        )

        # Create an Ollama task
        now = time.time()
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, max_retries, created_at, updated_at) "
            "VALUES (?, 'proj1', 'plan_proj1', 'Ollama Task', 'test', 'research', 0, "
            "'pending', 'ollama', 0, 0, 5, ?, ?)",
            ("ollama_task", now, now),
        )

        mock_budget = MagicMock()
        mock_budget.can_spend = AsyncMock(return_value=False)  # Budget exhausted
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.reserve_spend_project = AsyncMock(return_value=True)

        mock_progress = MagicMock()
        mock_progress.push_event = AsyncMock()

        mock_rm = MagicMock()
        mock_rm.is_available = MagicMock(return_value=True)

        executor = Executor(
            db=tmp_db, budget=mock_budget, progress=mock_progress,
            resource_monitor=mock_rm, tool_registry=MagicMock(),
        )

        await executor._tick()

        # Project should NOT have been paused
        project = await tmp_db.fetchone("SELECT status FROM projects WHERE id = 'proj1'")
        assert project["status"] != "paused", "Ollama-only project should not be paused by budget"


# ---------------------------------------------------------------------------
# Verifier output truncation (#38) and budget skip (#36)
# ---------------------------------------------------------------------------

class TestVerifierEnhancements:
    @pytest.mark.asyncio
    async def test_long_output_truncated(self):
        """Outputs longer than 8000 chars should be truncated before verification."""
        from backend.services.verifier import verify_output

        long_output = "x" * 10000

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"verdict": "passed", "notes": "ok"}', type="text")]
        mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        mock_budget = MagicMock()
        mock_budget.can_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()

        result = await verify_output(
            task_title="Test", task_description="Test",
            output_text=long_output, client=mock_client,
            budget=mock_budget, project_id="proj1", task_id="task1",
        )

        # Verify truncation happened in the message sent to Claude
        call_args = mock_client.messages.create.call_args
        user_msg = call_args.kwargs["messages"][0]["content"]
        assert "truncated" in user_msg
        assert len(user_msg) < len(long_output)
        assert result["result"].value == "passed"

    @pytest.mark.asyncio
    async def test_budget_exhausted_skips_verification(self):
        """Verification should be skipped when budget is exhausted."""
        from backend.services.verifier import verify_output
        from backend.models.enums import VerificationResult

        mock_budget = MagicMock()
        mock_budget.can_spend = AsyncMock(return_value=False)

        result = await verify_output(
            task_title="Test", task_description="Test",
            output_text="output", client=MagicMock(),
            budget=mock_budget, project_id="proj1", task_id="task1",
        )

        assert result["result"] == VerificationResult.SKIPPED
        assert result["cost_usd"] == 0.0

    @pytest.mark.asyncio
    async def test_knowledge_extractor_budget_skip(self):
        """Knowledge extraction should be skipped when budget is exhausted."""
        from backend.services.knowledge_extractor import extract_knowledge

        mock_budget = MagicMock()
        mock_budget.can_spend = AsyncMock(return_value=False)

        result = await extract_knowledge(
            task_title="Test", task_description="Test",
            output_text="x" * 500, client=MagicMock(),
            budget=mock_budget, project_id="proj1", task_id="task1",
            db=MagicMock(),
        )

        assert result == []


# ---------------------------------------------------------------------------
# Model router reset (#44)
# ---------------------------------------------------------------------------

class TestModelRouterReset:
    def test_reset_warned_models(self):
        from backend.services.model_router import _warned_models, _reset_warned_models, calculate_cost

        # Trigger a warning for unknown model
        calculate_cost("unknown-model", 100, 100)
        assert "unknown-model" in _warned_models

        _reset_warned_models()
        assert len(_warned_models) == 0
