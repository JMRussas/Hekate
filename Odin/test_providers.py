"""Tests for gods/providers/ — CLI provider abstraction layer.

Every Claude Code CLI flag must be testable through ClaudeCodeConfig.
"""

import json
import os
import pytest
from unittest.mock import patch

from gods.providers.base import CLIProvider, StandardResult, ProviderRegistry
from gods.providers.claude import ClaudeCodeProvider, ClaudeCodeConfig
from gods.providers.gemini import GeminiCLIProvider, GeminiCLIConfig


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
# ClaudeCodeConfig defaults
# ---------------------------------------------------------------------------

class TestClaudeCodeConfig:
    def test_defaults(self):
        cfg = ClaudeCodeConfig()
        assert cfg.model is None
        assert cfg.output_format == "stream-json"
        assert cfg.verbose is True
        assert cfg.no_chrome is True
        assert cfg.dangerously_skip_permissions is False
        assert cfg.max_turns is None
        assert cfg.max_budget_usd is None
        assert cfg.json_schema is None

    def test_custom(self):
        cfg = ClaudeCodeConfig(
            model="opus",
            max_turns=5,
            max_budget_usd=2.0,
            effort="high",
            json_schema={"type": "object", "properties": {"result": {"type": "string"}}},
        )
        assert cfg.model == "opus"
        assert cfg.max_turns == 5
        assert cfg.max_budget_usd == 2.0
        assert cfg.effort == "high"
        assert cfg.json_schema["type"] == "object"


# ---------------------------------------------------------------------------
# ClaudeCodeProvider — build_command flag coverage
# ---------------------------------------------------------------------------

class TestClaudeCodeBuildCommand:
    def test_minimal_command(self):
        p = ClaudeCodeProvider()
        cmd, stdin = p.build_command("Do something", "/tmp")
        assert cmd[0].endswith("claude") or "claude" in cmd[0].lower()
        assert "-p" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd
        assert stdin == "Do something"

    def test_model_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(model="opus"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus"

    def test_fallback_model_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(fallback_model="sonnet"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--fallback-model")
        assert cmd[idx + 1] == "sonnet"

    def test_allowed_tools_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(allowed_tools=["Read", "Edit"]))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--allowedTools" in cmd
        assert "Read" in cmd
        assert "Edit" in cmd

    def test_disallowed_tools_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(disallowed_tools=["Bash(rm *)"]))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--disallowedTools" in cmd
        assert "Bash(rm *)" in cmd

    def test_tools_restrict_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(tools="Bash,Read"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--tools")
        assert cmd[idx + 1] == "Bash,Read"

    def test_system_prompt_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(system_prompt="You are a Python expert"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--system-prompt")
        assert cmd[idx + 1] == "You are a Python expert"

    def test_system_prompt_file_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(system_prompt_file="/path/to/prompt.txt"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--system-prompt-file")
        assert cmd[idx + 1] == "/path/to/prompt.txt"

    def test_append_system_prompt_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(append_system_prompt="Always use TypeScript"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--append-system-prompt")
        assert cmd[idx + 1] == "Always use TypeScript"

    def test_append_system_prompt_file_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(append_system_prompt_file="./rules.txt"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--append-system-prompt-file")
        assert cmd[idx + 1] == "./rules.txt"

    def test_json_schema_flag(self):
        schema = {"type": "object", "properties": {"files": {"type": "array"}}}
        p = ClaudeCodeProvider(ClaudeCodeConfig(json_schema=schema))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--json-schema")
        assert json.loads(cmd[idx + 1]) == schema

    def test_input_format_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(input_format="stream-json"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--input-format")
        assert cmd[idx + 1] == "stream-json"

    def test_max_turns_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(max_turns=5))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--max-turns")
        assert cmd[idx + 1] == "5"

    def test_max_budget_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(max_budget_usd=3.50))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--max-budget-usd")
        assert cmd[idx + 1] == "3.5"

    def test_permission_mode_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(permission_mode="plan"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "plan"

    def test_skip_permissions_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(dangerously_skip_permissions=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--dangerously-skip-permissions" in cmd

    def test_session_id_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(session_id="550e8400-e29b-41d4-a716-446655440000"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--session-id")
        assert cmd[idx + 1] == "550e8400-e29b-41d4-a716-446655440000"

    def test_resume_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(resume="auth-refactor"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "auth-refactor"

    def test_continue_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(continue_session=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--continue" in cmd

    def test_fork_session_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(fork_session=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--fork-session" in cmd

    def test_name_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(name="health-endpoint-task"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--name")
        assert cmd[idx + 1] == "health-endpoint-task"

    def test_no_session_persistence_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(no_session_persistence=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--no-session-persistence" in cmd

    def test_worktree_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(worktree="feature-auth"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--worktree")
        assert cmd[idx + 1] == "feature-auth"

    def test_mcp_config_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(mcp_config="./mcp.json"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--mcp-config")
        assert cmd[idx + 1] == "./mcp.json"

    def test_strict_mcp_config_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(strict_mcp_config=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--strict-mcp-config" in cmd

    def test_add_dirs_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(add_dirs=["../apps", "../lib"]))
        cmd, _ = p.build_command("test", "/tmp")
        # Should have --add-dir ../apps --add-dir ../lib
        indices = [i for i, x in enumerate(cmd) if x == "--add-dir"]
        assert len(indices) == 2
        assert cmd[indices[0] + 1] == "../apps"
        assert cmd[indices[1] + 1] == "../lib"

    def test_effort_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(effort="high"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--effort")
        assert cmd[idx + 1] == "high"

    def test_include_partial_messages_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(include_partial_messages=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--include-partial-messages" in cmd

    def test_no_chrome_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(no_chrome=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--no-chrome" in cmd

    def test_no_chrome_disabled(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(no_chrome=False))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--no-chrome" not in cmd

    def test_debug_flag(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(debug="api,mcp"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--debug")
        assert cmd[idx + 1] == "api,mcp"

    def test_output_format_text(self):
        p = ClaudeCodeProvider(ClaudeCodeConfig(output_format="json", verbose=False))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"
        assert "--verbose" not in cmd

    def test_none_flags_omitted(self):
        """Flags set to None should not appear in command."""
        p = ClaudeCodeProvider(ClaudeCodeConfig())
        cmd, _ = p.build_command("test", "/tmp")
        assert "--model" not in cmd
        assert "--max-turns" not in cmd
        assert "--max-budget-usd" not in cmd
        assert "--system-prompt" not in cmd
        assert "--json-schema" not in cmd
        assert "--worktree" not in cmd
        assert "--effort" not in cmd


# ---------------------------------------------------------------------------
# with_config — task-specific overrides
# ---------------------------------------------------------------------------

class TestWithConfig:
    def test_override_model(self):
        base = ClaudeCodeProvider(ClaudeCodeConfig(model="sonnet"))
        task_provider = base.with_config(model="opus", max_turns=3)
        cmd, _ = task_provider.build_command("test", "/tmp")
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus"
        idx2 = cmd.index("--max-turns")
        assert cmd[idx2 + 1] == "3"

    def test_override_preserves_base(self):
        base = ClaudeCodeProvider(ClaudeCodeConfig(model="sonnet", effort="medium"))
        task_provider = base.with_config(effort="high")
        # Base unchanged
        assert base._config.effort == "medium"
        assert task_provider._config.effort == "high"
        assert task_provider._config.model == "sonnet"  # preserved


# ---------------------------------------------------------------------------
# ClaudeCodeProvider — env and parsing
# ---------------------------------------------------------------------------

class TestClaudeCodeEnvAndParsing:
    def test_build_env_strips_claudecode(self):
        p = ClaudeCodeProvider()
        with patch.dict(os.environ, {"CLAUDECODE": "1", "HOME": "/home/test"}):
            env = p.build_env()
            assert "CLAUDECODE" not in env
            assert "HOME" in env

    def test_parse_output_result_event(self):
        p = ClaudeCodeProvider()
        lines = [
            '{"type":"system","subtype":"init","session_id":"abc","model":"claude-opus-4-6"}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"I created the file."}]}}',
            '{"type":"result","subtype":"success","result":"Done.","total_cost_usd":0.05,"usage":{"input_tokens":100,"output_tokens":50},"modelUsage":{"claude-opus-4-6":{}}}',
        ]
        result = p.parse_output(lines)
        assert "I created the file." in result.output
        assert "Done." in result.output
        assert result.cost_usd == 0.05
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50
        assert result.model == "claude-opus-4-6"

    def test_parse_output_empty(self):
        p = ClaudeCodeProvider()
        result = p.parse_output([])
        assert result.output == ""

    def test_parse_output_tool_events(self):
        p = ClaudeCodeProvider()
        lines = [
            '{"type":"tool_use","tool":"Edit","input":"file.py"}',
            '{"type":"tool_result","output":"File edited"}',
        ]
        result = p.parse_output(lines)
        assert len(result.narration) == 2
        assert result.narration[0]["type"] == "tool_call"
        assert result.narration[1]["type"] == "tool_result"

    def test_parse_error_nested_session(self):
        p = ClaudeCodeProvider()
        assert p.parse_error(1, "Error: Claude Code cannot be launched inside another session.") == "nested_session"

    def test_parse_error_rate_limit(self):
        p = ClaudeCodeProvider()
        assert p.parse_error(1, "rate limit exceeded") == "rate_limit"

    def test_parse_error_unknown(self):
        p = ClaudeCodeProvider()
        err = p.parse_error(42, "something weird happened")
        assert "42" in err

    def test_name(self):
        assert ClaudeCodeProvider().name == "claude_code"


# ---------------------------------------------------------------------------
# GeminiCLIProvider
# ---------------------------------------------------------------------------

class TestGeminiCLIConfig:
    def test_defaults(self):
        cfg = GeminiCLIConfig()
        assert cfg.model is None
        assert cfg.approval_mode == "yolo"
        assert cfg.sandbox is False
        assert cfg.output_format == "text"
        assert cfg.debug is False

    def test_custom(self):
        cfg = GeminiCLIConfig(model="pro", sandbox=True, debug=True)
        assert cfg.model == "pro"
        assert cfg.sandbox is True


class TestGeminiCLIBuildCommand:
    def test_minimal_command(self):
        p = GeminiCLIProvider()
        cmd, stdin = p.build_command("Research this", "/tmp")
        assert "-p" in cmd
        assert "--approval-mode" in cmd
        assert "yolo" in cmd
        assert "-o" in cmd
        assert "text" in cmd
        assert stdin == "Research this"

    def test_model_flag(self):
        p = GeminiCLIProvider(GeminiCLIConfig(model="pro"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "pro"

    def test_model_flash(self):
        p = GeminiCLIProvider(GeminiCLIConfig(model="flash"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "flash"

    def test_sandbox_flag(self):
        p = GeminiCLIProvider(GeminiCLIConfig(sandbox=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--sandbox" in cmd

    def test_approval_mode_default(self):
        p = GeminiCLIProvider(GeminiCLIConfig(approval_mode="default"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--approval-mode")
        assert cmd[idx + 1] == "default"

    def test_approval_mode_auto_edit(self):
        p = GeminiCLIProvider(GeminiCLIConfig(approval_mode="auto_edit"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--approval-mode")
        assert cmd[idx + 1] == "auto_edit"

    def test_output_format_json(self):
        p = GeminiCLIProvider(GeminiCLIConfig(output_format="json"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("-o")
        assert cmd[idx + 1] == "json"

    def test_output_format_stream_json(self):
        p = GeminiCLIProvider(GeminiCLIConfig(output_format="stream-json"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("-o")
        assert cmd[idx + 1] == "stream-json"

    def test_extensions_flag(self):
        p = GeminiCLIProvider(GeminiCLIConfig(extensions=["web_search", "code_exec"]))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--extensions" in cmd
        assert "web_search" in cmd
        assert "code_exec" in cmd

    def test_allowed_mcp_servers_flag(self):
        p = GeminiCLIProvider(GeminiCLIConfig(allowed_mcp_server_names=["server1", "server2"]))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--allowed-mcp-server-names")
        assert cmd[idx + 1] == "server1,server2"

    def test_resume_flag(self):
        p = GeminiCLIProvider(GeminiCLIConfig(resume="latest"))
        cmd, _ = p.build_command("test", "/tmp")
        idx = cmd.index("--resume")
        assert cmd[idx + 1] == "latest"

    def test_include_directories_flag(self):
        p = GeminiCLIProvider(GeminiCLIConfig(include_directories=["../apps", "../lib"]))
        cmd, _ = p.build_command("test", "/tmp")
        indices = [i for i, x in enumerate(cmd) if x == "--include-directories"]
        assert len(indices) == 2

    def test_debug_flag(self):
        p = GeminiCLIProvider(GeminiCLIConfig(debug=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--debug" in cmd

    def test_screen_reader_flag(self):
        p = GeminiCLIProvider(GeminiCLIConfig(screen_reader=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--screen-reader" in cmd

    def test_experimental_acp_flag(self):
        p = GeminiCLIProvider(GeminiCLIConfig(experimental_acp=True))
        cmd, _ = p.build_command("test", "/tmp")
        assert "--experimental-acp" in cmd

    def test_none_flags_omitted(self):
        p = GeminiCLIProvider(GeminiCLIConfig())
        cmd, _ = p.build_command("test", "/tmp")
        assert "--model" not in cmd
        assert "--resume" not in cmd
        assert "--sandbox" not in cmd
        assert "--debug" not in cmd


class TestGeminiCLIEnvAndParsing:
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

    def test_parse_error_auth(self):
        p = GeminiCLIProvider()
        assert p.parse_error(1, "authentication failed") == "auth_error"

    def test_parse_error_quota(self):
        p = GeminiCLIProvider()
        assert p.parse_error(1, "quota exceeded") == "quota_exceeded"

    def test_name(self):
        assert GeminiCLIProvider().name == "gemini_cli"

    def test_with_config(self):
        base = GeminiCLIProvider(GeminiCLIConfig(model="flash"))
        task = base.with_config(model="pro", sandbox=True)
        assert task._config.model == "pro"
        assert task._config.sandbox is True
        assert base._config.model == "flash"  # base unchanged


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
