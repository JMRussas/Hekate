"""Base classes for CLI provider abstraction.

Each provider encapsulates: binary path, args, env vars, output parsing,
error classification, and health check. Hermes calls provider.execute()
instead of building commands inline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("gods.providers")


@dataclass
class StandardResult:
    """Normalized output from any CLI provider."""
    output: str = ""
    cost_usd: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    narration: list[dict] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)
    exit_code: int = 0

    @property
    def has_output(self) -> bool:
        return bool(self.output and self.output.strip())


class CLIProvider(ABC):
    """Abstract base for CLI providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g. 'claude_code', 'gemini_cli')."""

    @abstractmethod
    def build_command(self, prompt: str, cwd: str) -> tuple[list[str], str]:
        """Build CLI command args and stdin text.

        Returns: (cmd_args, stdin_text)
        """

    @abstractmethod
    def build_env(self) -> dict[str, str]:
        """Build clean environment dict with provider-specific vars."""

    @abstractmethod
    def parse_output(self, lines: list[str]) -> StandardResult:
        """Parse raw CLI output lines into StandardResult."""

    @abstractmethod
    def parse_error(self, exit_code: int, output: str) -> str:
        """Classify error from exit code and output text."""

    def find_binary(self, name: str) -> str | None:
        """Find binary on PATH."""
        return shutil.which(name)

    async def health_check(self) -> bool:
        """Check if provider binary exists and is callable."""
        binary = self.find_binary(self.name.replace("_", "-").replace("cli", "").strip("-") or self.name)
        return binary is not None

    async def execute(
        self,
        prompt: str,
        cwd: str,
        timeout: float = 600,
        on_process: Any = None,
        on_line: Any = None,
    ) -> StandardResult:
        """Full execution: build cmd → launch subprocess → capture → parse.

        Args:
            on_process: Optional callback(proc) invoked after subprocess creation,
                        allowing callers to track the process handle for cleanup.
            on_line: Optional async callback(line_str) invoked for each stdout line
                     as it arrives. Used for real-time narration streaming.

        Override _run_subprocess in tests.
        """
        cmd, stdin_text = self.build_command(prompt, cwd)
        env = self.build_env()

        logger.debug("%s: executing cmd=%s cwd=%s prompt_len=%d",
                     self.name, cmd, cwd, len(prompt))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            limit=10 * 1024 * 1024,
        )

        if on_process is not None:
            on_process(proc)

        proc.stdin.write(stdin_text.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        output_lines: list[str] = []
        # Per-line inactivity timeout: if no output for N seconds, CLI is stuck.
        # First line gets a longer grace period for cold start (CLI init, MCP connect).
        # Subsequent lines use a shorter timeout since the session is active.
        first_line_timeout = min(600.0, timeout)            # 10 min for cold start
        active_line_timeout = max(600.0, timeout / 2)       # at least 10 min — builds/tests run long
        got_first_line = False
        try:
            async with asyncio.timeout(timeout):
                while True:
                    current_timeout = active_line_timeout if got_first_line else first_line_timeout
                    try:
                        line = await asyncio.wait_for(
                            proc.stdout.readline(), timeout=current_timeout,
                        )
                    except asyncio.TimeoutError:
                        label = "active" if got_first_line else "cold start"
                        logger.error("%s: no output for %.0fs (%s), killing",
                                     self.name, current_timeout, label)
                        proc.kill()
                        break
                    if not line:
                        break
                    got_first_line = True
                    decoded = line.decode("utf-8", errors="replace").strip()
                    if decoded:
                        output_lines.append(decoded)
                        if on_line is not None:
                            # Fire-and-forget: don't block readline on relay writes
                            asyncio.ensure_future(on_line(decoded))
        except asyncio.TimeoutError:
            proc.kill()
            logger.error("%s: overall timeout after %.0fs", self.name, timeout)

        await proc.wait()
        logger.debug("%s: exited code=%d lines=%d",
                     self.name, proc.returncode or 0, len(output_lines))

        result = self.parse_output(output_lines)
        result.exit_code = proc.returncode or 0

        if result.exit_code != 0 and not result.has_output:
            error_class = self.parse_error(result.exit_code, "\n".join(output_lines))
            raise RuntimeError(f"{self.name} failed ({error_class}): exit code {result.exit_code}")

        return result


class ProviderRegistry:
    """Registry of available CLI providers."""

    def __init__(self):
        self._providers: dict[str, CLIProvider] = {}

    def register(self, provider: CLIProvider):
        self._providers[provider.name] = provider

    def get(self, name: str) -> CLIProvider | None:
        return self._providers.get(name)

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())

    def register_defaults(self):
        """Register all known providers."""
        from gods.providers.claude import ClaudeCodeProvider
        from gods.providers.gemini import GeminiCLIProvider

        self.register(ClaudeCodeProvider())
        self.register(GeminiCLIProvider())
