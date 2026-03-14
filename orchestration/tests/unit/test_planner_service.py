#  Orchestration Engine - Planner Service Tests
#
#  Tests for _extract_json_object and PlannerService.generate().
#
#  Depends on: backend/services/planner.py, backend/services/llm_router.py
#  Used by:    pytest

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from backend.exceptions import BudgetExhaustedError, NotFoundError, PlanParseError
from backend.models.enums import PlanStatus, ProjectStatus
from backend.services.llm_router import LLMResponse
from backend.services.planner import PlannerService, _extract_json_object, generate_plan


# ---------------------------------------------------------------------------
# TestExtractJsonObject
# ---------------------------------------------------------------------------

class TestExtractJsonObject:

    def test_simple_json(self):
        assert _extract_json_object('{"a": 1}') == {"a": 1}

    def test_json_embedded_in_prose(self):
        text = 'Here is the plan: {"summary": "build it"} done!'
        result = _extract_json_object(text)
        assert result == {"summary": "build it"}

    def test_nested_braces(self):
        text = '{"outer": {"inner": "val"}}'
        result = _extract_json_object(text)
        assert result == {"outer": {"inner": "val"}}

    def test_escaped_quotes(self):
        text = '{"key": "a \\"quoted\\" value"}'
        result = _extract_json_object(text)
        assert result["key"] == 'a "quoted" value'

    def test_no_json_returns_none(self):
        assert _extract_json_object("no json here") is None

    def test_malformed_json_returns_none(self):
        assert _extract_json_object('{"unclosed": ') is None

    def test_json_after_markdown_fence(self):
        text = '```json\n{"summary": "plan"}\n```'
        result = _extract_json_object(text)
        assert result == {"summary": "plan"}


# ---------------------------------------------------------------------------
# TestPlannerServiceGenerate
# ---------------------------------------------------------------------------

def _make_llm_response(text=None):
    """Build a mock LLMResponse for planning."""
    if text is None:
        text = json.dumps({
            "summary": "Test plan",
            "tasks": [{"title": "Task 1", "description": "Do it", "task_type": "code",
                        "complexity": "simple", "depends_on": [], "tools_needed": []}],
        })
    return LLMResponse(text=text, provider="test", model="test-model")


@pytest.fixture
async def planner_db(tmp_db):
    """Database with a draft project for planner tests."""
    now = time.time()
    await tmp_db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, ?, ?, 'draft', ?, ?)",
        ("proj_plan_001", "Test Project", "Build X\n\nDo Y\n\nTest Z", now, now),
    )
    return tmp_db


class TestPlannerServiceGenerate:

    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_success_stores_plan(self, mock_call_llm, planner_db):
        mock_call_llm.return_value = _make_llm_response()
        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)
        result = await svc.generate("proj_plan_001")

        assert result["plan"]["summary"] == "Test plan"
        assert result["version"] == 1

        plan_row = await planner_db.fetchone(
            "SELECT * FROM plans WHERE project_id = ?", ("proj_plan_001",)
        )
        assert plan_row is not None
        assert plan_row["status"] == PlanStatus.DRAFT

        proj = await planner_db.fetchone(
            "SELECT status FROM projects WHERE id = ?", ("proj_plan_001",)
        )
        assert proj["status"] == ProjectStatus.DRAFT

    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_requirement_numbering(self, mock_call_llm, planner_db):
        mock_call_llm.return_value = _make_llm_response()
        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)
        await svc.generate("proj_plan_001")

        # call_llm(system_prompt, user_msg, ...)
        user_msg = mock_call_llm.call_args[0][1]
        assert "[R1]" in user_msg
        assert "[R2]" in user_msg
        assert "[R3]" in user_msg

    async def test_project_not_found(self, planner_db):
        mock_budget = AsyncMock()
        svc = PlannerService(db=planner_db, budget=mock_budget)
        with pytest.raises(NotFoundError):
            await svc.generate("nonexistent")

    async def test_global_budget_exhausted_raises(self, planner_db):
        mock_budget = AsyncMock()
        mock_budget.can_spend = AsyncMock(return_value=False)

        svc = PlannerService(db=planner_db, budget=mock_budget)
        with pytest.raises(BudgetExhaustedError, match="Global budget"):
            await svc.generate("proj_plan_001")

    async def test_project_budget_exhausted_raises(self, planner_db):
        mock_budget = AsyncMock()
        mock_budget.can_spend = AsyncMock(return_value=True)
        mock_budget.can_spend_project = AsyncMock(return_value=False)

        svc = PlannerService(db=planner_db, budget=mock_budget)
        with pytest.raises(BudgetExhaustedError, match="per-project budget"):
            await svc.generate("proj_plan_001")

    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_empty_response_raises(self, mock_call_llm, planner_db):
        mock_call_llm.return_value = LLMResponse(text="", provider="test", model="test-model")
        mock_budget = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)
        with pytest.raises(PlanParseError, match="empty response"):
            await svc.generate("proj_plan_001")

        # Project reset to draft
        proj = await planner_db.fetchone(
            "SELECT status FROM projects WHERE id = ?", ("proj_plan_001",)
        )
        assert proj["status"] == ProjectStatus.DRAFT

    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_non_json_falls_back_to_extract(self, mock_call_llm, planner_db):
        """Response with prose + JSON falls back to _extract_json_object."""
        plan_json = '{"summary": "extracted", "tasks": []}'
        text = f"Here is the plan:\n{plan_json}\nHope that helps!"

        mock_call_llm.return_value = _make_llm_response(text)
        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)
        result = await svc.generate("proj_plan_001")

        assert result["plan"]["summary"] == "extracted"

    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_unparseable_raises_and_resets(self, mock_call_llm, planner_db):
        mock_call_llm.return_value = _make_llm_response("totally not json at all")
        mock_budget = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)
        with pytest.raises(PlanParseError):
            await svc.generate("proj_plan_001")

        proj = await planner_db.fetchone(
            "SELECT status FROM projects WHERE id = ?", ("proj_plan_001",)
        )
        assert proj["status"] == ProjectStatus.DRAFT

    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_supersedes_previous_draft(self, mock_call_llm, planner_db):
        mock_call_llm.return_value = _make_llm_response()
        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)

        # First plan
        result1 = await svc.generate("proj_plan_001")
        # Second plan
        result2 = await svc.generate("proj_plan_001")

        assert result2["version"] == 2

        # First plan should be superseded
        old_plan = await planner_db.fetchone(
            "SELECT status FROM plans WHERE id = ?", (result1["plan_id"],)
        )
        assert old_plan["status"] == PlanStatus.SUPERSEDED

    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_records_spend(self, mock_call_llm, planner_db):
        mock_call_llm.return_value = _make_llm_response()
        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)
        await svc.generate("proj_plan_001")

        mock_budget.record_spend.assert_awaited_once()
        call_kwargs = mock_budget.record_spend.call_args.kwargs
        assert call_kwargs["cost_usd"] == 0.0
        assert call_kwargs["purpose"] == "plan_generation"

    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_passes_provider_to_call_llm(self, mock_call_llm, planner_db):
        mock_call_llm.return_value = _make_llm_response()
        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)
        await svc.generate("proj_plan_001", provider="gemini")

        call_kwargs = mock_call_llm.call_args.kwargs
        assert call_kwargs["provider"] == "gemini"

    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_backward_compat_wrapper(self, mock_call_llm, planner_db):
        mock_call_llm.return_value = _make_llm_response()
        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        result = await generate_plan(
            "proj_plan_001", db=planner_db, budget=mock_budget,
        )
        assert result["plan"]["summary"] == "Test plan"
