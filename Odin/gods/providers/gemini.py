"""Gemini CLI provider.

Wraps every Gemini CLI flag as a configurable option.
See: https://geminicli.com/docs/cli/cli-reference/
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from gods.providers.base import CLIProvider, StandardResult

logger = logging.getLogger("gods.providers.gemini")

_ERROR_PATTERNS = {
    "authentication": "auth_error",
    "unauthorized": "auth_error",
    "quota": "quota_exceeded",
    "rate limit": "rate_limit",
    "model not found": "model_not_found",
    "not supported": "model_not_supported",
    "permission denied": "permission_denied",
    "sandbox": "sandbox_error",
}


@dataclass
class GeminiCLIConfig:
    """Configuration for a Gemini CLI invocation.

    Every Gemini CLI flag is represented here. Set to None to omit.
    """
    # Model
    model: str | None = None                      # --model / -m (auto, pro, flash, flash-lite)

    # Execution
    approval_mode: str = "yolo"                   # --approval-mode (default, auto_edit, yolo)
    sandbox: bool = False                         # --sandbox / -s
    yolo: bool = False                            # --yolo / -y (deprecated, use approval_mode)

    # Output
    output_format: str = "text"                   # --output-format / -o (text, json, stream-json)

    # Extensions / Tools
    extensions: list[str] | None = None           # --extensions / -e
    list_extensions: bool = False                 # --list-extensions / -l
    allowed_mcp_server_names: list[str] | None = None  # --allowed-mcp-server-names

    # Session
    resume: str | None = None                     # --resume / -r (latest or session ID)
    prompt_interactive: str | None = None         # --prompt-interactive / -i

    # Directories
    include_directories: list[str] | None = None  # --include-directories

    # Debug
    debug: bool = False                           # --debug / -d
    screen_reader: bool = False                   # --screen-reader

    # Experimental
    experimental_acp: bool = False                # --experimental-acp
    experimental_zed: bool = False                # --experimental-zed-integration


class GeminiCLIProvider(CLIProvider):
    """Gemini CLI provider with full flag support."""

    def __init__(self, config: GeminiCLIConfig | None = None):
        self._config = config or GeminiCLIConfig()
        self._binary = shutil.which("gemini") or "gemini"

    @property
    def name(self) -> str:
        return "gemini_cli"

    def with_config(self, **overrides) -> GeminiCLIProvider:
        """Return a new provider with config overrides."""
        import dataclasses
        new_config = dataclasses.replace(self._config, **overrides)
        p = GeminiCLIProvider(config=new_config)
        p._binary = self._binary
        return p

    def build_command(self, prompt: str, cwd: str) -> tuple[list[str], str]:
        cfg = self._config
        cmd = [self._binary, "-p"]

        # Model
        if cfg.model:
            cmd.extend(["--model", cfg.model])

        # Approval mode
        if cfg.approval_mode:
            cmd.extend(["--approval-mode", cfg.approval_mode])

        # Sandbox
        if cfg.sandbox:
            cmd.append("--sandbox")

        # Yolo (deprecated but still functional)
        if cfg.yolo:
            cmd.append("--yolo")

        # Output format
        if cfg.output_format:
            cmd.extend(["-o", cfg.output_format])

        # Extensions
        if cfg.extensions:
            cmd.extend(["--extensions"] + cfg.extensions)
        if cfg.list_extensions:
            cmd.append("--list-extensions")

        # MCP server names
        if cfg.allowed_mcp_server_names:
            cmd.extend(["--allowed-mcp-server-names", ",".join(cfg.allowed_mcp_server_names)])

        # Session
        if cfg.resume:
            cmd.extend(["--resume", cfg.resume])

        # Directories
        if cfg.include_directories:
            for d in cfg.include_directories:
                cmd.extend(["--include-directories", d])

        # Debug
        if cfg.debug:
            cmd.append("--debug")
        if cfg.screen_reader:
            cmd.append("--screen-reader")

        # Experimental
        if cfg.experimental_acp:
            cmd.append("--experimental-acp")
        if cfg.experimental_zed:
            cmd.append("--experimental-zed-integration")

        return cmd, prompt

    def build_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        # Gemini needs file-based OAuth (not Windows Credential Manager)
        env["GEMINI_FORCE_FILE_STORAGE"] = "true"
        return env

    def parse_output(self, lines: list[str]) -> StandardResult:
        # Gemini outputs plain text by default
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
