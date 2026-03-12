#  Orchestration Engine - Knowledge Injection Tests
#
#  Unit tests for project knowledge injection in claude_agent.py
#
#  Depends on: conftest.py fixtures
#  Used by:    CI pipeline

import hashlib
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import create_test_project


def _make_task_row(project_id="proj1", task_id="task1"):
    """Create a minimal task row dict for run_claude_task."""
    return {
        "id": task_id,
        "project_id": project_id,
        "model_tier": "sonnet",
        "system_prompt": "You are a test executor.",
        "context_json": "[]",
        "tools_json": "[]",
        "description": "Do the test task",
        "max_tokens": 4096,
    }


def _make_mock_client(output_text="Task completed successfully."):
    """Create a mock Anthropic client that returns a simple text response."""
    response = MagicMock()
    response.content = [MagicMock(type="text", text=output_text)]
    response.usage = MagicMock(input_tokens=100, output_tokens=50)
    response.stop_reason = "end_turn"

    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


async def _seed_knowledge(db, project_id, entries):
    """Insert knowledge entries directly into the DB."""
    now = time.time()
    for i, entry in enumerate(entries):
        content = entry["content"]
        category = entry.get("category", "discovery")
        content_hash = hashlib.sha256(content.lower().encode()).hexdigest()[:32]
        await db.execute_write(
            "INSERT INTO project_knowledge "
            "(id, project_id, task_id, category, content, content_hash, "
            "source_task_title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (f"k{i}", project_id, None, category, content,
             content_hash, f"Source Task {i}", now - i),
        )


class TestKnowledgeInjection:
    """Tests for project knowledge injection into the system prompt."""

    @pytest.mark.asyncio
    async def test_knowledge_injected_into_system_prompt(self, tmp_db):
        """Knowledge entries appear in the system prompt sent to Claude."""
        from backend.services.claude_agent import run_claude_task

        await create_test_project(tmp_db, "proj1")
        await _seed_knowledge(tmp_db, "proj1", [
            {"category": "constraint", "content": "API has a 100/min rate limit"},
            {"category": "gotcha", "content": "Library X breaks with Python 3.12"},
        ])

        client = _make_mock_client()
        budget = AsyncMock()
        budget.can_spend = AsyncMock(return_value=True)
        progress = AsyncMock()
        tool_registry = MagicMock()
        tool_registry.get_many = MagicMock(return_value=[])

        await run_claude_task(
            task_row=_make_task_row(),
            client=client,
            tool_registry=tool_registry,
            budget=budget,
            progress=progress,
            db=tmp_db,
        )

        call_kwargs = client.messages.create.call_args.kwargs
        system_prompt = call_kwargs["system"]
        assert "<project_knowledge>" in system_prompt
        assert "API has a 100/min rate limit" in system_prompt
        assert "Library X breaks with Python 3.12" in system_prompt

    @pytest.mark.asyncio
    async def test_knowledge_injection_respects_max_chars(self, tmp_db):
        """Total injected knowledge is capped at KNOWLEDGE_INJECTION_MAX_CHARS."""
        from backend.services.claude_agent import run_claude_task

        await create_test_project(tmp_db, "proj1")
        # Seed many large entries that exceed the cap
        entries = [
            {"category": "discovery", "content": f"Finding {i}: " + "x" * 500}
            for i in range(20)
        ]
        await _seed_knowledge(tmp_db, "proj1", entries)

        client = _make_mock_client()
        budget = AsyncMock()
        budget.can_spend = AsyncMock(return_value=True)
        progress = AsyncMock()
        tool_registry = MagicMock()
        tool_registry.get_many = MagicMock(return_value=[])

        await run_claude_task(
            task_row=_make_task_row(),
            client=client,
            tool_registry=tool_registry,
            budget=budget,
            progress=progress,
            db=tmp_db,
        )

        call_kwargs = client.messages.create.call_args.kwargs
        system_prompt = call_kwargs["system"]

        # Extract the knowledge section
        if "<project_knowledge>" in system_prompt:
            knowledge_section = system_prompt[system_prompt.index("<project_knowledge>"):]
            # Should be capped — not all 20 entries should appear
            assert knowledge_section.count("Finding") < 20

    @pytest.mark.asyncio
    async def test_knowledge_injection_empty_table(self, tmp_db):
        """No knowledge rows means system prompt has no [project_knowledge] section."""
        from backend.services.claude_agent import run_claude_task

        await create_test_project(tmp_db, "proj1")

        client = _make_mock_client()
        budget = AsyncMock()
        budget.can_spend = AsyncMock(return_value=True)
        progress = AsyncMock()
        tool_registry = MagicMock()
        tool_registry.get_many = MagicMock(return_value=[])

        await run_claude_task(
            task_row=_make_task_row(),
            client=client,
            tool_registry=tool_registry,
            budget=budget,
            progress=progress,
            db=tmp_db,
        )

        call_kwargs = client.messages.create.call_args.kwargs
        system_prompt = call_kwargs["system"]
        assert "<project_knowledge>" not in system_prompt

    @pytest.mark.asyncio
    async def test_knowledge_injection_db_error_ignored(self, tmp_db):
        """DB error during knowledge injection doesn't prevent task execution."""
        from backend.services.claude_agent import run_claude_task

        # Use a mock DB that fails on fetchall for knowledge query
        mock_db = AsyncMock()
        mock_db.fetchall = AsyncMock(side_effect=RuntimeError("DB exploded"))

        client = _make_mock_client()
        budget = AsyncMock()
        budget.can_spend = AsyncMock(return_value=True)
        progress = AsyncMock()
        tool_registry = MagicMock()
        tool_registry.get_many = MagicMock(return_value=[])

        # Should not raise — DB error is caught and ignored
        result = await run_claude_task(
            task_row=_make_task_row(),
            client=client,
            tool_registry=tool_registry,
            budget=budget,
            progress=progress,
            db=mock_db,
        )

        assert "output" in result
        client.messages.create.assert_awaited()

    @pytest.mark.asyncio
    async def test_self_report_instruction_in_prompt(self, tmp_db):
        """System prompt includes the self-report instruction for findings."""
        from backend.services.claude_agent import run_claude_task

        await create_test_project(tmp_db, "proj1")

        client = _make_mock_client()
        budget = AsyncMock()
        budget.can_spend = AsyncMock(return_value=True)
        progress = AsyncMock()
        tool_registry = MagicMock()
        tool_registry.get_many = MagicMock(return_value=[])

        await run_claude_task(
            task_row=_make_task_row(),
            client=client,
            tool_registry=tool_registry,
            budget=budget,
            progress=progress,
            db=tmp_db,
        )

        call_kwargs = client.messages.create.call_args.kwargs
        system_prompt = call_kwargs["system"]
        assert "constraints" in system_prompt.lower()
        assert "gotchas" in system_prompt.lower()
        assert "preserved" in system_prompt.lower()
