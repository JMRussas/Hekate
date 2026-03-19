"""Tests for gods/providers/ — CLI provider abstraction layer.

RED PHASE: gods/providers/ does not exist yet.

Each provider (claude_code, gemini_cli, codex_cli, ollama) encapsulates:
  - Binary path + required args
  - Environment variables (auth quirks)
  - Output parsing → StandardResult
  - Error classification
  - Health check
"""

import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from gods.providers.base import CLIProvider, StandardResult, ProviderRegistry
from gods.providers.claude import ClaudeCodeProvider
from gods.providers.gemini import GeminiCLIProvider


# ---------------------------------------------------------------------------
# StandardResult
# ---------------------------------------------------------------------------

class TestStandardResult:
    def test_defaults(self):
        r = StandardResult(output="hello")
        assert r.output == "hello"
        assert r.cost_usd == 0.0
        assert r.prompt_tokens == 0
        assert r.completion_tokens == 0
        assert r.model == ""
        assert r.narration == []
        assert r.exit_code == 0

    def test_has_output(self):
        assert StandardResult(output="hello").has_output
        assert not StandardResult(output="").has_output
        assert not StandardResult(output="   ").has_output


# ---------------------------------------------------------------------------
# ClaudeCodeProvider
# ---------------------------------------------------------------------------

class TestClaudeCodeProvider:
    def test_build_command(self):
        p = ClaudeCodeProvider()
        cmd, stdin = p.build_command("Do something", "/tmp/work")
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd
        assert stdin == "Do something"

    def test_build_env_strips_claudecode(self):
        p = ClaudeCodeProvider()
        with patch.dict(os.environ, {"CLAUDECODE": "1", "HOME": "/home/test"}):
            env = p.build_env()
            assert "CLAUDECODE" not in env
            assert "HOME" in env

    def test_parse_output_result_event(self):
        p = ClaudeCodeProvider()
        lines = [
            '{"type":"system","subtype":"init","session_id":"abc"}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"I created the file."}]}}',
            '{"type":"result","subtype":"success","result":"Done.","total_cost_usd":0.05,"usage":{"input_tokens":100,"output_tokens":50}}',
        ]
        result = p.parse_output(lines)
        assert "I created the file." in result.output
        assert "Done." in result.output
        assert result.cost_usd == 0.05
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50

    def test_parse_output_empty(self):
        p = ClaudeCodeProvider()
        result = p.parse_output([])
        assert result.output == ""
        assert result.exit_code == 0

    def test_parse_error_nested_session(self):
        p = ClaudeCodeProvider()
        err = p.parse_error(1, "Error: Claude Code cannot be launched inside another Claude Code session.")
        assert "nested" in err.lower() or "session" in err.lower()

    def test_parse_error_rate_limit(self):
        p = ClaudeCodeProvider()
        err = p.parse_error(1, "Error: rate limit exceeded")
        assert "rate" in err.lower()

    def test_name(self):
        p = ClaudeCodeProvider()
        assert p.name == "claude_code"


# ---------------------------------------------------------------------------
# GeminiCLIProvider
# ---------------------------------------------------------------------------

class TestGeminiCLIProvider:
    def test_build_command(self):
        p = GeminiCLIProvider()
        cmd, stdin = p.build_command("Research this", "/tmp/work")
        assert "-p" in cmd
        assert "--approval-mode" in cmd
        assert "yolo" in cmd
        assert stdin == "Research this"

    def test_build_env_sets_force_file_storage(self):
        p = GeminiCLIProvider()
        env = p.build_env()
        assert env.get("GEMINI_FORCE_FILE_STORAGE") == "true"

    def test_build_env_strips_claudecode(self):
        p = GeminiCLIProvider()
        with patch.dict(os.environ, {"CLAUDECODE": "1"}):
            env = p.build_env()
            assert "CLAUDECODE" not in env

    def test_parse_output_text(self):
        p = GeminiCLIProvider()
        lines = ["The project uses FastAPI.", "Dependencies: fastapi, uvicorn"]
        result = p.parse_output(lines)
        assert "FastAPI" in result.output
        assert "Dependencies" in result.output

    def test_name(self):
        p = GeminiCLIProvider()
        assert p.name == "gemini_cli"


# ---------------------------------------------------------------------------
# ProviderRegistry
# ---------------------------------------------------------------------------

class TestProviderRegistry:
    def test_register_and_get(self):
        reg = ProviderRegistry()
        p = ClaudeCodeProvider()
        reg.register(p)
        assert reg.get("claude_code") is p

    def test_get_missing_returns_none(self):
        reg = ProviderRegistry()
        assert reg.get("nonexistent") is None

    def test_register_all_defaults(self):
        reg = ProviderRegistry()
        reg.register_defaults()
        assert reg.get("claude_code") is not None
        assert reg.get("gemini_cli") is not None

    def test_available_providers(self):
        reg = ProviderRegistry()
        reg.register(ClaudeCodeProvider())
        reg.register(GeminiCLIProvider())
        names = reg.list_providers()
        assert "claude_code" in names
        assert "gemini_cli" in names
