#  Orchestration Engine - Planner Service Tests
#
#  Tests for _extract_json_object and PlannerService.generate().
#
#  Depends on: backend/services/planner.py, backend/db/connection.py
#  Used by:    pytest

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.exceptions import BudgetExhaustedError, NotFoundError, PlanParseError
from backend.models.enums import PlanStatus, ProjectStatus
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

def _make_plan_response(plan_text=None, pt=100, ct=200):
    """Build a mock Claude response for planning."""
    if plan_text is None:
        plan_text = json.dumps({
            "summary": "Test plan",
            "tasks": [{"title": "Task 1", "description": "Do it", "task_type": "code",
                        "complexity": "simple", "depends_on": [], "tools_needed": []}],
        })
    response = MagicMock()
    response.content = [MagicMock(text=plan_text, type="text")]
    response.usage = MagicMock(input_tokens=pt, output_tokens=ct)
    return response


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

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_success_stores_plan(self, _mock_cost, planner_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_plan_response())
        mock_client.close = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)
        result = await svc.generate("proj_plan_001", client=mock_client)

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

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_requirement_numbering(self, _mock_cost, planner_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_plan_response())

        svc = PlannerService(db=planner_db, budget=mock_budget)
        await svc.generate("proj_plan_001", client=mock_client)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        user_msg = call_kwargs["messages"][0]["content"]
        assert "[R1]" in user_msg
        assert "[R2]" in user_msg
        assert "[R3]" in user_msg

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    async def test_budget_exhausted_raises(self, _mock_cost, planner_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=False)

        svc = PlannerService(db=planner_db, budget=mock_budget)
        with pytest.raises(BudgetExhaustedError):
            await svc.generate("proj_plan_001")

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    async def test_project_not_found(self, _mock_cost, planner_db):
        mock_budget = AsyncMock()
        svc = PlannerService(db=planner_db, budget=mock_budget)
        with pytest.raises(NotFoundError):
            await svc.generate("nonexistent")

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_empty_response_raises(self, _mock_cost, planner_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.release_reservation = AsyncMock()

        response = MagicMock()
        response.content = []

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=response)
        mock_client.close = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)
        with pytest.raises(PlanParseError, match="empty response"):
            await svc.generate("proj_plan_001", client=mock_client)

        # Project reset to draft
        proj = await planner_db.fetchone(
            "SELECT status FROM projects WHERE id = ?", ("proj_plan_001",)
        )
        assert proj["status"] == ProjectStatus.DRAFT
        mock_budget.release_reservation.assert_awaited_once()

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_non_json_falls_back_to_extract(self, _mock_cost, planner_db):
        """Response with prose + JSON falls back to _extract_json_object."""
        plan_json = '{"summary": "extracted", "tasks": []}'
        text = f"Here is the plan:\n{plan_json}\nHope that helps!"

        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_plan_response(text))

        svc = PlannerService(db=planner_db, budget=mock_budget)
        result = await svc.generate("proj_plan_001", client=mock_client)

        assert result["plan"]["summary"] == "extracted"

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_unparseable_raises_and_resets(self, _mock_cost, planner_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(
            return_value=_make_plan_response("totally not json at all")
        )
        mock_client.close = AsyncMock()

        svc = PlannerService(db=planner_db, budget=mock_budget)
        with pytest.raises(PlanParseError):
            await svc.generate("proj_plan_001", client=mock_client)

        proj = await planner_db.fetchone(
            "SELECT status FROM projects WHERE id = ?", ("proj_plan_001",)
        )
        assert proj["status"] == ProjectStatus.DRAFT
        mock_budget.release_reservation.assert_awaited_once()

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_supersedes_previous_draft(self, _mock_cost, planner_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_plan_response())

        svc = PlannerService(db=planner_db, budget=mock_budget)

        # First plan
        result1 = await svc.generate("proj_plan_001", client=mock_client)
        # Second plan
        result2 = await svc.generate("proj_plan_001", client=mock_client)

        assert result2["version"] == 2

        # First plan should be superseded
        old_plan = await planner_db.fetchone(
            "SELECT status FROM plans WHERE id = ?", (result1["plan_id"],)
        )
        assert old_plan["status"] == PlanStatus.SUPERSEDED

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_records_spend_and_releases(self, _mock_cost, planner_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_plan_response())

        svc = PlannerService(db=planner_db, budget=mock_budget)
        await svc.generate("proj_plan_001", client=mock_client)

        mock_budget.record_spend.assert_awaited_once()
        mock_budget.release_reservation.assert_awaited_once()

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    @patch("backend.services.planner.ANTHROPIC_API_KEY", "test-key")
    async def test_creates_own_client(self, _mock_cost, planner_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_plan_response())
        mock_client.close = AsyncMock()

        with patch("backend.services.planner.anthropic.AsyncAnthropic", return_value=mock_client):
            svc = PlannerService(db=planner_db, budget=mock_budget)
            await svc.generate("proj_plan_001")  # No client argument

        mock_client.close.assert_awaited_once()

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_backward_compat_wrapper(self, _mock_cost, planner_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_plan_response())

        result = await generate_plan(
            "proj_plan_001", db=planner_db, budget=mock_budget, client=mock_client,
        )
        assert result["plan"]["summary"] == "Test plan"
