"""Tests for the agent-based verification path in mimir.py.

Covers _call_verifier via monkeypatching _spawn_agent:
  - Agent succeeds and submits verdict → _agent_submitted sentinel
  - Agent times out → gateway fallback
  - Agent exits non-zero → gateway fallback
  - claude binary missing → gateway fallback
  - Gateway fallback parses verdict correctly

Also covers _verify_task handling of _agent_submitted:
  - passed verdict → task_verified relay event
  - gaps_found verdict + retries remaining → task_rejected relay event
  - gaps_found verdict + max retries → needs_human_review relay event
  - human_needed verdict → needs_human_review relay event
"""

import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from gods.pipeline import Event
from gods.handlers.mimir import (
    MimirRunner,
    _call_verifier,
    _call_verifier_gateway,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def mimir_db(sqlite_db):
    """SQLite with full task + relay schema for mimir agent tests."""
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT DEFAULT 'executing',
            config_json TEXT DEFAULT '{}',
            updated_at REAL
        )
    """)
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            title TEXT,
            description TEXT DEFAULT '',
            task_type TEXT DEFAULT 'code',
            status TEXT DEFAULT 'completed',
            model_tier TEXT DEFAULT 'claude_code',
            output_text TEXT,
            error TEXT,
            context_json TEXT DEFAULT '{}',
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            verification_status TEXT,
            verification_notes TEXT,
            updated_at REAL
        )
    """)
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS project_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            task_id TEXT,
            content TEXT,
            created_at REAL
        )
    """)
    return sqlite_db


async def _insert_task(db, task_id, project_id, output_text="output ok", retry_count=0,
                       max_retries=3, status="completed", verification_status=None):
    await db.execute_write(
        "INSERT INTO tasks (id, project_id, title, description, output_text, "
        "retry_count, max_retries, status, verification_status, updated_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        (task_id, project_id, "Test task", "Do something useful",
         output_text, retry_count, max_retries, status, verification_status, time.time()),
    )


# ---------------------------------------------------------------------------
# _call_verifier — agent path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_call_verifier_agent_success():
    """Agent exits 0 → returns _agent_submitted sentinel."""
    with patch("gods.handlers.mimir._spawn_agent", new_callable=AsyncMock) as mock_spawn:
        mock_spawn.return_value = (0, b"done", b"")
        with patch("shutil.which", return_value="/usr/bin/claude"):
            result = await _call_verifier(
                task_title="Test",
                task_description="desc",
                output_text="output",
                task_id="task-001",
            )
    assert result["verdict"] == "_agent_submitted"
    assert mock_spawn.called


@pytest.mark.asyncio
async def test_call_verifier_agent_timeout_falls_back_to_gateway():
    """Agent timeout → gateway fallback, NOT human_needed directly."""
    gateway_result = {"verdict": "passed", "confidence": 0.9, "feedback": "looks good"}
    with patch("gods.handlers.mimir._spawn_agent", new_callable=AsyncMock) as mock_spawn:
        mock_spawn.return_value = (-1, b"", b"timeout")
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("gods.handlers.mimir._call_verifier_gateway", new_callable=AsyncMock) as mock_gw:
                mock_gw.return_value = gateway_result
                result = await _call_verifier(
                    task_title="Test",
                    task_description="desc",
                    output_text="output",
                    task_id="task-001",
                )
    assert result["verdict"] == "passed"
    assert mock_gw.called


@pytest.mark.asyncio
async def test_call_verifier_agent_nonzero_exit_falls_back_to_gateway():
    """Agent exits non-zero → gateway fallback."""
    gateway_result = {"verdict": "gaps_found", "confidence": 0.7, "feedback": "missing tests"}
    with patch("gods.handlers.mimir._spawn_agent", new_callable=AsyncMock) as mock_spawn:
        mock_spawn.return_value = (1, b"", b"some error")
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("gods.handlers.mimir._call_verifier_gateway", new_callable=AsyncMock) as mock_gw:
                mock_gw.return_value = gateway_result
                result = await _call_verifier(
                    task_title="Test",
                    task_description="desc",
                    output_text="output",
                    task_id="task-001",
                )
    assert result["verdict"] == "gaps_found"
    assert mock_gw.called


@pytest.mark.asyncio
async def test_call_verifier_no_claude_binary_falls_back_to_gateway():
    """No claude binary → gateway fallback immediately."""
    gateway_result = {"verdict": "passed", "confidence": 1.0, "feedback": ""}
    with patch("shutil.which", return_value=None):
        with patch("gods.handlers.mimir._call_verifier_gateway", new_callable=AsyncMock) as mock_gw:
            mock_gw.return_value = gateway_result
            result = await _call_verifier(
                task_title="Test",
                task_description="desc",
                output_text="output",
                task_id="task-001",
            )
    assert result["verdict"] == "passed"
    assert mock_gw.called


# ---------------------------------------------------------------------------
# _verify_task — _agent_submitted handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_task_agent_passed(mimir_db):
    """_agent_submitted + verification_status=passed → task_verified relay event."""
    await _insert_task(mimir_db, "t1", "p1", verification_status="passed")

    runner = MimirRunner(db=mimir_db)
    with patch("gods.handlers.mimir._spawn_agent", new_callable=AsyncMock) as mock_spawn:
        mock_spawn.return_value = (0, b"", b"")
        with patch("shutil.which", return_value="/usr/bin/claude"):
            await runner._verify_task(
                task_id="t1",
                project_id="p1",
                title="Test task",
                description="desc",
                output_text="output ok",
                retry_count=0,
                max_retries=3,
            )

    rows = await mimir_db.fetchall(
        "SELECT event_type FROM god_relay_events WHERE source = 'mimir'", ()
    )
    event_types = [r["event_type"] for r in rows]
    assert "task_verified" in event_types


@pytest.mark.asyncio
async def test_verify_task_agent_gaps_found_retries(mimir_db):
    """_agent_submitted + status=pending (gaps_found retry) → task_rejected relay event."""
    await _insert_task(mimir_db, "t2", "p1", status="pending", verification_status="gaps_found")

    runner = MimirRunner(db=mimir_db)
    with patch("gods.handlers.mimir._spawn_agent", new_callable=AsyncMock) as mock_spawn:
        mock_spawn.return_value = (0, b"", b"")
        with patch("shutil.which", return_value="/usr/bin/claude"):
            await runner._verify_task(
                task_id="t2",
                project_id="p1",
                title="Test task",
                description="desc",
                output_text="output ok",
                retry_count=1,
                max_retries=3,
            )

    rows = await mimir_db.fetchall(
        "SELECT event_type FROM god_relay_events WHERE source = 'mimir'", ()
    )
    event_types = [r["event_type"] for r in rows]
    assert "task_rejected" in event_types


@pytest.mark.asyncio
async def test_verify_task_agent_human_needed(mimir_db):
    """_agent_submitted + status=needs_review (human_needed) → needs_human_review relay event."""
    await _insert_task(mimir_db, "t3", "p1", status="needs_review", verification_status="human_needed")

    runner = MimirRunner(db=mimir_db)
    with patch("gods.handlers.mimir._spawn_agent", new_callable=AsyncMock) as mock_spawn:
        mock_spawn.return_value = (0, b"", b"")
        with patch("shutil.which", return_value="/usr/bin/claude"):
            await runner._verify_task(
                task_id="t3",
                project_id="p1",
                title="Test task",
                description="desc",
                output_text="output ok",
                retry_count=0,
                max_retries=3,
            )

    rows = await mimir_db.fetchall(
        "SELECT event_type FROM god_relay_events WHERE source = 'mimir'", ()
    )
    event_types = [r["event_type"] for r in rows]
    assert "needs_human_review" in event_types


@pytest.mark.asyncio
async def test_verify_task_gateway_fallback_on_timeout(mimir_db):
    """Agent timeout → gateway returns passed → task_verified relay event."""
    await _insert_task(mimir_db, "t4", "p1")

    runner = MimirRunner(db=mimir_db)
    with patch("gods.handlers.mimir._spawn_agent", new_callable=AsyncMock) as mock_spawn:
        mock_spawn.return_value = (-1, b"", b"timeout")
        with patch("shutil.which", return_value="/usr/bin/claude"):
            with patch("gods.handlers.mimir._call_verifier_gateway", new_callable=AsyncMock) as mock_gw:
                mock_gw.return_value = {"verdict": "passed", "confidence": 0.8, "feedback": "ok via gateway"}
                await runner._verify_task(
                    task_id="t4",
                    project_id="p1",
                    title="Test task",
                    description="desc",
                    output_text="output ok",
                    retry_count=0,
                    max_retries=3,
                )

    rows = await mimir_db.fetchall(
        "SELECT event_type FROM god_relay_events WHERE source = 'mimir'", ()
    )
    event_types = [r["event_type"] for r in rows]
    assert "task_verified" in event_types
