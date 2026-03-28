"""Mimir agent verification tests.

Tests the Claude agent subprocess path through _spawn_agent, _call_verifier,
and MimirRunner._verify_task. All tests monkeypatch _spawn_agent so no
real claude binary or network is required.

Run with:
  cd Odin && pytest tests/test_mimir_agent.py -v
"""

from __future__ import annotations

import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, patch

from gods.pipeline import Event, Emit


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class FakeDB:
    """Minimal async DB stub."""

    def __init__(self, rows=None):
        self._rows = rows or {}
        self.writes = []

    async def fetchone(self, query, params=()):
        key = params[0] if params else None
        return self._rows.get(key)

    async def fetchall(self, query, params=()):
        return []

    async def execute_write(self, query, params=()):
        self.writes.append((query, params))


def _task_row(
    task_id,
    *,
    status="completed",
    verification_status=None,
    verification_notes=None,
    retry_count=0,
    max_retries=3,
    output_text="All tests pass. Implementation complete.",
):
    return {
        "id": task_id,
        "title": "Implement feature X",
        "description": "Add feature X to the codebase",
        "status": status,
        "verification_status": verification_status,
        "verification_notes": verification_notes,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "output_text": output_text,
        "context_json": "{}",
    }


def _relay_event_type(db):
    """Return the event_type from the last relay write."""
    for query, params in reversed(db.writes):
        if "god_relay_events" in query:
            return params[0]
    return None


# ---------------------------------------------------------------------------
# _call_verifier — agent subprocess path
# ---------------------------------------------------------------------------

class TestCallVerifier:

    @pytest.mark.asyncio
    async def test_agent_success_returns_agent_submitted(self):
        """When the agent exits 0, _call_verifier returns _agent_submitted."""
        from gods.handlers.mimir import _call_verifier

        with patch("gods.handlers.mimir._spawn_agent", new=AsyncMock(return_value=(0, b"done", b""))) as mock_spawn, \
             patch("gods.handlers.mimir.shutil.which", return_value="/usr/bin/claude"):
            result = await _call_verifier(
                task_title="Feature X",
                task_description="Add feature X",
                output_text="Implementation complete.",
                task_id="task-abc-123",
            )

        assert result["verdict"] == "_agent_submitted"
        assert mock_spawn.called

    @pytest.mark.asyncio
    async def test_agent_timeout_falls_back_to_gateway(self):
        """When _spawn_agent returns timeout sentinel, falls back to gateway verifier."""
        from gods.handlers.mimir import _call_verifier

        gateway_result = {"verdict": "passed", "confidence": 0.8, "feedback": "looks good"}

        with patch("gods.handlers.mimir._spawn_agent", new=AsyncMock(return_value=(-1, b"", b"timeout"))), \
             patch("gods.handlers.mimir.shutil.which", return_value="/usr/bin/claude"), \
             patch("gods.handlers.mimir._call_verifier_gateway", new=AsyncMock(return_value=gateway_result)) as mock_gw:
            result = await _call_verifier(
                task_title="Feature X",
                task_description="Add feature X",
                output_text="Implementation complete.",
                task_id="task-abc-123",
            )

        assert result["verdict"] == "passed"
        assert mock_gw.called

    @pytest.mark.asyncio
    async def test_agent_nonzero_exit_falls_back_to_gateway(self):
        """When agent exits non-zero (crash, auth fail), falls back to gateway."""
        from gods.handlers.mimir import _call_verifier

        gateway_result = {"verdict": "gaps_found", "confidence": 0.7, "feedback": "missing tests"}

        with patch("gods.handlers.mimir._spawn_agent", new=AsyncMock(return_value=(1, b"", b"Error: auth failed"))), \
             patch("gods.handlers.mimir.shutil.which", return_value="/usr/bin/claude"), \
             patch("gods.handlers.mimir._call_verifier_gateway", new=AsyncMock(return_value=gateway_result)) as mock_gw:
            result = await _call_verifier(
                task_title="Feature X",
                task_description="Add feature X",
                output_text="Implementation complete.",
                task_id="task-abc-123",
            )

        assert result["verdict"] == "gaps_found"
        assert mock_gw.called

    @pytest.mark.asyncio
    async def test_no_claude_binary_falls_back_to_gateway(self):
        """When claude is not on PATH, skips agent entirely and uses gateway."""
        from gods.handlers.mimir import _call_verifier

        gateway_result = {"verdict": "passed", "confidence": 0.9, "feedback": ""}

        with patch("gods.handlers.mimir.shutil.which", return_value=None), \
             patch("gods.handlers.mimir._spawn_agent", new=AsyncMock()) as mock_spawn, \
             patch("gods.handlers.mimir._call_verifier_gateway", new=AsyncMock(return_value=gateway_result)):
            result = await _call_verifier(
                task_title="Feature X",
                task_description="Add feature X",
                output_text="Implementation complete.",
                task_id="task-abc-123",
            )

        assert result["verdict"] == "passed"
        mock_spawn.assert_not_called()


# ---------------------------------------------------------------------------
# MimirRunner._verify_task — relay event routing
# ---------------------------------------------------------------------------

class TestVerifyTaskRelay:

    @pytest.mark.asyncio
    async def test_agent_submitted_passed_emits_task_verified(self):
        """When agent submits 'passed', relay gets task_verified."""
        from gods.handlers.mimir import MimirRunner

        task_id = "task-111"
        db = FakeDB(rows={
            task_id: {
                **_task_row(task_id),
                "verification_status": "passed",
                "status": "completed",
                "verification_notes": "Looks good",
            }
        })
        runner = MimirRunner(db=db)

        with patch("gods.handlers.mimir._call_verifier", new=AsyncMock(
            return_value={"verdict": "_agent_submitted", "confidence": 1.0, "feedback": ""}
        )):
            await runner._verify_task(
                task_id=task_id,
                project_id="proj-1",
                title="Feature X",
                description="desc",
                output_text="done",
                retry_count=0,
                max_retries=3,
            )

        assert _relay_event_type(db) == "task_verified"

    @pytest.mark.asyncio
    async def test_agent_submitted_gaps_found_emits_task_rejected(self):
        """When agent submits 'gaps_found' and retries remain, relay gets task_rejected."""
        from gods.handlers.mimir import MimirRunner

        task_id = "task-222"
        db = FakeDB(rows={
            task_id: {
                **_task_row(task_id),
                "verification_status": "gaps_found",
                "status": "pending",  # engine already reset to pending
                "verification_notes": "Missing error handling",
            }
        })
        runner = MimirRunner(db=db)

        with patch("gods.handlers.mimir._call_verifier", new=AsyncMock(
            return_value={"verdict": "_agent_submitted", "confidence": 1.0, "feedback": ""}
        )):
            await runner._verify_task(
                task_id=task_id,
                project_id="proj-1",
                title="Feature X",
                description="desc",
                output_text="done",
                retry_count=1,
                max_retries=3,
            )

        assert _relay_event_type(db) == "task_rejected"

    @pytest.mark.asyncio
    async def test_agent_submitted_human_needed_emits_needs_human_review(self):
        """When agent submits 'human_needed', relay gets needs_human_review."""
        from gods.handlers.mimir import MimirRunner

        task_id = "task-333"
        db = FakeDB(rows={
            task_id: {
                **_task_row(task_id),
                "verification_status": "human_needed",
                "status": "needs_review",
                "verification_notes": "Ambiguous output",
            }
        })
        runner = MimirRunner(db=db)

        with patch("gods.handlers.mimir._call_verifier", new=AsyncMock(
            return_value={"verdict": "_agent_submitted", "confidence": 1.0, "feedback": ""}
        )):
            await runner._verify_task(
                task_id=task_id,
                project_id="proj-1",
                title="Feature X",
                description="desc",
                output_text="done",
                retry_count=0,
                max_retries=3,
            )

        assert _relay_event_type(db) == "needs_human_review"

    @pytest.mark.asyncio
    async def test_verifier_exception_emits_needs_human_review(self):
        """When _call_verifier raises (network error, etc), relay gets needs_human_review — not silently dropped."""
        from gods.handlers.mimir import MimirRunner

        task_id = "task-444"
        db = FakeDB(rows={task_id: _task_row(task_id)})
        runner = MimirRunner(db=db)

        with patch("gods.handlers.mimir._call_verifier", new=AsyncMock(
            side_effect=RuntimeError("connection refused")
        )):
            await runner._verify_task(
                task_id=task_id,
                project_id="proj-1",
                title="Feature X",
                description="desc",
                output_text="done",
                retry_count=0,
                max_retries=3,
            )

        assert _relay_event_type(db) == "needs_human_review"

    @pytest.mark.asyncio
    async def test_timeout_and_gateway_passed_emits_task_verified(self):
        """Timeout → gateway fallback → passed verdict → task_verified in relay."""
        from gods.handlers.mimir import MimirRunner

        task_id = "task-555"
        db = FakeDB(rows={task_id: _task_row(task_id)})
        runner = MimirRunner(db=db)

        with patch("gods.handlers.mimir._call_verifier", new=AsyncMock(
            return_value={"verdict": "passed", "confidence": 0.8, "feedback": "gateway fallback"}
        )), patch("gods.handlers.mimir._extract_knowledge", new=AsyncMock(return_value=[])):
            await runner._verify_task(
                task_id=task_id,
                project_id="proj-1",
                title="Feature X",
                description="desc",
                output_text="done",
                retry_count=0,
                max_retries=3,
            )

        assert _relay_event_type(db) == "task_verified"

    @pytest.mark.asyncio
    async def test_timeout_and_gateway_gaps_found_emits_task_rejected(self):
        """Timeout → gateway fallback → gaps_found → task_rejected in relay (retry available)."""
        from gods.handlers.mimir import MimirRunner

        task_id = "task-666"
        db = FakeDB(rows={task_id: _task_row(task_id)})
        runner = MimirRunner(db=db)

        with patch("gods.handlers.mimir._call_verifier", new=AsyncMock(
            return_value={"verdict": "gaps_found", "confidence": 0.6, "feedback": "missing tests"}
        )):
            await runner._verify_task(
                task_id=task_id,
                project_id="proj-1",
                title="Feature X",
                description="desc",
                output_text="done",
                retry_count=0,
                max_retries=3,
            )

        assert _relay_event_type(db) == "task_rejected"
