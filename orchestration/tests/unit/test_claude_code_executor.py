#  Orchestration Engine - Claude Code Executor Tests
#
#  Tests for run_claude_code_task: prompt building, stream-json parsing,
#  progress event mapping, timeout handling, and error propagation.
#
#  Depends on: backend/services/claude_code_executor.py
#  Used by:    pytest

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.cli_common import build_prompt as _build_prompt, resolve_cwd as _resolve_cwd
from backend.services.claude_code_executor import (
    _handle_stream_event,
    run_claude_code_task,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_task_row(**overrides):
    """Build a task dict matching what DB queries return."""
    defaults = {
        "id": "task_001",
        "project_id": "proj_001",
        "plan_id": "plan_001",
        "title": "Test Task",
        "description": "Implement the Foo class",
        "task_type": "code",
        "priority": 50,
        "status": "queued",
        "model_tier": "claude_code",
        "model_used": None,
        "context_json": "[]",
        "tools_json": "[]",
        "system_prompt": "",
        "output_text": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "max_tokens": 4096,
        "retry_count": 0,
        "max_retries": 5,
        "wave": 0,
        "verification_status": None,
        "verification_notes": None,
        "error": None,
        "started_at": None,
        "completed_at": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    defaults.update(overrides)
    return defaults


def _stream_lines(*events):
    """Encode events as newline-delimited JSON bytes (stream-json format)."""
    lines = []
    for event in events:
        lines.append(json.dumps(event).encode("utf-8") + b"\n")
    return b"".join(lines)


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_description_only(self):
        row = _make_task_row(description="Do X", system_prompt="", context_json="[]")
        prompt = _build_prompt(row)
        assert "Do X" in prompt

    def test_includes_system_prompt(self):
        row = _make_task_row(
            description="Do X",
            system_prompt="You are a focused task executor.",
        )
        prompt = _build_prompt(row)
        assert "You are a focused task executor." in prompt
        assert "Do X" in prompt

    def test_includes_context(self):
        context = [{"type": "dependency_output", "content": "Previous result here"}]
        row = _make_task_row(
            description="Do X",
            context_json=json.dumps(context),
        )
        prompt = _build_prompt(row)
        assert "<dependency_output>" in prompt
        assert "Previous result here" in prompt

    def test_description_is_last(self):
        row = _make_task_row(
            description="Do X",
            system_prompt="System prompt",
            context_json=json.dumps([{"type": "ctx", "content": "context"}]),
        )
        prompt = _build_prompt(row)
        assert prompt.endswith("Do X")

    def test_context_type_sanitized(self):
        """Malicious context types should be sanitized to prevent prompt injection."""
        context = [{"type": "foo><malicious><bar", "content": "some content"}]
        row = _make_task_row(
            description="Do X",
            context_json=json.dumps(context),
        )
        prompt = _build_prompt(row)
        # Angle brackets should be replaced with underscores
        assert "<foo__malicious__bar>" in prompt
        assert "foo><malicious>" not in prompt

    def test_context_type_alphanumeric_preserved(self):
        """Normal context types with underscores should pass through unchanged."""
        context = [{"type": "dependency_output_v2", "content": "result"}]
        row = _make_task_row(
            description="Do X",
            context_json=json.dumps(context),
        )
        prompt = _build_prompt(row)
        assert "<dependency_output_v2>" in prompt


# ---------------------------------------------------------------------------
# _resolve_cwd
# ---------------------------------------------------------------------------

class TestResolveCwd:
    @pytest.mark.asyncio
    async def test_returns_repo_path(self):
        db = AsyncMock()
        db.fetchone = AsyncMock(return_value={"repo_path": "/home/user/project"})
        result = await _resolve_cwd(db, "proj_001")
        assert result == "/home/user/project"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_repo_path(self):
        db = AsyncMock()
        db.fetchone = AsyncMock(return_value={"repo_path": None})
        result = await _resolve_cwd(db, "proj_001")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_db_error(self):
        db = AsyncMock()
        db.fetchone = AsyncMock(side_effect=Exception("DB error"))
        result = await _resolve_cwd(db, "proj_001")
        assert result is None


# ---------------------------------------------------------------------------
# _handle_stream_event
# ---------------------------------------------------------------------------

class TestHandleStreamEvent:
    @pytest.mark.asyncio
    async def test_assistant_text_appended(self):
        progress = AsyncMock()
        text_parts = []

        event = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Hello world"}]
            },
        }
        await _handle_stream_event(event, "t1", "p1", progress, text_parts)

        assert text_parts == ["Hello world"]
        progress.push_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_tool_use_pushes_progress(self):
        progress = AsyncMock()
        text_parts = []

        event = {"type": "tool_use", "name": "bash"}
        await _handle_stream_event(event, "t1", "p1", progress, text_parts)

        progress.push_event.assert_called_once()
        call_kwargs = progress.push_event.call_args
        assert "bash" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_tool_result_no_progress(self):
        progress = AsyncMock()
        text_parts = []

        event = {"type": "tool_result", "output": "some output"}
        await _handle_stream_event(event, "t1", "p1", progress, text_parts)

        progress.push_event.assert_not_called()
        assert text_parts == []

    @pytest.mark.asyncio
    async def test_error_event_pushes_progress(self):
        progress = AsyncMock()
        text_parts = []

        event = {"type": "error", "error": {"message": "Something broke"}}
        await _handle_stream_event(event, "t1", "p1", progress, text_parts)

        progress.push_event.assert_called_once()


# ---------------------------------------------------------------------------
# run_claude_code_task — full integration with mocked subprocess
# ---------------------------------------------------------------------------

class TestRunClaudeCodeTask:
    @pytest.mark.asyncio
    @patch("backend.services.claude_code_executor._resolve_cmd", return_value="claude")
    async def test_successful_execution(self, mock_resolve):
        """Mocks the subprocess to return stream-json events."""
        task_row = _make_task_row()

        # Build stream-json output
        stream = _stream_lines(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "I'll implement Foo."}]},
            },
            {
                "type": "tool_use",
                "name": "write",
            },
            {
                "type": "result",
                "result": "Done. Foo class created.",
                "total_cost": 0.0,
                "model": "claude-opus-4-6",
                "usage": {"input_tokens": 500, "output_tokens": 200},
            },
        )

        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        # Simulate stdout line-by-line reading
        lines = stream.split(b"\n")
        line_iter = iter(lines)
        async def mock_readline():
            try:
                line = next(line_iter)
                return line + b"\n" if line else b""
            except StopIteration:
                return b""
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = mock_readline
        mock_proc.wait = AsyncMock()

        db = AsyncMock()
        db.fetchone = AsyncMock(return_value={"repo_path": "/test/project"})
        budget = AsyncMock()
        budget.record_spend = AsyncMock()
        progress = AsyncMock()
        progress.push_event = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_claude_code_task(
                task_row=task_row, db=db, budget=budget, progress=progress,
            )

        assert "I'll implement Foo" in result["output"]
        assert "Done. Foo class created." in result["output"]
        assert result["model_used"] == "claude-opus-4-6"
        assert result["cost_usd"] == 0.0
        # Verify audit trail is recorded even for $0 cost
        budget.record_spend.assert_called_once()
        assert budget.record_spend.call_args.kwargs["cost_usd"] == 0.0
        assert budget.record_spend.call_args.kwargs["purpose"] == "execution"

    @pytest.mark.asyncio
    @patch("backend.services.claude_code_executor._resolve_cmd", return_value="claude")
    async def test_cli_failure_raises(self, mock_resolve):
        """Non-zero exit with no output raises RuntimeError."""
        task_row = _make_task_row()

        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"Error: invalid config")
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()

        db = AsyncMock()
        db.fetchone = AsyncMock(return_value={"repo_path": None})
        budget = AsyncMock()
        progress = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="Claude Code CLI failed"):
                await run_claude_code_task(
                    task_row=task_row, db=db, budget=budget, progress=progress,
                )

    @pytest.mark.asyncio
    @patch("backend.services.claude_code_executor._resolve_cmd", return_value="claude")
    @patch("backend.services.claude_code_executor.CLAUDE_CODE_TIMEOUT", 0.01)
    async def test_timeout_produces_partial_output(self, mock_resolve):
        """Timeout appends a message but doesn't lose prior output."""
        task_row = _make_task_row()

        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.stdin.close = MagicMock()
        mock_proc.returncode = -9
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        # Simulate a readline that hangs
        async def hang_readline():
            await asyncio.sleep(10)
            return b""
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.readline = hang_readline

        db = AsyncMock()
        db.fetchone = AsyncMock(return_value={"repo_path": None})
        budget = AsyncMock()
        progress = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await run_claude_code_task(
                task_row=task_row, db=db, budget=budget, progress=progress,
            )

        assert "timed out" in result["output"]

    @pytest.mark.asyncio
    @patch("backend.services.claude_code_executor._resolve_cmd", return_value=None)
    async def test_cli_not_found_raises(self, mock_resolve):
        """When Claude CLI is not found, raises RuntimeError with clear message."""
        task_row = _make_task_row()
        db = AsyncMock()
        db.fetchone = AsyncMock(return_value={"repo_path": None})
        budget = AsyncMock()
        progress = AsyncMock()

        with pytest.raises(RuntimeError, match="Claude Code CLI not found"):
            await run_claude_code_task(
                task_row=task_row, db=db, budget=budget, progress=progress,
            )
