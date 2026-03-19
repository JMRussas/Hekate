"""Claude Code CLI provider."""

from __future__ import annotations

import json
import logging
import os
import shutil
from typing import Any

from gods.providers.base import CLIProvider, StandardResult

logger = logging.getLogger("gods.providers.claude")

# Known error patterns
_ERROR_PATTERNS = {
    "nested session": "nested_session",
    "cannot be launched inside another": "nested_session",
    "rate limit": "rate_limit",
    "rate_limit_exceeded": "rate_limit",
    "authentication": "auth_error",
    "unauthorized": "auth_error",
    "context window": "context_overflow",
    "max.*token": "context_overflow",
}


class ClaudeCodeProvider(CLIProvider):
    """Claude Code CLI (claude -p --output-format stream-json)."""

    def __init__(
        self,
        allowed_tools: str = "Edit,Write,Read,Glob,Grep,Bash(*)",
    ):
        self._allowed_tools = allowed_tools
        self._binary = shutil.which("claude") or "claude"

    @property
    def name(self) -> str:
        return "claude_code"

    def build_command(self, prompt: str, cwd: str) -> tuple[list[str], str]:
        cmd = [
            self._binary, "-p",
            "--verbose",
            "--output-format", "stream-json",
            "--allowedTools", self._allowed_tools,
        ]
        return cmd, prompt

    def build_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        return env

    def parse_output(self, lines: list[str]) -> StandardResult:
        output_parts: list[str] = []
        narration: list[dict] = []
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
                narration.append({"type": "assistant", "text": str(msg.get("content", ""))[:200]})

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

        return StandardResult(
            output="\n".join(output_parts),
            cost_usd=cost,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
            narration=narration,
        )

    def parse_error(self, exit_code: int, output: str) -> str:
        lower = output.lower()
        for pattern, classification in _ERROR_PATTERNS.items():
            if pattern in lower:
                return classification
        return f"unknown_error_code_{exit_code}"
