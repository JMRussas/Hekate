"""Claude Code CLI provider.

Wraps every Claude Code CLI flag as a configurable option.
See: https://code.claude.com/docs/en/cli
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from gods.providers.base import CLIProvider, StandardResult

logger = logging.getLogger("gods.providers.claude")

# Known error patterns → classification
_ERROR_PATTERNS = {
    "nested session": "nested_session",
    "cannot be launched inside another": "nested_session",
    "rate limit": "rate_limit",
    "rate_limit_exceeded": "rate_limit",
    "authentication": "auth_error",
    "unauthorized": "auth_error",
    "context window": "context_overflow",
    "max.*token": "context_overflow",
    "overloaded": "overloaded",
}


@dataclass
class ClaudeCodeConfig:
    """Configuration for a Claude Code CLI invocation.

    Every Claude Code flag is represented here. Set to None to omit.
    """
    # Model
    model: str | None = None                  # --model (sonnet, opus, or full name)
    fallback_model: str | None = None         # --fallback-model (auto-fallback on overload)

    # Tools
    allowed_tools: list[str] | None = None    # --allowedTools
    disallowed_tools: list[str] | None = None # --disallowedTools
    tools: str | None = None                  # --tools (restrict available tools)

    # System prompt
    system_prompt: str | None = None          # --system-prompt (replaces default)
    system_prompt_file: str | None = None     # --system-prompt-file
    append_system_prompt: str | None = None   # --append-system-prompt (adds to default)
    append_system_prompt_file: str | None = None  # --append-system-prompt-file

    # Output
    output_format: str = "stream-json"        # --output-format (text, json, stream-json)
    json_schema: dict | None = None           # --json-schema (validated JSON output)

    # Input
    input_format: str | None = None           # --input-format (text, stream-json)

    # Limits
    max_turns: int | None = None              # --max-turns
    max_budget_usd: float | None = None       # --max-budget-usd

    # Execution
    permission_mode: str | None = None        # --permission-mode (default, plan, bypassPermissions)
    dangerously_skip_permissions: bool = False # --dangerously-skip-permissions
    verbose: bool = True                      # --verbose

    # Session
    session_id: str | None = None             # --session-id (resume specific session)
    resume: str | None = None                 # --resume (resume by ID or name)
    continue_session: bool = False            # --continue
    fork_session: bool = False                # --fork-session
    name: str | None = None                   # --name (session display name)
    no_session_persistence: bool = False      # --no-session-persistence

    # Git
    worktree: str | None = None              # --worktree (isolated git worktree)

    # MCP
    mcp_config: str | None = None            # --mcp-config (path or JSON string)
    strict_mcp_config: bool = False          # --strict-mcp-config

    # Misc
    add_dirs: list[str] | None = None        # --add-dir (additional working directories)
    effort: str | None = None                # --effort (low, medium, high, max)
    include_partial_messages: bool = False    # --include-partial-messages
    no_chrome: bool = True                   # --no-chrome (disable browser)
    debug: str | None = None                 # --debug (category filtering)


class ClaudeCodeProvider(CLIProvider):
    """Claude Code CLI provider with full flag support."""

    def __init__(self, config: ClaudeCodeConfig | None = None):
        self._config = config or ClaudeCodeConfig(
            # Full tool access — the model should be able to read, write, search, and run commands
            allowed_tools=["Edit", "Write", "Read", "Glob", "Grep", "Bash(*)"],
            # Multi-turn: let the model iterate up to 30 turns (read → plan → execute → self-review)
            max_turns=30,
            # No budget cap — CLI subscription, not API
            max_budget_usd=None,
            # Skip permission prompts — the pipeline is automated
            dangerously_skip_permissions=True,
            # Self-review prompt appended to every session
            append_system_prompt=(
                "After completing your work, review what you did:\n"
                "1. Any gaps? Anything the task asked for that you missed?\n"
                "2. Does your change fit the current architecture and patterns?\n"
                "3. Did you handle edge cases?\n"
                "4. If you wrote code, does it have the right imports and no syntax errors?\n"
                "If you find issues, fix them before finishing. Don't just list problems — fix them."
            ),
        )
        self._binary = shutil.which("claude") or "claude"

    @property
    def name(self) -> str:
        return "claude_code"

    def with_config(self, **overrides) -> ClaudeCodeProvider:
        """Return a new provider with config overrides for a specific task."""
        import dataclasses
        new_config = dataclasses.replace(self._config, **overrides)
        p = ClaudeCodeProvider(config=new_config)
        p._binary = self._binary
        return p

    def build_command(self, prompt: str, cwd: str) -> tuple[list[str], str]:
        cfg = self._config
        cmd = [self._binary, "-p"]

        # Model
        if cfg.model:
            cmd.extend(["--model", cfg.model])
        if cfg.fallback_model:
            cmd.extend(["--fallback-model", cfg.fallback_model])

        # Output format
        if cfg.output_format:
            cmd.extend(["--output-format", cfg.output_format])

        # Input format
        if cfg.input_format:
            cmd.extend(["--input-format", cfg.input_format])

        # Verbose (required for stream-json)
        if cfg.verbose:
            cmd.append("--verbose")

        # JSON schema
        if cfg.json_schema:
            cmd.extend(["--json-schema", json.dumps(cfg.json_schema)])

        # Tools
        if cfg.allowed_tools:
            cmd.extend(["--allowedTools"] + cfg.allowed_tools)
        if cfg.disallowed_tools:
            cmd.extend(["--disallowedTools"] + cfg.disallowed_tools)
        if cfg.tools:
            cmd.extend(["--tools", cfg.tools])

        # System prompt
        if cfg.system_prompt:
            cmd.extend(["--system-prompt", cfg.system_prompt])
        if cfg.system_prompt_file:
            cmd.extend(["--system-prompt-file", cfg.system_prompt_file])
        if cfg.append_system_prompt:
            cmd.extend(["--append-system-prompt", cfg.append_system_prompt])
        if cfg.append_system_prompt_file:
            cmd.extend(["--append-system-prompt-file", cfg.append_system_prompt_file])

        # Limits
        if cfg.max_turns is not None:
            cmd.extend(["--max-turns", str(cfg.max_turns)])
        if cfg.max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(cfg.max_budget_usd)])

        # Permissions
        if cfg.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if cfg.permission_mode:
            cmd.extend(["--permission-mode", cfg.permission_mode])

        # Session
        if cfg.session_id:
            cmd.extend(["--session-id", cfg.session_id])
        if cfg.resume:
            cmd.extend(["--resume", cfg.resume])
        if cfg.continue_session:
            cmd.append("--continue")
        if cfg.fork_session:
            cmd.append("--fork-session")
        if cfg.name:
            cmd.extend(["--name", cfg.name])
        if cfg.no_session_persistence:
            cmd.append("--no-session-persistence")

        # Git worktree
        if cfg.worktree:
            cmd.extend(["--worktree", cfg.worktree])

        # MCP
        if cfg.mcp_config:
            cmd.extend(["--mcp-config", cfg.mcp_config])
        if cfg.strict_mcp_config:
            cmd.append("--strict-mcp-config")

        # Additional dirs
        if cfg.add_dirs:
            for d in cfg.add_dirs:
                cmd.extend(["--add-dir", d])

        # Effort
        if cfg.effort:
            cmd.extend(["--effort", cfg.effort])

        # Partial messages
        if cfg.include_partial_messages:
            cmd.append("--include-partial-messages")

        # Chrome
        if cfg.no_chrome:
            cmd.append("--no-chrome")

        # Debug
        if cfg.debug:
            cmd.extend(["--debug", cfg.debug])

        return cmd, prompt

    def build_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        return env

    def parse_output(self, lines: list[str]) -> StandardResult:
        output_parts: list[str] = []
        narration: list[dict] = []
        affected_files: set[str] = set()
        cost = 0.0
        prompt_tokens = 0
        completion_tokens = 0
        model = ""

        for line in lines:
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                output_parts.append(line)
                continue

            event_type = data.get("type")

            if event_type == "assistant":
                msg = data.get("message", {})
                for content in msg.get("content", []):
                    if content.get("type") == "text":
                        text = content.get("text", "")
                        if text:
                            output_parts.append(text)
                narration.append({
                    "type": "assistant",
                    "text": str(msg.get("content", ""))[:200],
                })

            elif event_type == "result":
                result_text = data.get("result", "")
                if result_text:
                    output_parts.append(result_text)
                cost = data.get("total_cost_usd", 0.0)
                usage = data.get("usage", {})
                prompt_tokens = usage.get("input_tokens", 0)
                completion_tokens = usage.get("output_tokens", 0)
                model_usage = data.get("modelUsage", {})
                if model_usage:
                    model = next(iter(model_usage.keys()), "")

            elif event_type == "system":
                model = data.get("model", model)

            elif event_type == "tool_use":
                tool_name = data.get("tool", "")
                tool_input = data.get("input", {})
                narration.append({
                    "type": "tool_call",
                    "tool": tool_name,
                    "input": str(tool_input)[:200],
                })
                # Extract affected files from Edit/Write tool calls
                if tool_name in ("Edit", "Write") and isinstance(tool_input, dict):
                    fp = tool_input.get("file_path", "")
                    if fp:
                        affected_files.add(fp)

            elif event_type == "tool_result":
                narration.append({
                    "type": "tool_result",
                    "output": str(data.get("output", ""))[:200],
                })

        return StandardResult(
            output="\n".join(output_parts),
            cost_usd=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
            narration=narration,
            affected_files=sorted(affected_files),
        )

    def parse_error(self, exit_code: int, output: str) -> str:
        lower = output.lower()
        for pattern, classification in _ERROR_PATTERNS.items():
            if pattern in lower:
                return classification
        return f"unknown_error_code_{exit_code}"
