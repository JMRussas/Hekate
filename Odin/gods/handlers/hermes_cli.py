"""Shared CLI utilities for hermes execution.

Pure functions — no state, no DB, no async. Used by both
hermes.py (sync) and hermes_async.py.

- build_cli_command: construct CLI args for each provider
- parse_stream_event: parse Claude Code stream-json lines
- check_tdd: heuristic TDD gate check on output text
- CostTracker: accumulates token usage and cost across stream events
"""

from __future__ import annotations

import json
import re
import shutil


# ---------------------------------------------------------------------------
# CLI command building
# ---------------------------------------------------------------------------

def build_cli_command(
    provider: str,
    prompt: str,
    cwd: str,
    allowed_tools: str = "Edit,Write,Read,Glob,Grep,Bash(*)",
) -> tuple[list[str], str]:
    """Build CLI command args and stdin text for a provider.

    Returns (cmd_args, stdin_text).
    """
    if provider == "claude_code":
        claude_bin = shutil.which("claude") or "claude"
        cmd = [
            claude_bin, "-p",
            "--verbose",
            "--output-format", "stream-json",
            "--allowedTools", allowed_tools,
        ]
        return cmd, prompt

    elif provider == "gemini_cli":
        gemini_bin = shutil.which("gemini") or "gemini"
        cmd = [
            gemini_bin, "-p",
            "--approval-mode", "yolo",
            "-o", "text",
        ]
        return cmd, prompt

    elif provider == "codex_cli":
        codex_bin = shutil.which("codex") or "codex"
        cmd = [
            codex_bin, "exec",
            "--dangerously-bypass-approvals-and-sandbox",
        ]
        return cmd, prompt

    elif provider == "ollama":
        raise ValueError("Ollama uses HTTP API, not CLI subprocess")

    else:
        raise ValueError(f"Unknown provider: {provider}")


# ---------------------------------------------------------------------------
# Stream event parsing
# ---------------------------------------------------------------------------

def parse_stream_event(line: str) -> dict | None:
    """Parse a single line of Claude Code stream-json output.

    Returns a normalized event dict or None.
    """
    try:
        data = json.loads(line.strip())
    except (json.JSONDecodeError, ValueError):
        return None

    event_type = data.get("type", "")

    if event_type == "assistant":
        content = data.get("message", {}).get("content", [])
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        if texts:
            return {"type": "narration", "text": " ".join(texts)}

    elif event_type == "tool_use":
        return {
            "type": "tool_call",
            "tool": data.get("name", "unknown"),
            "args": data.get("input", {}),
        }

    elif event_type == "tool_result":
        return {
            "type": "tool_result",
            "tool": data.get("name", "unknown"),
            "output": str(data.get("output", ""))[:500],
        }

    elif event_type == "error":
        return {
            "type": "error",
            "text": data.get("error", {}).get("message", str(data)),
        }

    elif event_type == "result":
        # Final result event with cost and metadata
        return {
            "type": "result",
            "cost_usd": data.get("cost_usd", 0.0),
            "duration_ms": data.get("duration_ms", 0),
            "num_turns": data.get("num_turns", 0),
            "is_error": data.get("is_error", False),
            "result_text": data.get("result", ""),
        }

    elif event_type == "usage":
        return {
            "type": "usage",
            "input_tokens": data.get("input_tokens", 0),
            "output_tokens": data.get("output_tokens", 0),
        }

    return None


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------

class CostTracker:
    """Accumulates token usage and cost across stream events."""

    def __init__(self):
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.cost_usd: float = 0.0
        self.num_turns: int = 0

    def add_usage(self, data: dict):
        """Add token counts from a usage event."""
        self.prompt_tokens += data.get("input_tokens", 0)
        self.completion_tokens += data.get("output_tokens", 0)

    def add_result(self, data: dict):
        """Extract cost from a result event."""
        self.cost_usd = data.get("cost_usd", self.cost_usd)
        self.num_turns = data.get("num_turns", self.num_turns)

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
        }


# ---------------------------------------------------------------------------
# TDD gate (heuristic)
# ---------------------------------------------------------------------------

_TEST_FILE_PATTERN = re.compile(r'test[_/]|_test\.py|\.test\.|spec\.|\.spec\.', re.IGNORECASE)
_TEST_FAIL_PATTERN = re.compile(r'(\d+)\s+(failed|errors?|FAIL)', re.IGNORECASE)
_TEST_PASS_PATTERN = re.compile(r'(tests?\s+pass|all\s+pass|\d+\s+passed|✓|PASSED)', re.IGNORECASE)


def check_tdd(output_text: str, task_type: str) -> dict:
    """Check if TDD was followed for code tasks.

    Returns {passed: bool, reason: str}.
    """
    if task_type not in ("code", "integration", "refactor"):
        return {"passed": True, "reason": "TDD not required for non-code tasks"}

    if not output_text:
        return {"passed": False, "reason": "No output to evaluate"}

    has_tests = bool(_TEST_FILE_PATTERN.search(output_text))
    if not has_tests:
        return {"passed": False,
                "reason": "No test files mentioned in output. TDD requires writing tests first."}

    has_failures = bool(_TEST_FAIL_PATTERN.search(output_text))
    has_passes = bool(_TEST_PASS_PATTERN.search(output_text))

    if has_failures and not has_passes:
        return {"passed": False,
                "reason": "Tests are failing. Implementation must make all tests pass."}

    if has_failures and has_passes:
        return {"passed": False,
                "reason": "Some tests are still failing. All tests must pass."}

    return {"passed": True, "reason": "Tests present and passing"}
