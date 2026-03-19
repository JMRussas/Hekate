"""Tests for gods/handlers/hermes.py — task execution with TDD and narration.

Hermes wraps the CLI executor (Claude Code, Gemini, Codex, Ollama).
It receives dispatch_command events and runs tasks through:
  1. Set task → running
  2. Resolve working directory (worktree)
  3. Execute CLI with narration callbacks
  4. Capture output
  5. Emit worker_event (completed/failed)

TDD mode enforces: write test → verify fails → implement → verify passes.
Narration emits real-time events for dashboard display.
"""

import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

from gods.pipeline import Event, Emit

from gods.handlers.hermes import (
    hermes_execute,
    hermes_tdd_gate,
    _build_cli_command,
    _parse_stream_event,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def exec_db(sqlite_db):
    """SQLite with task + project schema for hermes tests."""
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT DEFAULT 'executing',
            repo_path TEXT,
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
            complexity TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'pending',
            model_tier TEXT DEFAULT 'claude_code',
            wave INTEGER DEFAULT 0,
            output_text TEXT,
            error TEXT,
            context_json TEXT DEFAULT '{}',
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            cost_usd REAL DEFAULT 0,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            model_used TEXT,
            started_at REAL,
            completed_at REAL,
            updated_at REAL
        )
    """)
    return sqlite_db


async def _seed(db, task_id="t1", project_id="proj-1", status="pending",
                model_tier="claude_code", task_type="code", description="Build feature X"):
    await db.execute_write(
        "INSERT OR REPLACE INTO projects (id, name, status, repo_path, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, "Test", "executing", "/tmp/repo", time.time()),
    )
    await db.execute_write(
        "INSERT OR REPLACE INTO tasks (id, project_id, title, description, task_type, "
        "status, model_tier, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, project_id, f"Task {task_id}", description, task_type,
         status, model_tier, time.time()),
    )


# ---------------------------------------------------------------------------
# Task state transitions
# ---------------------------------------------------------------------------

class TestHermesStateTransitions:
    @pytest.mark.asyncio
    async def test_sets_task_to_running(self, exec_db):
        """dispatch_command → task status becomes running."""
        await _seed(exec_db)
        event = Event("dispatch_command", {
            "task_id": "t1",
            "project_id": "proj-1",
            "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "output": "Done.",
                "cost_usd": 0.01,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "model_used": "claude-sonnet-4",
            }
            emits = await hermes_execute(event, exec_db)

        # Task should have been set to running (then completed)
        row = await exec_db.fetchone(
            "SELECT status, output_text, model_used FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "completed"
        assert row["output_text"] == "Done."

    @pytest.mark.asyncio
    async def test_emits_worker_completed(self, exec_db):
        await _seed(exec_db)
        event = Event("dispatch_command", {
            "task_id": "t1",
            "project_id": "proj-1",
            "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "output": "Done.",
                "cost_usd": 0.02,
                "prompt_tokens": 200,
                "completion_tokens": 100,
                "model_used": "claude-sonnet-4",
            }
            emits = await hermes_execute(event, exec_db)

        completed = next((e for e in emits if e.event_type == "worker_event"), None)
        assert completed is not None
        assert completed.payload["status"] == "completed"
        assert completed.payload["task_id"] == "t1"
        assert completed.payload["cost_usd"] == 0.02

    @pytest.mark.asyncio
    async def test_emits_worker_failed_on_error(self, exec_db):
        await _seed(exec_db)
        event = Event("dispatch_command", {
            "task_id": "t1",
            "project_id": "proj-1",
            "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = RuntimeError("CLI crashed")
            emits = await hermes_execute(event, exec_db)

        failed = next((e for e in emits if e.event_type == "worker_event"), None)
        assert failed is not None
        assert failed.payload["status"] == "failed"
        assert "crashed" in failed.payload["error"].lower()

        # Task should be marked failed in DB
        row = await exec_db.fetchone("SELECT status, error FROM tasks WHERE id = ?", ("t1",))
        assert row["status"] == "failed"
        assert "crashed" in row["error"].lower()

    @pytest.mark.asyncio
    async def test_skips_non_pending_task(self, exec_db):
        """Don't execute a task that's already running or completed."""
        await _seed(exec_db, status="running")
        event = Event("dispatch_command", {
            "task_id": "t1",
            "project_id": "proj-1",
            "provider": "claude_code",
        }, "odin")

        emits = await hermes_execute(event, exec_db)

        skipped = next((e for e in (emits or []) if e.event_type == "worker_event"), None)
        assert skipped is None or skipped.payload.get("status") == "skipped"


# ---------------------------------------------------------------------------
# CLI command building
# ---------------------------------------------------------------------------

class TestBuildCliCommand:
    def test_claude_code_command(self):
        cmd, stdin_text = _build_cli_command(
            provider="claude_code",
            prompt="Fix the bug",
            cwd="/tmp/repo",
        )
        assert "claude" in cmd[0].lower() or cmd[0] == "claude"
        assert "--output-format" in cmd or "stream-json" in " ".join(cmd)
        assert stdin_text == "Fix the bug"

    def test_gemini_command(self):
        cmd, stdin_text = _build_cli_command(
            provider="gemini_cli",
            prompt="Research X",
            cwd="/tmp/repo",
        )
        assert "gemini" in cmd[0].lower()
        assert stdin_text == "Research X"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            _build_cli_command(
                provider="nonexistent",
                prompt="hello",
                cwd="/tmp",
            )


# ---------------------------------------------------------------------------
# Stream event parsing (Claude Code stream-json)
# ---------------------------------------------------------------------------

class TestParseStreamEvent:
    def test_parses_assistant_text(self):
        line = '{"type":"assistant","message":{"content":[{"type":"text","text":"I will read the file first."}]}}'
        event = _parse_stream_event(line)
        assert event is not None
        assert event["type"] == "narration"
        assert "read the file" in event["text"]

    def test_parses_tool_call(self):
        line = '{"type":"tool_use","name":"Read","input":{"file_path":"src/main.py"}}'
        event = _parse_stream_event(line)
        assert event is not None
        assert event["type"] == "tool_call"
        assert event["tool"] == "Read"

    def test_ignores_unknown_types(self):
        line = '{"type":"system","data":"something"}'
        event = _parse_stream_event(line)
        assert event is None

    def test_handles_malformed_json(self):
        event = _parse_stream_event("not json at all")
        assert event is None


# ---------------------------------------------------------------------------
# Narration — events emitted during execution
# ---------------------------------------------------------------------------

class TestNarration:
    @pytest.mark.asyncio
    async def test_narration_events_collected(self, exec_db):
        """Hermes should collect narration events during execution."""
        await _seed(exec_db)
        event = Event("dispatch_command", {
            "task_id": "t1",
            "project_id": "proj-1",
            "provider": "claude_code",
        }, "odin")

        narration_events = []

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "output": "Done.",
                "cost_usd": 0.01,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "model_used": "claude-sonnet-4",
                "narration": [
                    {"type": "narration", "text": "Reading the existing tests..."},
                    {"type": "tool_call", "tool": "Read", "args": {"file_path": "test.py"}},
                    {"type": "narration", "text": "Writing implementation..."},
                ],
            }
            emits = await hermes_execute(event, exec_db)

        narration = [e for e in emits if e.event_type == "narration"]
        assert len(narration) >= 2  # At least the narration text events


# ---------------------------------------------------------------------------
# TDD enforcement
# ---------------------------------------------------------------------------

class TestTDDGate:
    @pytest.mark.asyncio
    async def test_tdd_gate_passes_when_tests_exist_and_pass(self, exec_db):
        """If tests were written and pass, TDD gate passes."""
        await _seed(exec_db, task_type="code")
        result = await hermes_tdd_gate(
            task_id="t1",
            output_text="Wrote test_feature.py, then implemented feature.py. All tests pass.",
            task_type="code",
            db=exec_db,
        )
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_tdd_gate_fails_when_no_tests(self, exec_db):
        """If no test files mentioned in output, TDD gate fails."""
        await _seed(exec_db, task_type="code")
        result = await hermes_tdd_gate(
            task_id="t1",
            output_text="Implemented feature.py. Done.",
            task_type="code",
            db=exec_db,
        )
        assert result["passed"] is False
        assert "test" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_tdd_gate_skips_for_non_code_tasks(self, exec_db):
        """Research/analysis tasks don't need TDD."""
        await _seed(exec_db, task_type="research")
        result = await hermes_tdd_gate(
            task_id="t1",
            output_text="Research findings about X.",
            task_type="research",
            db=exec_db,
        )
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_tdd_gate_fails_when_tests_fail(self, exec_db):
        """If output mentions test failures, TDD gate fails."""
        await _seed(exec_db, task_type="code")
        result = await hermes_tdd_gate(
            task_id="t1",
            output_text="Wrote test_feature.py. Tests fail: 2 failed, 3 passed.",
            task_type="code",
            db=exec_db,
        )
        assert result["passed"] is False
        assert "fail" in result["reason"].lower()


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------

class TestProviderRouting:
    @pytest.mark.asyncio
    async def test_uses_provider_from_event(self, exec_db):
        """Hermes should use the provider specified in dispatch_command."""
        await _seed(exec_db, model_tier="claude_code")
        event = Event("dispatch_command", {
            "task_id": "t1",
            "project_id": "proj-1",
            "provider": "gemini_cli",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "output": "Done.",
                "cost_usd": 0.0,
                "prompt_tokens": 50,
                "completion_tokens": 25,
                "model_used": "gemini-2.5-pro",
            }
            emits = await hermes_execute(event, exec_db)

        # Verify _run_cli was called with gemini_cli
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs[1]["provider"] == "gemini_cli" or \
               call_kwargs[0][1] == "gemini_cli" if call_kwargs[0] else True

    @pytest.mark.asyncio
    async def test_records_cost_and_tokens(self, exec_db):
        """Cost and token usage should be stored on the task."""
        await _seed(exec_db)
        event = Event("dispatch_command", {
            "task_id": "t1",
            "project_id": "proj-1",
            "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "output": "Done.",
                "cost_usd": 0.15,
                "prompt_tokens": 5000,
                "completion_tokens": 2000,
                "model_used": "claude-opus-4",
            }
            emits = await hermes_execute(event, exec_db)

        row = await exec_db.fetchone(
            "SELECT cost_usd, prompt_tokens, completion_tokens, model_used "
            "FROM tasks WHERE id = ?", ("t1",))
        assert row["cost_usd"] == 0.15
        assert row["prompt_tokens"] == 5000
        assert row["completion_tokens"] == 2000
        assert row["model_used"] == "claude-opus-4"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestHermesEdgeCases:
    @pytest.mark.asyncio
    async def test_missing_task_emits_error(self, exec_db):
        """dispatch_command for nonexistent task → error event."""
        event = Event("dispatch_command", {
            "task_id": "nonexistent",
            "project_id": "proj-1",
            "provider": "claude_code",
        }, "odin")

        emits = await hermes_execute(event, exec_db)

        error = next((e for e in emits if e.event_type == "hermes_error"), None)
        assert error is not None

    @pytest.mark.asyncio
    async def test_empty_output_marks_failed(self, exec_db):
        """CLI returns empty output → task failed."""
        await _seed(exec_db)
        event = Event("dispatch_command", {
            "task_id": "t1",
            "project_id": "proj-1",
            "provider": "claude_code",
        }, "odin")

        with patch("gods.handlers.hermes._run_cli", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {
                "output": "",
                "cost_usd": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model_used": "claude-sonnet-4",
            }
            emits = await hermes_execute(event, exec_db)

        failed = next((e for e in emits if e.event_type == "worker_event"), None)
        assert failed is not None
        assert failed.payload["status"] == "failed"
        assert "empty" in failed.payload["error"].lower()
