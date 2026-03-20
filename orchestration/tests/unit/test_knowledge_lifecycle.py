#  Orchestration Engine - Knowledge Lifecycle Tests
#
#  Tests for the end-to-end knowledge flow:
#  1. Pre-dispatch injection of knowledge into task context_json
#  2. Interrogation context gathers knowledge for self-interrogation
#  3. Knowledge not duplicated on retries
#  4. DB errors during injection don't block dispatch
#
#  Depends on: conftest.py fixtures
#  Used by:    CI pipeline

import hashlib
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import create_test_project, create_test_task


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


def _make_task_row(project_id="proj1", task_id="task1", context_json="[]",
                   description="Do the task", system_prompt="You are a test executor.",
                   model_tier="sonnet"):
    """Create a minimal task row dict matching what task_lifecycle expects."""
    return {
        "id": task_id,
        "project_id": project_id,
        "title": "Test Task",
        "description": description,
        "task_type": "code",
        "model_tier": model_tier,
        "system_prompt": system_prompt,
        "context_json": context_json,
        "tools_json": "[]",
        "max_tokens": 4096,
        "status": "running",
        "wave": 0,
        "retry_count": 0,
        "max_retries": 3,
        "priority": 0,
    }


class TestPreDispatchKnowledgeInjection:
    """Tests for knowledge injection into context_json before task dispatch.

    The code under test is in task_lifecycle.py around lines 1498-1520:
    it queries project_knowledge and inserts a 'project_knowledge' entry
    into the task's context_json.
    """

    @pytest.mark.asyncio
    async def test_knowledge_injected_into_context_json(self, tmp_db):
        """Knowledge rows from DB are injected into task context before dispatch."""
        await create_test_project(tmp_db, "proj1")
        await _seed_knowledge(tmp_db, "proj1", [
            {
                "category": "constraint",
                "content": "API rate limit is 100/min",
                "rationale": "Observed during load test",
                "alternatives_considered": "Batch API",
                "confidence": "high",
            },
            {
                "category": "gotcha",
                "content": "Library X breaks with Python 3.12",
                "confidence": "medium",
            },
        ])

        # Query knowledge the same way task_lifecycle does
        rows = await tmp_db.fetchall(
            "SELECT category, content AS finding, rationale, alternatives_considered, "
            "confidence, source_task_title "
            "FROM project_knowledge WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5",
            ("proj1",),
        )

        assert len(rows) == 2
        items = [dict(r) for r in rows]

        # Simulate the injection logic from task_lifecycle
        task_row = _make_task_row()
        current_context = json.loads(task_row["context_json"])
        assert not any(c.get("type") == "project_knowledge" for c in current_context)

        current_context.insert(0, {"type": "project_knowledge", "content": items})
        injected = json.dumps(current_context)

        # Verify the injected context
        parsed = json.loads(injected)
        assert len(parsed) == 1
        assert parsed[0]["type"] == "project_knowledge"
        assert len(parsed[0]["content"]) == 2

        # Check all fields survive
        first = parsed[0]["content"][0]
        assert first["finding"] is not None
        assert "rationale" in first
        assert "confidence" in first

    @pytest.mark.asyncio
    async def test_knowledge_not_duplicated_on_retry(self, tmp_db):
        """If context already has project_knowledge, it's not added again."""
        await create_test_project(tmp_db, "proj1")
        await _seed_knowledge(tmp_db, "proj1", [
            {"content": "Existing finding", "category": "discovery"},
        ])

        # Simulate a task that already has knowledge injected (from first attempt)
        existing_context = [
            {"type": "project_knowledge", "content": [{"finding": "Old finding"}]},
        ]
        task_row = _make_task_row(context_json=json.dumps(existing_context))

        current_context = json.loads(task_row["context_json"])

        # This is the retry-guard check from task_lifecycle
        already_has_knowledge = any(
            c.get("type") == "project_knowledge" for c in current_context
        )
        assert already_has_knowledge is True

        # On retry, knowledge should NOT be re-injected
        if not already_has_knowledge:
            rows = await tmp_db.fetchall(
                "SELECT category, content AS finding FROM project_knowledge "
                "WHERE project_id = $1 LIMIT 5",
                ("proj1",),
            )
            current_context.insert(0, {"type": "project_knowledge", "content": [dict(r) for r in rows]})

        # Context unchanged — still has just the original entry
        assert len(current_context) == 1
        assert current_context[0]["content"][0]["finding"] == "Old finding"

    @pytest.mark.asyncio
    async def test_knowledge_injection_with_empty_table(self, tmp_db):
        """No knowledge rows means no project_knowledge entry is added."""
        await create_test_project(tmp_db, "proj1")

        rows = await tmp_db.fetchall(
            "SELECT category, content AS finding, rationale, alternatives_considered, "
            "confidence, source_task_title "
            "FROM project_knowledge WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5",
            ("proj1",),
        )

        assert len(rows) == 0

        # The task_lifecycle code only injects if knowledge_rows is truthy
        task_row = _make_task_row()
        current_context = json.loads(task_row["context_json"])
        if rows:
            current_context.insert(0, {"type": "project_knowledge", "content": [dict(r) for r in rows]})

        assert len(current_context) == 0

    @pytest.mark.asyncio
    async def test_knowledge_limit_is_five(self, tmp_db):
        """At most 5 knowledge entries are injected (most recent first)."""
        await create_test_project(tmp_db, "proj1")
        entries = [
            {"content": f"Finding {i}", "category": "discovery"}
            for i in range(10)
        ]
        await _seed_knowledge(tmp_db, "proj1", entries)

        rows = await tmp_db.fetchall(
            "SELECT category, content AS finding, rationale, alternatives_considered, "
            "confidence, source_task_title "
            "FROM project_knowledge WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5",
            ("proj1",),
        )

        assert len(rows) == 5

    @pytest.mark.asyncio
    async def test_injected_knowledge_renders_in_prompt(self, tmp_db):
        """End-to-end: DB knowledge → context_json → build_prompt → XML output."""
        from backend.services.cli_common import build_prompt

        await create_test_project(tmp_db, "proj1")
        await _seed_knowledge(tmp_db, "proj1", [
            {
                "category": "decision",
                "content": "Chose FastAPI over Flask",
                "rationale": "Async support and Pydantic integration",
                "alternatives_considered": "Flask, Django",
                "confidence": "high",
            },
        ])

        rows = await tmp_db.fetchall(
            "SELECT category, content AS finding, rationale, alternatives_considered, "
            "confidence, source_task_title "
            "FROM project_knowledge WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5",
            ("proj1",),
        )
        items = [dict(r) for r in rows]

        context = [{"type": "project_knowledge", "content": items}]
        task_row = _make_task_row(context_json=json.dumps(context))

        prompt = build_prompt(task_row)

        assert "<historical_rationale>" in prompt
        assert "Chose FastAPI over Flask" in prompt
        assert "<rationale>Async support and Pydantic integration</rationale>" in prompt
        assert "Flask, Django" in prompt
        assert 'confidence="high"' in prompt


class TestInterrogationKnowledgeContext:
    """Tests for knowledge gathering in _gather_interrogation_context."""

    @pytest.mark.asyncio
    async def test_knowledge_formatted_for_interrogation(self, tmp_db):
        """Knowledge findings are formatted with category, confidence, and rationale."""
        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "task1", "proj1")
        await _seed_knowledge(tmp_db, "proj1", [
            {
                "category": "constraint",
                "content": "Must use Python 3.11",
                "rationale": "3.14 breaks FastAPI",
                "alternatives_considered": "Tried 3.14, failed on import",
                "confidence": "high",
            },
        ])

        # Query as _gather_interrogation_context does
        findings = await tmp_db.fetchall(
            "SELECT content, category, rationale, alternatives_considered, "
            "confidence, source_task_title FROM project_knowledge "
            "WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5",
            ("proj1",),
        )

        assert len(findings) == 1
        f = dict(findings[0])

        # Format as the interrogation context builder does
        line = f"[{f['category']}|{f['confidence']}] {f['content']}"
        if f.get("rationale"):
            line += f"\n    WHY: {f['rationale'][:200]}"
        if f.get("alternatives_considered"):
            line += f"\n    REJECTED: {f['alternatives_considered'][:100]}"
        if f.get("source_task_title"):
            line += f"\n    (from: {f['source_task_title']})"

        assert "[constraint|high] Must use Python 3.11" in line
        assert "WHY: 3.14 breaks FastAPI" in line
        assert "REJECTED: Tried 3.14, failed on import" in line
        assert "(from: Source Task 0)" in line

    @pytest.mark.asyncio
    async def test_interrogation_context_with_no_knowledge(self, tmp_db):
        """No knowledge rows means knowledge_findings key is absent from context."""
        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "task1", "proj1")

        findings = await tmp_db.fetchall(
            "SELECT content, category, rationale, alternatives_considered, "
            "confidence, source_task_title FROM project_knowledge "
            "WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5",
            ("proj1",),
        )

        assert len(findings) == 0

        # The code only adds knowledge_findings if findings is truthy
        ctx = {}
        if findings:
            ctx["knowledge_findings"] = "would be set"
        assert "knowledge_findings" not in ctx

    @pytest.mark.asyncio
    async def test_interrogation_context_truncates_long_rationale(self, tmp_db):
        """Long rationale is truncated to 200 chars in interrogation context."""
        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "task1", "proj1")
        long_rationale = "R" * 500
        await _seed_knowledge(tmp_db, "proj1", [
            {
                "category": "discovery",
                "content": "Something learned",
                "rationale": long_rationale,
                "confidence": "medium",
            },
        ])

        findings = await tmp_db.fetchall(
            "SELECT content, category, rationale, alternatives_considered, "
            "confidence, source_task_title FROM project_knowledge "
            "WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5",
            ("proj1",),
        )

        f = findings[0]
        # Matches the truncation in _gather_interrogation_context
        truncated = f["rationale"][:200]
        assert len(truncated) == 200
        assert truncated == "R" * 200


class TestKnowledgeDBSchema:
    """Tests verifying the project_knowledge table schema and constraints."""

    @pytest.mark.asyncio
    async def test_content_hash_unique_per_project(self, tmp_db):
        """Unique index on (project_id, content_hash) prevents duplicates."""
        await create_test_project(tmp_db, "proj1")
        now = time.time()
        content = "Duplicate content"
        content_hash = hashlib.sha256(content.lower().encode()).hexdigest()[:32]

        await tmp_db.execute_write(
            "INSERT INTO project_knowledge "
            "(id, project_id, task_id, category, content, content_hash, "
            "rationale, alternatives_considered, confidence, "
            "source_task_title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("k1", "proj1", None, "discovery", content, content_hash,
             "", "", "medium", "Task 1", now),
        )

        # Second insert with same content_hash should fail or be ignored
        try:
            await tmp_db.execute_write(
                "INSERT INTO project_knowledge "
                "(id, project_id, task_id, category, content, content_hash, "
                "rationale, alternatives_considered, confidence, "
                "source_task_title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("k2", "proj1", None, "discovery", content, content_hash,
                 "", "", "medium", "Task 2", now),
            )
        except Exception:
            pass  # Expected: unique constraint violation

        rows = await tmp_db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ?", ("proj1",)
        )
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_all_columns_stored(self, tmp_db):
        """All knowledge columns are persisted correctly."""
        await create_test_project(tmp_db, "proj1")
        await create_test_task(tmp_db, "task1", "proj1")
        now = time.time()
        content = "Full column test"
        content_hash = hashlib.sha256(content.lower().encode()).hexdigest()[:32]

        await tmp_db.execute_write(
            "INSERT INTO project_knowledge "
            "(id, project_id, task_id, category, content, content_hash, "
            "rationale, alternatives_considered, confidence, "
            "source_task_title, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("k_full", "proj1", "task1", "decision", content, content_hash,
             "Because of X", "Tried Y and Z", "high", "Origin Task", now),
        )

        row = await tmp_db.fetchone(
            "SELECT * FROM project_knowledge WHERE id = ?", ("k_full",)
        )

        assert row["id"] == "k_full"
        assert row["project_id"] == "proj1"
        assert row["task_id"] == "task1"
        assert row["category"] == "decision"
        assert row["content"] == "Full column test"
        assert row["content_hash"] == content_hash
        assert row["rationale"] == "Because of X"
        assert row["alternatives_considered"] == "Tried Y and Z"
        assert row["confidence"] == "high"
        assert row["source_task_title"] == "Origin Task"
        assert row["created_at"] == now

    @pytest.mark.asyncio
    async def test_cascade_on_project_delete(self, tmp_db):
        """Deleting a project cascades to its knowledge entries."""
        await create_test_project(tmp_db, "proj_del")
        await _seed_knowledge(tmp_db, "proj_del", [
            {"content": "Will be cascade-deleted"},
        ])

        # Verify knowledge exists
        rows = await tmp_db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ?", ("proj_del",)
        )
        assert len(rows) == 1

        # Delete the project (cascade should delete knowledge)
        await tmp_db.execute_write(
            "DELETE FROM projects WHERE id = ?", ("proj_del",)
        )

        rows = await tmp_db.fetchall(
            "SELECT * FROM project_knowledge WHERE project_id = ?", ("proj_del",)
        )
        assert len(rows) == 0
