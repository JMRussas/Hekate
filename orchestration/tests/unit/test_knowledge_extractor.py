#  Orchestration Engine - Knowledge Extractor Tests
#
#  Unit tests for backend/services/knowledge_extractor.py
#
#  Depends on: conftest.py fixtures
#  Used by:    CI pipeline

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import create_test_project, create_test_task


def _make_mock_client(findings: list[dict]):
    """Create a mock Anthropic client that returns findings JSON."""
    response = MagicMock()
    response.content = [
        MagicMock(type="text", text=json.dumps({"findings": findings}))
    ]
    response.usage = MagicMock(input_tokens=200, output_tokens=100)

    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


def _make_mock_client_raw(raw_text: str):
    """Create a mock Anthropic client that returns raw text."""
    response = MagicMock()
    response.content = [MagicMock(type="text", text=raw_text)]
    response.usage = MagicMock(input_tokens=100, output_tokens=50)

    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


class TestExtractKnowledge:
    """Tests for the extract_knowledge function."""

    @pytest.mark.asyncio
    async def test_basic_extraction(self, tmp_db):
        """Two findings extracted and persisted to DB."""
        from backend.services.knowledge_extractor import extract_knowledge

        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "task1", "proj1")

        findings = [
            {"category": "constraint", "content": "API rate limit is 100 requests per minute"},
            {"category": "gotcha", "content": "Library X does not support async mode"},
        ]
        client = _make_mock_client(findings)
        budget = AsyncMock()

        result = await extract_knowledge(
            task_title="Research API",
            task_description="Investigate the external API",
            output_text="A" * 300,
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        assert len(result) == 2
        assert result[0]["category"] == "constraint"
        assert result[1]["category"] == "gotcha"

        rows = await tmp_db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ?", ("proj1",)
        )
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_empty_findings_response(self, tmp_db):
        """Empty findings array from Haiku produces no DB rows."""
        from backend.services.knowledge_extractor import extract_knowledge

        await create_test_project(tmp_db, "proj1")
        client = _make_mock_client([])
        budget = AsyncMock()

        result = await extract_knowledge(
            task_title="Simple Task",
            task_description="Do something",
            output_text="A" * 300,
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        assert result == []
        rows = await tmp_db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ?", ("proj1",)
        )
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_short_output_skipped(self, tmp_db):
        """Output below minimum length threshold skips Haiku call entirely."""
        from backend.services.knowledge_extractor import extract_knowledge

        client = AsyncMock()
        budget = AsyncMock()

        result = await extract_knowledge(
            task_title="Quick Task",
            task_description="Do it",
            output_text="Short output",
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        assert result == []
        client.messages.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_output_skipped(self, tmp_db):
        """Empty output returns [] without calling Haiku."""
        from backend.services.knowledge_extractor import extract_knowledge

        client = AsyncMock()
        budget = AsyncMock()

        result = await extract_knowledge(
            task_title="Empty Task",
            task_description="Nothing",
            output_text="",
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        assert result == []
        client.messages.create.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_output_skipped(self, tmp_db):
        """None output returns [] without calling Haiku."""
        from backend.services.knowledge_extractor import extract_knowledge

        client = AsyncMock()
        budget = AsyncMock()

        result = await extract_knowledge(
            task_title="Null Task",
            task_description="Nothing",
            output_text=None,
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_unparseable_response(self, tmp_db):
        """Non-JSON response returns [] without raising."""
        from backend.services.knowledge_extractor import extract_knowledge

        await create_test_project(tmp_db, "proj1")
        client = _make_mock_client_raw("This is not JSON at all")
        budget = AsyncMock()

        result = await extract_knowledge(
            task_title="Bad Response",
            task_description="Test",
            output_text="A" * 300,
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_dedup_same_content(self, tmp_db):
        """Same content inserted twice for same project produces one row."""
        from backend.services.knowledge_extractor import extract_knowledge

        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "task1", "proj1")
        await create_test_task(tmp_db, "task2", "proj1")

        findings = [{"category": "gotcha", "content": "API rate limit is 100/min"}]
        budget = AsyncMock()

        await extract_knowledge(
            task_title="Task 1",
            task_description="Research",
            output_text="A" * 300,
            client=_make_mock_client(findings),
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        await extract_knowledge(
            task_title="Task 2",
            task_description="More research",
            output_text="B" * 300,
            client=_make_mock_client(findings),
            budget=budget,
            project_id="proj1",
            task_id="task2",
            db=tmp_db,
        )

        rows = await tmp_db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ?", ("proj1",)
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_dedup_case_insensitive(self, tmp_db):
        """Case differences in content are treated as duplicates."""
        from backend.services.knowledge_extractor import extract_knowledge

        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "task1", "proj1")
        await create_test_task(tmp_db, "task2", "proj1")
        budget = AsyncMock()

        await extract_knowledge(
            task_title="Task 1",
            task_description="Research",
            output_text="A" * 300,
            client=_make_mock_client([{"category": "discovery", "content": "API Rate Limit Is 100"}]),
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        await extract_knowledge(
            task_title="Task 2",
            task_description="Research",
            output_text="B" * 300,
            client=_make_mock_client([{"category": "discovery", "content": "api rate limit is 100"}]),
            budget=budget,
            project_id="proj1",
            task_id="task2",
            db=tmp_db,
        )

        rows = await tmp_db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ?", ("proj1",)
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_invalid_category_defaults_to_discovery(self, tmp_db):
        """Finding with unknown category stored as 'discovery'."""
        from backend.services.knowledge_extractor import extract_knowledge

        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "task1", "proj1")

        findings = [{"category": "banana", "content": "Something important"}]
        client = _make_mock_client(findings)
        budget = AsyncMock()

        result = await extract_knowledge(
            task_title="Task",
            task_description="Test",
            output_text="A" * 300,
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        assert len(result) == 1
        assert result[0]["category"] == "discovery"

        row = await tmp_db.fetchone(
            "SELECT category FROM project_knowledge WHERE project_id = ?", ("proj1",)
        )
        assert row["category"] == "discovery"

    @pytest.mark.asyncio
    async def test_budget_recorded(self, tmp_db):
        """Budget spend is recorded with purpose='knowledge_extraction'."""
        from backend.services.knowledge_extractor import extract_knowledge

        await create_test_project(tmp_db, "proj1")

        findings = [{"category": "discovery", "content": "Interesting finding"}]
        client = _make_mock_client(findings)
        budget = AsyncMock()

        await extract_knowledge(
            task_title="Task",
            task_description="Test",
            output_text="A" * 300,
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        budget.record_spend.assert_awaited_once()
        call_kwargs = budget.record_spend.call_args.kwargs
        assert call_kwargs["purpose"] == "knowledge_extraction"
        assert call_kwargs["project_id"] == "proj1"
        assert call_kwargs["task_id"] == "task1"
        assert call_kwargs["provider"] == "anthropic"

    @pytest.mark.asyncio
    async def test_exception_does_not_propagate(self, tmp_db):
        """Internal exception returns [] without raising."""
        from backend.services.knowledge_extractor import extract_knowledge

        client = AsyncMock()
        client.messages.create = AsyncMock(side_effect=RuntimeError("API down"))
        budget = AsyncMock()

        result = await extract_knowledge(
            task_title="Task",
            task_description="Test",
            output_text="A" * 300,
            client=client,
            budget=budget,
            project_id="proj1",
            task_id="task1",
            db=tmp_db,
        )

        assert result == []

    @pytest.mark.asyncio
    async def test_cross_project_isolation(self, tmp_db):
        """Same content in different projects produces separate rows."""
        from backend.services.knowledge_extractor import extract_knowledge

        await create_test_project(tmp_db, "projA")
        await create_test_project(tmp_db, "projB")
        await create_test_task(tmp_db, "taskA", "projA")
        await create_test_task(tmp_db, "taskB", "projB")

        findings = [{"category": "constraint", "content": "Shared finding across projects"}]
        budget = AsyncMock()

        await extract_knowledge(
            task_title="Task A",
            task_description="Research",
            output_text="A" * 300,
            client=_make_mock_client(findings),
            budget=budget,
            project_id="projA",
            task_id="taskA",
            db=tmp_db,
        )

        await extract_knowledge(
            task_title="Task B",
            task_description="Research",
            output_text="B" * 300,
            client=_make_mock_client(findings),
            budget=budget,
            project_id="projB",
            task_id="taskB",
            db=tmp_db,
        )

        rows_a = await tmp_db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ?", ("projA",)
        )
        rows_b = await tmp_db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ?", ("projB",)
        )
        assert len(rows_a) == 1
        assert len(rows_b) == 1
