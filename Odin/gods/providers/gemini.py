"""Gemini CLI provider."""

from __future__ import annotations

import logging
import os
import shutil

from gods.providers.base import CLIProvider, StandardResult

logger = logging.getLogger("gods.providers.gemini")

_ERROR_PATTERNS = {
    "authentication": "auth_error",
    "unauthorized": "auth_error",
    "quota": "quota_exceeded",
    "rate limit": "rate_limit",
    "model not found": "model_not_found",
    "not supported": "model_not_supported",
}


class GeminiCLIProvider(CLIProvider):
    """Gemini CLI (gemini -p --approval-mode yolo -o text)."""

    def __init__(self):
        self._binary = shutil.which("gemini") or "gemini"

    @property
    def name(self) -> str:
        return "gemini_cli"

    def build_command(self, prompt: str, cwd: str) -> tuple[list[str], str]:
        cmd = [
            self._binary, "-p",
            "--approval-mode", "yolo",
            "-o", "text",
        ]
        return cmd, prompt

    def build_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        # Gemini needs file-based OAuth (not Windows Credential Manager)
        env["GEMINI_FORCE_FILE_STORAGE"] = "true"
        return env

    def parse_output(self, lines: list[str]) -> StandardResult:
        # Gemini outputs plain text
        return StandardResult(
            output="\n".join(lines),
            model="gemini",
        )

    def parse_error(self, exit_code: int, output: str) -> str:
        lower = output.lower()
        for pattern, classification in _ERROR_PATTERNS.items():
            if pattern in lower:
                return classification
        return f"unknown_error_code_{exit_code}"
