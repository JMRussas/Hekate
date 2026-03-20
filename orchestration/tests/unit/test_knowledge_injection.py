#  Orchestration Engine - Knowledge Injection Tests
#
#  Unit tests for project knowledge injection in cli_common.py build_prompt
#  and claude_agent.py system prompt assembly.
#
#  Depends on: conftest.py fixtures
#  Used by:    CI pipeline

import hashlib
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.cli_common import build_prompt
from tests.conftest import create_test_project


async def _seed_knowledge(db, project_id, entries):
    """Insert knowledge entries directly into the DB."""
    now = time.time()
    for i, entry in enumerate(entries):
        content = entry["content"]
        category = entry.get("category", "discovery")
        rationale = entry.get("rationale", "")
        alternatives = entry.get("alternatives_considered", "")
        confidence = entry.get("confidence", "medium")
        content_hash = hashlib.sha256(content.lower().encode()).hexdigest()[:32]
        await db.execute_write(
            "INSERT INTO project_knowledge "
            "(id, project_id, task_id, category, content, content_hash, "
            "rationale, alternatives_considered, confidence, "
            "source_task_title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"k{i}", project_id, None, category, content,
             content_hash, rationale, alternatives, confidence,
             f"Source Task {i}", now - i),
        )


def _make_mock_client(output_text="Task completed successfully."):
    """Create a mock Anthropic client that returns a simple text response."""
    response = MagicMock()
    response.content = [MagicMock(type="text", text=output_text)]
    response.usage = MagicMock(input_tokens=100, output_tokens=50)
    response.stop_reason = "end_turn"

    client = AsyncMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


def _make_task_row(project_id="proj1", task_id="task1", context_json="[]"):
    """Create a minimal task row dict."""
    return {
        "id": task_id,
        "project_id": project_id,
        "model_tier": "sonnet",
        "system_prompt": "You are a test executor.",
        "context_json": context_json,
        "tools_json": "[]",
        "description": "Do the test task",
        "max_tokens": 4096,
    }


class TestBuildPromptKnowledgeInjection:
    """Tests for knowledge injection via build_prompt (cli_common.py)."""

    def test_rationale_included_in_prompt(self):
        """Rationale appears in the prompt when present."""
        context = [{
            "type": "project_knowledge",
            "content": [{
                "finding": "SQLite is the chosen DB",
                "category": "decision",
                "rationale": "No concurrent writes needed",
                "alternatives_considered": "Postgres, DuckDB",
                "confidence": "high",
            }],
        }]
        task_row = _make_task_row(context_json=json.dumps(context))

        prompt = build_prompt(task_row)

        assert "<historical_rationale>" in prompt
        assert "SQLite is the chosen DB" in prompt
        assert "<rationale>No concurrent writes needed</rationale>" in prompt
        assert "<rejected_alternatives>Postgres, DuckDB</rejected_alternatives>" in prompt
        assert 'confidence="high"' in prompt

    def test_missing_rationale_omitted(self):
        """When rationale is empty/missing, the tag is not rendered."""
        context = [{
            "type": "project_knowledge",
            "content": [{
                "finding": "API returns XML",
                "category": "discovery",
            }],
        }]
        task_row = _make_task_row(context_json=json.dumps(context))

        prompt = build_prompt(task_row)

        assert "API returns XML" in prompt
        assert "<rationale>" not in prompt
        assert "<alternatives_considered>" not in prompt
        assert "<confidence>" not in prompt

    def test_multiple_findings_with_mixed_rationale(self):
        """Mix of findings with and without rationale renders correctly."""
        context = [{
            "type": "project_knowledge",
            "content": [
                {
                    "finding": "Must use Python 3.11+",
                    "category": "constraint",
                    "rationale": "Type hint syntax requires it",
                    "confidence": "high",
                },
                {
                    "finding": "Library X breaks with 3.12",
                    "category": "gotcha",
                },
            ],
        }]
        task_row = _make_task_row(context_json=json.dumps(context))

        prompt = build_prompt(task_row)

        assert "Must use Python 3.11+" in prompt
        assert "<rationale>Type hint syntax requires it</rationale>" in prompt
        assert "Library X breaks with 3.12" in prompt
        # Second finding has no rationale — tag should appear only once
        assert prompt.count("<rationale>") == 1

    def test_old_format_string_content_fallback(self):
        """Old format with string content still works."""
        context = [{
            "type": "project_knowledge",
            "content": "- API rate limit is 100/min\n- Use WAL mode",
        }]
        task_row = _make_task_row(context_json=json.dumps(context))

        prompt = build_prompt(task_row)

        assert "<project_knowledge>" in prompt
        assert "API rate limit is 100/min" in prompt


class TestKnowledgeInjection:
    """Tests for project knowledge injection into the system prompt via claude_agent."""

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
        assert "<historical_rationale>" in system_prompt
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
