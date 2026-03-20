#  Orchestration Engine - CLI Provider
#
#  Unified, typed Python API for all external CLI providers (Claude Code,
#  Gemini CLI, Codex CLI). Single source of truth for command construction,
#  environment setup, stream parsing, and model configuration.
#
#  Usable by both single-shot executors (executor.py, generic_cli_executor.py)
#  and conversational tree runners (claude_agent.py, future session-based flows).
#
#  Depends on: config.py
#  Used by:    services/claude_code_executor.py, services/generic_cli_executor.py,
#              services/claude_agent.py (future migration)

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple, Union

from backend.config import cfg

logger = logging.getLogger("orchestration.cli_provider")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ProviderName(str, Enum):
    """Canonical provider identifiers."""
    CLAUDE_CODE = "claude_code"
    GEMINI_CLI = "gemini_cli"
    CODEX_CLI = "codex_cli"


class ExecutionMode(str, Enum):
    """CLI execution modes."""
    SINGLE_SHOT = "single_shot"     # One prompt in, one result out
    SESSION_START = "session_start"  # Begin a named conversation
    SESSION_RESUME = "session_resume"  # Continue an existing conversation
    SESSION_CONTINUE = "session_continue"  # Continue most recent in cwd


class OutputFormat(str, Enum):
    """CLI output format options."""
    TEXT = "text"
    JSON = "json"
    STREAM_JSON = "stream-json"


class PermissionMode(str, Enum):
    """Claude Code permission modes."""
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS = "bypassPermissions"
    DONT_ASK = "dontAsk"
    PLAN = "plan"
    AUTO = "auto"


class ApprovalMode(str, Enum):
    """Gemini CLI approval modes."""
    DEFAULT = "default"
    AUTO_EDIT = "auto_edit"
    YOLO = "yolo"
    PLAN = "plan"


class Effort(str, Enum):
    """Claude Code effort levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StreamEventType(str, Enum):
    """Normalized stream event types across providers."""
    ASSISTANT = "assistant"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    RESULT = "result"
    ERROR = "error"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderCapabilities:
    """Per-provider capability flags.

    Describes what a CLI provider supports at the protocol level.
    Individual models may further restrict capabilities (see ModelConfig).
    """
    supports_sessions: bool = False
    supports_stream_json: bool = False
    supports_mcp: bool = False
    supports_tools: bool = False
    supports_structured_output: bool = False
    supports_effort: bool = False
    supports_worktree: bool = False
    supports_agents: bool = False
    supports_fallback_model: bool = False
    supports_budget_cap: bool = False
    supports_system_prompt: bool = False
    supports_permission_modes: bool = False
    max_context_chars: int = 200_000


@dataclass
class ModelConfig:
    """Per-model behavioral configuration.

    Each model within a provider can have distinct settings for cost,
    timeout, capability flags, and defaults.
    """
    model_id: str
    provider: str
    effort: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: int = 16384
    cost_per_mtok_input: float = 0.0
    cost_per_mtok_output: float = 0.0
    supports_tools: bool = True
    fallback_model: Optional[str] = None
    timeout_seconds: int = 600
    session_capable: bool = False

    @property
    def is_free(self) -> bool:
        """True if this model has zero API cost (subscription/local)."""
        return self.cost_per_mtok_input == 0.0 and self.cost_per_mtok_output == 0.0


@dataclass
class ProviderConfig:
    """Per-provider configuration.

    Holds the resolved binary path, capability flags, model registry,
    environment overrides, and default CLI flags.
    """
    name: str
    binary: str  # Resolved command path (or raw name for deferred resolution)
    capabilities: ProviderCapabilities
    models: Dict[str, ModelConfig] = field(default_factory=dict)
    default_model: str = ""
    env_overrides: Dict[str, str] = field(default_factory=dict)
    env_strip: Set[str] = field(default_factory=set)
    auth_file: str = ""
    allowed_tools: str = ""
    permission_mode: str = ""
    default_flags: List[str] = field(default_factory=list)
    enabled: bool = True

    def get_model(self, model_id: Optional[str] = None) -> ModelConfig:
        """Get a ModelConfig by ID or alias, falling back to default."""
        key = model_id or self.default_model
        if key in self.models:
            return self.models[key]
        # Try matching by alias (e.g. "sonnet" -> "claude-sonnet-4-6")
        for cfg_model in self.models.values():
            if cfg_model.model_id == key:
                return cfg_model
        # Return a synthetic config if we don't know the model
        logger.warning("Unknown model '%s' for provider '%s', using defaults", key, self.name)
        return ModelConfig(model_id=key, provider=self.name)


@dataclass
class StreamEvent:
    """Normalized stream event from any CLI provider."""
    type: StreamEventType
    raw_type: str  # Original event type string from the provider
    content: str = ""
    tool_name: str = ""
    tool_input: Optional[Dict[str, Any]] = None
    tool_result: Optional[str] = None
    cost_usd: float = 0.0
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Optional[Dict[str, Any]] = None


@dataclass
class ExecutionResult:
    """Result from a CLI execution."""
    output: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    model_used: str = ""
    session_id: Optional[str] = None
    exit_code: int = 0
    stderr: str = ""
    events: List[StreamEvent] = field(default_factory=list)
    timed_out: bool = False

    @property
    def success(self) -> bool:
        """True if execution completed without error."""
        return self.exit_code == 0 and not self.timed_out and bool(self.output.strip())


# ---------------------------------------------------------------------------
# Binary resolution
# ---------------------------------------------------------------------------

def _resolve_binary(name: str) -> str:
    """Resolve a CLI binary via shutil.which with npm global fallback.

    On Windows, checks the npm global bin directory for .cmd/.exe variants
    when shutil.which fails (common when PATH doesn't include npm global).
    """
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if sys.platform == "win32":
        npm_bin = os.path.join(os.environ.get("APPDATA", ""), "npm")
        for ext in (".cmd", ".exe", ""):
            candidate = os.path.join(npm_bin, "{}{}".format(name, ext))
            if os.path.isfile(candidate):
                return candidate
    return name  # Return raw name — will fail at subprocess exec time


# ---------------------------------------------------------------------------
# Pre-configured model registries
# ---------------------------------------------------------------------------

def _build_claude_models() -> Dict[str, ModelConfig]:
    """Build Claude Code model configurations."""
    models = {}

    # Primary models
    models["claude-sonnet-4-6"] = ModelConfig(
        model_id="claude-sonnet-4-6",
        provider=ProviderName.CLAUDE_CODE,
        effort="high",
        max_tokens=16384,
        cost_per_mtok_input=3.0,
        cost_per_mtok_output=15.0,
        supports_tools=True,
        fallback_model="claude-haiku-4-5-20251001",
        timeout_seconds=int(cfg("claude_code.timeout_seconds", 600)),
        session_capable=True,
    )

    models["claude-opus-4-6"] = ModelConfig(
        model_id="claude-opus-4-6",
        provider=ProviderName.CLAUDE_CODE,
        effort="high",
        max_tokens=16384,
        cost_per_mtok_input=15.0,
        cost_per_mtok_output=75.0,
        supports_tools=True,
        fallback_model="claude-sonnet-4-6",
        timeout_seconds=int(cfg("claude_code.timeout_seconds", 900)),
        session_capable=True,
    )

    models["claude-haiku-4-5-20251001"] = ModelConfig(
        model_id="claude-haiku-4-5-20251001",
        provider=ProviderName.CLAUDE_CODE,
        effort="medium",
        max_tokens=8192,
        cost_per_mtok_input=0.80,
        cost_per_mtok_output=4.0,
        supports_tools=True,
        fallback_model=None,
        timeout_seconds=int(cfg("claude_code.timeout_seconds", 300)),
        session_capable=True,
    )

    # Aliases — point to the same ModelConfig
    models["sonnet"] = models["claude-sonnet-4-6"]
    models["opus"] = models["claude-opus-4-6"]
    models["haiku"] = models["claude-haiku-4-5-20251001"]

    return models


def _build_gemini_models() -> Dict[str, ModelConfig]:
    """Build Gemini CLI model configurations."""
    models = {}

    models["gemini-2.5-pro"] = ModelConfig(
        model_id="gemini-2.5-pro",
        provider=ProviderName.GEMINI_CLI,
        max_tokens=65536,
        cost_per_mtok_input=1.25,
        cost_per_mtok_output=10.0,
        supports_tools=True,
        fallback_model="gemini-2.5-flash",
        timeout_seconds=int(cfg("cli_executor.timeout_seconds", 600)),
        session_capable=True,
    )

    models["gemini-2.5-flash"] = ModelConfig(
        model_id="gemini-2.5-flash",
        provider=ProviderName.GEMINI_CLI,
        max_tokens=65536,
        cost_per_mtok_input=0.15,
        cost_per_mtok_output=0.60,
        supports_tools=True,
        fallback_model="gemini-2.0-flash",
        timeout_seconds=int(cfg("cli_executor.timeout_seconds", 600)),
        session_capable=True,
    )

    models["gemini-2.0-flash"] = ModelConfig(
        model_id="gemini-2.0-flash",
        provider=ProviderName.GEMINI_CLI,
        max_tokens=8192,
        cost_per_mtok_input=0.10,
        cost_per_mtok_output=0.40,
        supports_tools=True,
        fallback_model=None,
        timeout_seconds=int(cfg("cli_executor.timeout_seconds", 600)),
        session_capable=True,
    )

    return models


def _build_codex_models() -> Dict[str, ModelConfig]:
    """Build Codex CLI model configurations."""
    models = {}

    models["gpt-5.2-codex"] = ModelConfig(
        model_id="gpt-5.2-codex",
        provider=ProviderName.CODEX_CLI,
        max_tokens=16384,
        cost_per_mtok_input=0.0,  # Subscription-billed
        cost_per_mtok_output=0.0,
        supports_tools=True,
        fallback_model=None,
        timeout_seconds=int(cfg("cli_executor.timeout_seconds", 600)),
        session_capable=False,
    )

    return models


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

# Default allowed tools for Claude Code in headless mode.
# Without this, -p mode is read-only and silently fails on writes.
_CLAUDE_DEFAULT_ALLOWED_TOOLS = cfg(
    "claude_code.allowed_tools",
    "Edit,Write,Read,Glob,Grep,"
    "Bash(git *),Bash(dotnet build *),Bash(dotnet test *),Bash(dotnet publish *),"
    "Bash(dotnet run *),Bash(npm *),Bash(python *),Bash(curl *),Bash(ls *),Bash(find *),"
    "mcp__hekate__*,mcp__ollama__*",
)

# Env vars that cause Claude Code to detect a nested session — must be stripped.
_CLAUDE_NESTED_SESSION_VARS = {
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRY_POINT",
    "CLAUDE_CODE_PARENT_SESSION_ID",
}

# Home directory for auth file resolution.
_HOME = os.environ.get("USERPROFILE", os.environ.get("HOME", ""))


def _build_provider_configs() -> Dict[str, ProviderConfig]:
    """Build the complete provider registry."""
    providers = {}

    # -----------------------------------------------------------------------
    # Claude Code CLI v2.1.71
    # -----------------------------------------------------------------------
    providers["claude_code"] = ProviderConfig(
        name="claude_code",
        binary=_resolve_binary("claude"),
        capabilities=ProviderCapabilities(
            supports_sessions=True,
            supports_stream_json=True,
            supports_mcp=True,
            supports_tools=True,
            supports_structured_output=True,
            supports_effort=True,
            supports_worktree=True,
            supports_agents=True,
            supports_fallback_model=True,
            supports_budget_cap=True,
            supports_system_prompt=True,
            supports_permission_modes=True,
            max_context_chars=1_000_000,
        ),
        models=_build_claude_models(),
        default_model=cfg("claude_code.default_model", "claude-sonnet-4-6"),
        env_overrides={
            "HOME": os.environ.get("HOME", _HOME),
            "USERPROFILE": os.environ.get("USERPROFILE", _HOME),
            "APPDATA": os.environ.get("APPDATA", os.path.join(_HOME, "AppData", "Roaming")),
        },
        env_strip=_CLAUDE_NESTED_SESSION_VARS,
        auth_file=os.path.join(_HOME, ".claude", ".credentials.json"),
        allowed_tools=_CLAUDE_DEFAULT_ALLOWED_TOOLS,
        permission_mode="",  # No default — use provider's default unless overridden
        default_flags=["--verbose"],
        enabled=True,
    )

    # -----------------------------------------------------------------------
    # Gemini CLI v0.33.1
    # -----------------------------------------------------------------------
    providers["gemini_cli"] = ProviderConfig(
        name="gemini_cli",
        binary=_resolve_binary("gemini"),
        capabilities=ProviderCapabilities(
            supports_sessions=True,
            supports_stream_json=True,
            supports_mcp=True,
            supports_tools=True,
            supports_structured_output=False,
            supports_effort=False,
            supports_worktree=False,
            supports_agents=False,
            supports_fallback_model=False,
            supports_budget_cap=False,
            supports_system_prompt=False,
            supports_permission_modes=True,
            max_context_chars=1_000_000,
        ),
        models=_build_gemini_models(),
        default_model=cfg("gemini_cli.model", "gemini-2.5-pro"),
        env_overrides={
            "GEMINI_FORCE_FILE_STORAGE": "true",
            "HOME": os.environ.get("HOME", _HOME),
            "USERPROFILE": os.environ.get("USERPROFILE", _HOME),
        },
        env_strip=set(),
        auth_file=os.path.join(_HOME, ".gemini", "oauth_creds.json"),
        allowed_tools="",  # Gemini uses policy engine, not --allowed-tools
        permission_mode="yolo",
        default_flags=[],
        enabled=True,
    )

    # -----------------------------------------------------------------------
    # Codex CLI (disabled — low quota)
    # -----------------------------------------------------------------------
    providers["codex_cli"] = ProviderConfig(
        name="codex_cli",
        binary=_resolve_binary("codex"),
        capabilities=ProviderCapabilities(
            supports_sessions=False,
            supports_stream_json=False,
            supports_mcp=False,
            supports_tools=True,
            supports_structured_output=False,
            supports_effort=False,
            supports_worktree=False,
            supports_agents=False,
            supports_fallback_model=False,
            supports_budget_cap=False,
            supports_system_prompt=False,
            supports_permission_modes=False,
            max_context_chars=128_000,
        ),
        models=_build_codex_models(),
        default_model="gpt-5.2-codex",
        env_overrides={},
        env_strip=set(),
        auth_file=os.path.join(_HOME, ".codex", "auth.json"),
        allowed_tools="",
        permission_mode="",
        default_flags=[],
        enabled=False,  # Disabled — low quota on ChatGPT subscription
    )

    return providers


# Materialized at import time — one lookup per call.
PROVIDERS = _build_provider_configs()


def get_provider_config(provider: str) -> ProviderConfig:
    """Get a provider config by name. Raises ValueError if unknown."""
    if provider not in PROVIDERS:
        raise ValueError(
            "Unknown provider '{}'. Available: {}".format(
                provider, ", ".join(PROVIDERS.keys())
            )
        )
    return PROVIDERS[provider]


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

class CommandBuilder:
    """Builds CLI command arguments for a specific provider and mode.

    Translates typed Python parameters into the exact flags each CLI expects.
    This is the core mapping layer — every flag for every provider is here.
    """

    def __init__(self, config: ProviderConfig):
        self.config = config

    def build(
        self,
        mode: ExecutionMode,
        *,
        model: Optional[str] = None,
        output_format: OutputFormat = OutputFormat.STREAM_JSON,
        input_format: Optional[str] = None,
        session_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        append_system_prompt: Optional[str] = None,
        allowed_tools: Optional[str] = None,
        disallowed_tools: Optional[str] = None,
        tools: Optional[str] = None,
        permission_mode: Optional[str] = None,
        approval_mode: Optional[str] = None,
        effort: Optional[str] = None,
        max_budget_usd: Optional[float] = None,
        mcp_config: Optional[List[str]] = None,
        strict_mcp_config: bool = False,
        add_dirs: Optional[List[str]] = None,
        include_dirs: Optional[List[str]] = None,
        agent: Optional[str] = None,
        agents_json: Optional[str] = None,
        json_schema: Optional[str] = None,
        worktree: Optional[Union[bool, str]] = None,
        fork_session: bool = False,
        fallback_model: Optional[str] = None,
        no_session_persistence: bool = False,
        include_partial_messages: bool = False,
        replay_user_messages: bool = False,
        betas: Optional[str] = None,
        settings: Optional[str] = None,
        setting_sources: Optional[str] = None,
        plugin_dir: Optional[List[str]] = None,
        disable_slash_commands: bool = False,
        file_specs: Optional[List[str]] = None,
        sandbox: bool = False,
        raw_output: bool = False,
        acp: bool = False,
        policy_files: Optional[List[str]] = None,
        extensions: Optional[List[str]] = None,
        allowed_mcp_servers: Optional[List[str]] = None,
        verbose: bool = False,
        debug: Optional[str] = None,
        extra_flags: Optional[List[str]] = None,
    ) -> List[str]:
        """Build the complete command argument list.

        Provider-specific logic routes to the appropriate builder method.
        """
        provider = self.config.name

        if provider == ProviderName.CLAUDE_CODE or provider == "claude_code":
            return self._build_claude(
                mode=mode, model=model, output_format=output_format,
                input_format=input_format, session_id=session_id,
                system_prompt=system_prompt, append_system_prompt=append_system_prompt,
                allowed_tools=allowed_tools, disallowed_tools=disallowed_tools,
                tools=tools, permission_mode=permission_mode, effort=effort,
                max_budget_usd=max_budget_usd, mcp_config=mcp_config,
                strict_mcp_config=strict_mcp_config, add_dirs=add_dirs,
                agent=agent, agents_json=agents_json, json_schema=json_schema,
                worktree=worktree, fork_session=fork_session,
                fallback_model=fallback_model,
                no_session_persistence=no_session_persistence,
                include_partial_messages=include_partial_messages,
                replay_user_messages=replay_user_messages, betas=betas,
                settings=settings, setting_sources=setting_sources,
                plugin_dir=plugin_dir, disable_slash_commands=disable_slash_commands,
                file_specs=file_specs, verbose=verbose, debug=debug,
                extra_flags=extra_flags,
            )

        elif provider == ProviderName.GEMINI_CLI or provider == "gemini_cli":
            return self._build_gemini(
                mode=mode, model=model, output_format=output_format,
                session_id=session_id, approval_mode=approval_mode,
                sandbox=sandbox, raw_output=raw_output, acp=acp,
                policy_files=policy_files, extensions=extensions,
                include_dirs=include_dirs,
                allowed_mcp_servers=allowed_mcp_servers,
                verbose=verbose, extra_flags=extra_flags,
            )

        elif provider == ProviderName.CODEX_CLI or provider == "codex_cli":
            return self._build_codex(
                model=model, extra_flags=extra_flags,
            )

        else:
            raise ValueError("Unknown provider: {}".format(provider))

    # -------------------------------------------------------------------
    # Claude Code
    # -------------------------------------------------------------------

    def _build_claude(
        self,
        mode: ExecutionMode,
        model: Optional[str],
        output_format: OutputFormat,
        input_format: Optional[str],
        session_id: Optional[str],
        system_prompt: Optional[str],
        append_system_prompt: Optional[str],
        allowed_tools: Optional[str],
        disallowed_tools: Optional[str],
        tools: Optional[str],
        permission_mode: Optional[str],
        effort: Optional[str],
        max_budget_usd: Optional[float],
        mcp_config: Optional[List[str]],
        strict_mcp_config: bool,
        add_dirs: Optional[List[str]],
        agent: Optional[str],
        agents_json: Optional[str],
        json_schema: Optional[str],
        worktree: Optional[Union[bool, str]],
        fork_session: bool,
        fallback_model: Optional[str],
        no_session_persistence: bool,
        include_partial_messages: bool,
        replay_user_messages: bool,
        betas: Optional[str],
        settings: Optional[str],
        setting_sources: Optional[str],
        plugin_dir: Optional[List[str]],
        disable_slash_commands: bool,
        file_specs: Optional[List[str]],
        verbose: bool,
        debug: Optional[str],
        extra_flags: Optional[List[str]],
    ) -> List[str]:
        """Build Claude Code CLI arguments."""
        args = [self.config.binary, "-p"]

        # Default flags from config (e.g. --verbose)
        args.extend(self.config.default_flags)

        # Output format
        args.extend(["--output-format", output_format.value
                      if isinstance(output_format, OutputFormat) else output_format])

        # Input format
        if input_format:
            args.extend(["--input-format", input_format])

        # Session management
        if mode == ExecutionMode.SESSION_START and session_id:
            args.extend(["--session-id", session_id])
        elif mode == ExecutionMode.SESSION_RESUME and session_id:
            args.extend(["--resume", session_id])
        elif mode == ExecutionMode.SESSION_CONTINUE:
            args.append("--continue")

        # Model
        if model:
            args.extend(["--model", model])

        # Tool access control
        effective_tools = allowed_tools if allowed_tools is not None else self.config.allowed_tools
        if effective_tools:
            args.extend(["--allowedTools", effective_tools])
        if disallowed_tools:
            args.extend(["--disallowedTools", disallowed_tools])
        if tools is not None:
            args.extend(["--tools", tools])

        # System prompt
        if system_prompt:
            args.extend(["--system-prompt", system_prompt])
        if append_system_prompt:
            args.extend(["--append-system-prompt", append_system_prompt])

        # Permission mode
        effective_permission = permission_mode or self.config.permission_mode
        if effective_permission:
            args.extend(["--permission-mode", effective_permission])

        # Effort
        if effort:
            args.extend(["--effort", effort])

        # Budget cap
        if max_budget_usd is not None:
            args.extend(["--max-budget-usd", str(max_budget_usd)])

        # MCP configuration
        if mcp_config:
            args.extend(["--mcp-config", ",".join(mcp_config)])
        if strict_mcp_config:
            args.append("--strict-mcp-config")

        # Additional directories
        if add_dirs:
            args.extend(["--add-dir", ",".join(add_dirs)])

        # Agents
        if agent:
            args.extend(["--agent", agent])
        if agents_json:
            args.extend(["--agents", agents_json])

        # Structured output
        if json_schema:
            args.extend(["--json-schema", json_schema])

        # Worktree
        if worktree is True:
            args.append("--worktree")
        elif isinstance(worktree, str):
            args.extend(["--worktree", worktree])

        # Session flags
        if fork_session:
            args.append("--fork-session")
        if no_session_persistence:
            args.append("--no-session-persistence")

        # Streaming flags
        if include_partial_messages:
            args.append("--include-partial-messages")
        if replay_user_messages:
            args.append("--replay-user-messages")

        # Fallback model
        if fallback_model:
            args.extend(["--fallback-model", fallback_model])

        # Betas
        if betas:
            args.extend(["--betas", betas])

        # Settings
        if settings:
            args.extend(["--settings", settings])
        if setting_sources:
            args.extend(["--setting-sources", setting_sources])

        # Plugins
        if plugin_dir:
            args.extend(["--plugin-dir", ",".join(plugin_dir)])

        # Slash commands
        if disable_slash_commands:
            args.append("--disable-slash-commands")

        # File resources
        if file_specs:
            for spec in file_specs:
                args.extend(["--file", spec])

        # Debug/verbose (--verbose already added via default_flags if configured)
        if verbose and "--verbose" not in self.config.default_flags:
            args.append("--verbose")
        if debug is not None:
            args.extend(["--debug", debug] if debug else ["--debug"])

        # Extra passthrough flags
        if extra_flags:
            args.extend(extra_flags)

        return args

    # -------------------------------------------------------------------
    # Gemini CLI
    # -------------------------------------------------------------------

    def _build_gemini(
        self,
        mode: ExecutionMode,
        model: Optional[str],
        output_format: OutputFormat,
        session_id: Optional[str],
        approval_mode: Optional[str],
        sandbox: bool,
        raw_output: bool,
        acp: bool,
        policy_files: Optional[List[str]],
        extensions: Optional[List[str]],
        include_dirs: Optional[List[str]],
        allowed_mcp_servers: Optional[List[str]],
        verbose: bool,
        extra_flags: Optional[List[str]],
    ) -> List[str]:
        """Build Gemini CLI arguments."""
        args = [self.config.binary]

        # Execution mode
        if mode == ExecutionMode.SESSION_RESUME:
            args.extend(["--resume", session_id or "latest"])
        else:
            # -p for non-interactive (single-shot and session start)
            args.append("-p")
            # Gemini -p takes the prompt as next positional arg.
            # We pass an empty string here; actual prompt goes via stdin.
            args.append("")

        # Default flags
        args.extend(self.config.default_flags)

        # Output format
        args.extend(["-o", output_format.value
                     if isinstance(output_format, OutputFormat) else output_format])

        # Model
        if model:
            args.extend(["-m", model])

        # Approval mode
        effective_approval = approval_mode or self.config.permission_mode
        if effective_approval:
            if effective_approval == "yolo":
                args.extend(["--approval-mode", "yolo"])
            else:
                args.extend(["--approval-mode", effective_approval])

        # Sandbox
        if sandbox:
            args.append("--sandbox")

        # Raw output
        if raw_output:
            args.append("--raw-output")

        # ACP mode
        if acp:
            args.append("--acp")

        # Policy files
        if policy_files:
            args.extend(["--policy", ",".join(policy_files)])

        # Extensions
        if extensions:
            args.extend(["-e", ",".join(extensions)])

        # Include directories
        if include_dirs:
            args.extend(["--include-directories", ",".join(include_dirs)])

        # Allowed MCP servers
        if allowed_mcp_servers:
            args.extend(["--allowed-mcp-server-names", ",".join(allowed_mcp_servers)])

        # Extra passthrough flags
        if extra_flags:
            args.extend(extra_flags)

        return args

    # -------------------------------------------------------------------
    # Codex CLI
    # -------------------------------------------------------------------

    def _build_codex(
        self,
        model: Optional[str],
        extra_flags: Optional[List[str]],
    ) -> List[str]:
        """Build Codex CLI arguments.

        Codex is simple — just exec with bypass and optional model.
        """
        args = [self.config.binary, "exec", "--dangerously-bypass-approvals-and-sandbox"]

        if model:
            args.extend(["--model", model])

        if extra_flags:
            args.extend(extra_flags)

        return args


# ---------------------------------------------------------------------------
# Environment builder
# ---------------------------------------------------------------------------

def build_env(config: ProviderConfig) -> Dict[str, str]:
    """Build the subprocess environment for a provider.

    1. Starts from current process env
    2. Strips vars listed in env_strip (e.g., nested session detection)
    3. Applies env_overrides (e.g., HOME, GEMINI_FORCE_FILE_STORAGE)
    """
    env = {k: v for k, v in os.environ.items() if k not in config.env_strip}
    env.update(config.env_overrides)
    return env


# ---------------------------------------------------------------------------
# Stream event parser
# ---------------------------------------------------------------------------

def _normalize_claude_event(raw: Dict[str, Any]) -> StreamEvent:
    """Normalize a Claude Code stream-json event."""
    event_type = raw.get("type", "")

    if event_type == "assistant":
        # Extract text from content blocks
        message = raw.get("message", {})
        content_blocks = message.get("content", []) if isinstance(message, dict) else []
        text_parts = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)
        return StreamEvent(
            type=StreamEventType.ASSISTANT,
            raw_type=event_type,
            content="\n".join(text_parts),
            raw=raw,
        )

    elif event_type == "tool_use":
        return StreamEvent(
            type=StreamEventType.TOOL_USE,
            raw_type=event_type,
            tool_name=raw.get("name", raw.get("tool", "unknown")),
            tool_input=raw.get("input"),
            raw=raw,
        )

    elif event_type == "tool_result":
        return StreamEvent(
            type=StreamEventType.TOOL_RESULT,
            raw_type=event_type,
            tool_name=raw.get("name", raw.get("tool", "")),
            tool_result=str(raw.get("output", raw.get("content", "")))[:1000],
            raw=raw,
        )

    elif event_type == "result":
        result_data = raw.get("result", "")
        content = result_data if isinstance(result_data, str) else ""
        usage = raw.get("usage", {})
        return StreamEvent(
            type=StreamEventType.RESULT,
            raw_type=event_type,
            content=content,
            cost_usd=raw.get("total_cost", 0.0) or 0.0,
            model=raw.get("model", ""),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            raw=raw,
        )

    elif event_type == "error":
        error = raw.get("error", {})
        if isinstance(error, dict):
            msg = error.get("message", str(error))
        else:
            msg = str(error)
        return StreamEvent(
            type=StreamEventType.ERROR,
            raw_type=event_type,
            content=msg,
            raw=raw,
        )

    else:
        return StreamEvent(
            type=StreamEventType.UNKNOWN,
            raw_type=event_type,
            raw=raw,
        )


def _normalize_gemini_event(raw: Dict[str, Any]) -> StreamEvent:
    """Normalize a Gemini CLI stream-json event.

    Gemini stream-json format is similar to Claude's but with some differences
    in field names. This normalizer handles both known and unknown event shapes.
    """
    event_type = raw.get("type", raw.get("event", ""))

    if event_type in ("assistant", "message", "text"):
        content = raw.get("content", raw.get("text", raw.get("message", "")))
        if isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            content = "\n".join(text_parts)
        return StreamEvent(
            type=StreamEventType.ASSISTANT,
            raw_type=event_type,
            content=str(content),
            raw=raw,
        )

    elif event_type in ("tool_use", "tool_call", "function_call"):
        return StreamEvent(
            type=StreamEventType.TOOL_USE,
            raw_type=event_type,
            tool_name=raw.get("name", raw.get("tool", raw.get("function", "unknown"))),
            tool_input=raw.get("input", raw.get("args", raw.get("arguments"))),
            raw=raw,
        )

    elif event_type in ("tool_result", "tool_response", "function_response"):
        return StreamEvent(
            type=StreamEventType.TOOL_RESULT,
            raw_type=event_type,
            tool_name=raw.get("name", raw.get("tool", "")),
            tool_result=str(raw.get("output", raw.get("content", raw.get("result", ""))))[:1000],
            raw=raw,
        )

    elif event_type in ("result", "done", "final"):
        usage = raw.get("usage", raw.get("usageMetadata", {}))
        return StreamEvent(
            type=StreamEventType.RESULT,
            raw_type=event_type,
            content=str(raw.get("result", raw.get("content", raw.get("text", "")))),
            cost_usd=raw.get("total_cost", 0.0) or 0.0,
            model=raw.get("model", ""),
            input_tokens=usage.get("input_tokens", usage.get("promptTokenCount", 0)),
            output_tokens=usage.get("output_tokens", usage.get("candidatesTokenCount", 0)),
            raw=raw,
        )

    elif event_type == "error":
        error = raw.get("error", raw.get("message", ""))
        if isinstance(error, dict):
            msg = error.get("message", str(error))
        else:
            msg = str(error)
        return StreamEvent(
            type=StreamEventType.ERROR,
            raw_type=event_type,
            content=msg,
            raw=raw,
        )

    else:
        return StreamEvent(
            type=StreamEventType.UNKNOWN,
            raw_type=event_type,
            content=str(raw.get("content", raw.get("text", ""))),
            raw=raw,
        )


# Type alias for stream event callbacks.
StreamCallback = Callable[[StreamEvent], Coroutine[Any, Any, None]]


async def parse_stream_events(
    stdout_stream: asyncio.StreamReader,
    *,
    provider: str = "claude_code",
    on_text: Optional[StreamCallback] = None,
    on_tool_use: Optional[StreamCallback] = None,
    on_tool_result: Optional[StreamCallback] = None,
    on_result: Optional[StreamCallback] = None,
    on_error: Optional[StreamCallback] = None,
    on_any: Optional[StreamCallback] = None,
) -> List[StreamEvent]:
    """Parse stream-json events from a CLI provider's stdout.

    Reads lines from the stream, parses JSON, normalizes to StreamEvent,
    and dispatches to the appropriate callback. Returns all collected events.

    Args:
        stdout_stream: The subprocess stdout StreamReader.
        provider: Provider name for correct event normalization.
        on_text: Called for assistant text events.
        on_tool_use: Called when a tool is invoked.
        on_tool_result: Called when a tool completes.
        on_result: Called for the final result event.
        on_error: Called for error events.
        on_any: Called for every event (in addition to type-specific callback).

    Returns:
        List of all StreamEvent objects in order.
    """
    normalizer = _normalize_claude_event
    if provider in ("gemini_cli", "gemini"):
        normalizer = _normalize_gemini_event

    events = []  # type: List[StreamEvent]

    while True:
        line = await stdout_stream.readline()
        if not line:
            break

        line_text = line.decode("utf-8", errors="replace").strip()
        if not line_text:
            continue

        try:
            raw = json.loads(line_text)
        except json.JSONDecodeError:
            logger.debug("Non-JSON line from %s: %s", provider, line_text[:200])
            continue

        event = normalizer(raw)
        events.append(event)

        # Dispatch to callbacks
        if on_any is not None:
            await on_any(event)

        if event.type == StreamEventType.ASSISTANT and on_text is not None:
            await on_text(event)
        elif event.type == StreamEventType.TOOL_USE and on_tool_use is not None:
            await on_tool_use(event)
        elif event.type == StreamEventType.TOOL_RESULT and on_tool_result is not None:
            await on_tool_result(event)
        elif event.type == StreamEventType.RESULT and on_result is not None:
            await on_result(event)
        elif event.type == StreamEventType.ERROR and on_error is not None:
            await on_error(event)

    return events


# ---------------------------------------------------------------------------
# CLIProvider — unified interface
# ---------------------------------------------------------------------------

class CLIProvider:
    """Unified interface to all CLI providers.

    Wraps binary resolution, command construction, environment setup,
    subprocess management, and stream parsing into a single class.

    Usage:
        provider = CLIProvider("claude_code", model="sonnet")
        result = await provider.execute("Write a hello world program", cwd="/path/to/project")

        # Or for sessions:
        sid = str(uuid.uuid4())
        result = await provider.start_session("Let's build a REST API", session_id=sid, cwd="...")
        result2 = await provider.resume_session("Now add tests", session_id=sid, cwd="...")
    """

    def __init__(self, provider: str, model: Optional[str] = None):
        """Initialize with provider name and optional model override.

        Args:
            provider: One of "claude_code", "gemini_cli", "codex_cli".
            model: Model ID or alias. If None, uses provider's default.
        """
        self._config = get_provider_config(provider)
        self._model_id = model
        self._cmd_builder = CommandBuilder(self._config)
        self._model_config = self._config.get_model(model)

    @property
    def config(self) -> ProviderConfig:
        """The provider configuration."""
        return self._config

    @property
    def model(self) -> ModelConfig:
        """The active model configuration."""
        return self._model_config

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Provider capability flags."""
        return self._config.capabilities

    def build_cmd_args(self, mode: str = "single_shot", **kwargs) -> List[str]:
        """Build the raw command line arguments. Exposed for inspection/debugging.

        Args:
            mode: Execution mode string (maps to ExecutionMode enum).
            **kwargs: All keyword arguments supported by CommandBuilder.build().

        Returns:
            List of command-line argument strings.
        """
        exec_mode = ExecutionMode(mode) if mode in [e.value for e in ExecutionMode] else ExecutionMode.SINGLE_SHOT

        # Inject model if not explicitly provided
        if "model" not in kwargs or kwargs["model"] is None:
            kwargs["model"] = self._model_id

        return self._cmd_builder.build(exec_mode, **kwargs)

    def build_env(self) -> Dict[str, str]:
        """Build the environment dict for subprocess execution."""
        return build_env(self._config)

    async def execute(
        self,
        prompt: str,
        *,
        cwd: Optional[str] = None,
        output_format: OutputFormat = OutputFormat.STREAM_JSON,
        timeout: Optional[int] = None,
        on_event: Optional[StreamCallback] = None,
        **kwargs,
    ) -> ExecutionResult:
        """Single-shot execution. Returns output + usage.

        Pipes the prompt via stdin to avoid Windows cmd length limits.
        Parses stream-json events for real-time progress and cost tracking.

        Args:
            prompt: The full prompt text.
            cwd: Working directory for the subprocess.
            output_format: Output format (default: stream-json for event parsing).
            timeout: Timeout in seconds (default: from model config).
            on_event: Optional callback for every stream event.
            **kwargs: Additional flags passed to CommandBuilder.build().

        Returns:
            ExecutionResult with output, tokens, cost, and events.
        """
        return await self._run(
            mode=ExecutionMode.SINGLE_SHOT,
            prompt=prompt,
            cwd=cwd,
            output_format=output_format,
            timeout=timeout,
            on_event=on_event,
            **kwargs,
        )

    async def start_session(
        self,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        output_format: OutputFormat = OutputFormat.STREAM_JSON,
        timeout: Optional[int] = None,
        on_event: Optional[StreamCallback] = None,
        **kwargs,
    ) -> ExecutionResult:
        """Start a new conversation session (first turn).

        Args:
            prompt: The initial prompt.
            session_id: Session identifier. Generated if not provided.
            cwd: Working directory.
            output_format: Output format.
            timeout: Timeout in seconds.
            on_event: Optional callback for every stream event.
            **kwargs: Additional flags passed to CommandBuilder.build().

        Returns:
            ExecutionResult with session_id set.
        """
        if not self._config.capabilities.supports_sessions:
            logger.warning(
                "Provider '%s' does not support sessions, falling back to single-shot",
                self._config.name,
            )
            return await self.execute(prompt, cwd=cwd, output_format=output_format,
                                      timeout=timeout, on_event=on_event, **kwargs)

        sid = session_id or str(uuid.uuid4())
        result = await self._run(
            mode=ExecutionMode.SESSION_START,
            prompt=prompt,
            session_id=sid,
            cwd=cwd,
            output_format=output_format,
            timeout=timeout,
            on_event=on_event,
            **kwargs,
        )
        result.session_id = sid
        return result

    async def resume_session(
        self,
        prompt: str,
        *,
        session_id: str,
        cwd: Optional[str] = None,
        output_format: OutputFormat = OutputFormat.STREAM_JSON,
        timeout: Optional[int] = None,
        on_event: Optional[StreamCallback] = None,
        **kwargs,
    ) -> ExecutionResult:
        """Send a follow-up message in an existing session.

        Args:
            prompt: The follow-up prompt.
            session_id: Session identifier from start_session.
            cwd: Working directory.
            output_format: Output format.
            timeout: Timeout in seconds.
            on_event: Optional callback for every stream event.
            **kwargs: Additional flags passed to CommandBuilder.build().

        Returns:
            ExecutionResult for this turn.
        """
        if not self._config.capabilities.supports_sessions:
            logger.warning(
                "Provider '%s' does not support sessions, falling back to single-shot",
                self._config.name,
            )
            return await self.execute(prompt, cwd=cwd, output_format=output_format,
                                      timeout=timeout, on_event=on_event, **kwargs)

        result = await self._run(
            mode=ExecutionMode.SESSION_RESUME,
            prompt=prompt,
            session_id=session_id,
            cwd=cwd,
            output_format=output_format,
            timeout=timeout,
            on_event=on_event,
            **kwargs,
        )
        result.session_id = session_id
        return result

    async def check_auth(self) -> bool:
        """Verify the provider's auth is valid.

        Checks if the auth file exists and is non-empty. For Claude Code,
        also runs `claude auth status` to verify the credential is valid.
        """
        # Check auth file exists
        if self._config.auth_file:
            if not os.path.isfile(self._config.auth_file):
                logger.warning("Auth file not found: %s", self._config.auth_file)
                return False

        # Provider-specific checks
        if self._config.name == "claude_code":
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._config.binary, "auth", "status",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self.build_env(),
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                return proc.returncode == 0
            except Exception as e:
                logger.debug("Claude auth check failed: %s", e)
                return False

        # For other providers, auth file existence is sufficient
        return True

    async def list_mcp_servers(self) -> List[Dict[str, Any]]:
        """List configured MCP servers for this provider.

        Returns:
            List of MCP server configuration dicts.
        """
        if not self._config.capabilities.supports_mcp:
            return []

        if self._config.name == "claude_code":
            cmd = [self._config.binary, "mcp", "list"]
        elif self._config.name == "gemini_cli":
            cmd = [self._config.binary, "mcp", "list"]
        else:
            return []

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.build_env(),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = stdout.decode("utf-8", errors="replace").strip()
            if not output:
                return []
            # Try parsing as JSON array
            try:
                return json.loads(output)
            except json.JSONDecodeError:
                # Return raw text as a single entry
                return [{"raw": output}]
        except Exception as e:
            logger.debug("MCP list failed for %s: %s", self._config.name, e)
            return []

    async def add_mcp_server(
        self,
        name: str,
        command_or_url: str,
        *,
        transport: Optional[str] = None,
        args: Optional[List[str]] = None,
        server_json: Optional[str] = None,
    ) -> bool:
        """Add an MCP server to this provider.

        Args:
            name: MCP server name.
            command_or_url: Command or URL for the server.
            transport: Transport type (e.g., "http") — Claude Code only.
            args: Additional arguments for the command.
            server_json: JSON config string (uses add-json subcommand).

        Returns:
            True if the server was added successfully.
        """
        if not self._config.capabilities.supports_mcp:
            logger.warning("Provider '%s' does not support MCP", self._config.name)
            return False

        if self._config.name == "claude_code":
            if server_json:
                cmd = [self._config.binary, "mcp", "add-json", name, server_json]
            else:
                cmd = [self._config.binary, "mcp", "add"]
                if transport:
                    cmd.extend(["--transport", transport])
                cmd.extend([name, command_or_url])
                if args:
                    cmd.extend(args)
        elif self._config.name == "gemini_cli":
            cmd = [self._config.binary, "mcp", "add", name, command_or_url]
            if args:
                cmd.extend(args)
        else:
            return False

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.build_env(),
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                logger.warning(
                    "Failed to add MCP server '%s' to %s: %s",
                    name, self._config.name,
                    stderr.decode("utf-8", errors="replace")[:200],
                )
                return False
            return True
        except Exception as e:
            logger.debug("MCP add failed for %s: %s", self._config.name, e)
            return False

    # -------------------------------------------------------------------
    # Internal execution
    # -------------------------------------------------------------------

    async def _run(
        self,
        mode: ExecutionMode,
        prompt: str,
        *,
        session_id: Optional[str] = None,
        cwd: Optional[str] = None,
        output_format: OutputFormat = OutputFormat.STREAM_JSON,
        timeout: Optional[int] = None,
        on_event: Optional[StreamCallback] = None,
        **kwargs,
    ) -> ExecutionResult:
        """Core execution method shared by execute/start_session/resume_session.

        Builds the command, launches the subprocess, pipes the prompt via stdin,
        and parses stream-json output (or collects raw text for non-streaming).
        """
        effective_timeout = timeout or self._model_config.timeout_seconds

        # Build command args
        cmd_args = self._cmd_builder.build(
            mode,
            model=self._model_id,
            output_format=output_format,
            session_id=session_id,
            **kwargs,
        )

        # Build environment
        env = self.build_env()

        logger.debug(
            "Running %s: %s (cwd=%s, timeout=%ds)",
            self._config.name, " ".join(cmd_args[:5]) + "...", cwd, effective_timeout,
        )

        # Determine if we should stream-parse or collect raw output
        use_streaming = (
            output_format == OutputFormat.STREAM_JSON
            and self._config.capabilities.supports_stream_json
        )

        # Launch subprocess.
        # 10MB line buffer prevents "Separator is found, but chunk is longer than limit"
        # when Claude Code emits large stream-json lines (e.g., tool_result with file contents).
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=10 * 1024 * 1024,  # 10 MB line buffer
        )

        text_parts = []  # type: List[str]
        all_events = []  # type: List[StreamEvent]
        total_cost = 0.0
        model_used = self._model_config.model_id
        total_input_tokens = 0
        total_output_tokens = 0
        timed_out = False

        try:
            # Send prompt via stdin
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            if use_streaming:
                # Parse stream-json events
                async def _read_stream():
                    nonlocal total_cost, model_used, total_input_tokens, total_output_tokens

                    events = await parse_stream_events(
                        proc.stdout,
                        provider=self._config.name,
                        on_any=on_event,
                    )
                    all_events.extend(events)

                    for event in events:
                        if event.type == StreamEventType.ASSISTANT and event.content:
                            text_parts.append(event.content)
                        elif event.type == StreamEventType.RESULT:
                            if event.content:
                                text_parts.append(event.content)
                            total_cost = event.cost_usd
                            if event.model:
                                model_used = event.model
                            total_input_tokens = event.input_tokens
                            total_output_tokens = event.output_tokens

                await asyncio.wait_for(_read_stream(), timeout=effective_timeout)
            else:
                # Collect raw stdout
                stdout_bytes, _ = await asyncio.wait_for(
                    proc.communicate(input=None),  # stdin already closed
                    timeout=effective_timeout,
                )
                raw_output = stdout_bytes.decode("utf-8", errors="replace").strip()
                if raw_output:
                    text_parts.append(raw_output)

            await proc.wait()

        except asyncio.TimeoutError:
            logger.warning(
                "%s timed out after %ds", self._config.name, effective_timeout,
            )
            proc.kill()
            await proc.wait()
            timed_out = True

        except Exception:
            proc.kill()
            await proc.wait()
            raise

        # Collect stderr
        stderr_text = ""
        if proc.stderr:
            try:
                stderr_bytes = await proc.stderr.read()
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            except Exception:
                pass

        output = "\n".join(text_parts).strip()

        return ExecutionResult(
            output=output,
            prompt_tokens=total_input_tokens,
            completion_tokens=total_output_tokens,
            cost_usd=total_cost,
            model_used=model_used,
            session_id=session_id,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stderr=stderr_text,
            events=all_events,
            timed_out=timed_out,
        )


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def create_provider(
    provider: str,
    model: Optional[str] = None,
) -> CLIProvider:
    """Create a CLIProvider instance.

    Convenience factory that validates the provider name and returns
    a configured CLIProvider ready for execution.

    Args:
        provider: Provider name ("claude_code", "gemini_cli", "codex_cli").
        model: Optional model ID or alias.

    Returns:
        Configured CLIProvider instance.

    Raises:
        ValueError: If the provider is unknown or disabled.
    """
    config = get_provider_config(provider)
    if not config.enabled:
        logger.warning(
            "Provider '%s' is disabled. Creating provider anyway for inspection.",
            provider,
        )
    return CLIProvider(provider, model)


# ---------------------------------------------------------------------------
# Subcommand helpers
# ---------------------------------------------------------------------------

async def run_provider_subcommand(
    provider: str,
    subcommand: List[str],
    *,
    timeout: int = 15,
) -> Tuple[str, str, int]:
    """Run a provider subcommand (e.g., `claude mcp list`, `gemini extensions list`).

    Args:
        provider: Provider name.
        subcommand: Subcommand args (e.g., ["mcp", "list"]).
        timeout: Timeout in seconds.

    Returns:
        Tuple of (stdout, stderr, returncode).
    """
    config = get_provider_config(provider)
    cmd = [config.binary] + subcommand
    env = build_env(config)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "", "Timed out after {}s".format(timeout), -1

    return (
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
        proc.returncode or 0,
    )


# ---------------------------------------------------------------------------
# Claude Code subcommand wrappers
# ---------------------------------------------------------------------------

async def claude_auth_login() -> Tuple[str, str, int]:
    """Run `claude auth login`."""
    return await run_provider_subcommand("claude_code", ["auth", "login"], timeout=30)


async def claude_auth_logout() -> Tuple[str, str, int]:
    """Run `claude auth logout`."""
    return await run_provider_subcommand("claude_code", ["auth", "logout"])


async def claude_auth_status() -> Tuple[str, str, int]:
    """Run `claude auth status`."""
    return await run_provider_subcommand("claude_code", ["auth", "status"])


async def claude_doctor() -> Tuple[str, str, int]:
    """Run `claude doctor` — health check."""
    return await run_provider_subcommand("claude_code", ["doctor"], timeout=30)


async def claude_mcp_list() -> Tuple[str, str, int]:
    """Run `claude mcp list`."""
    return await run_provider_subcommand("claude_code", ["mcp", "list"])


async def claude_mcp_get(name: str) -> Tuple[str, str, int]:
    """Run `claude mcp get <name>`."""
    return await run_provider_subcommand("claude_code", ["mcp", "get", name])


async def claude_mcp_remove(name: str) -> Tuple[str, str, int]:
    """Run `claude mcp remove <name>`."""
    return await run_provider_subcommand("claude_code", ["mcp", "remove", name])


async def claude_mcp_serve() -> Tuple[str, str, int]:
    """Run `claude mcp serve` — start Claude Code as MCP server."""
    return await run_provider_subcommand("claude_code", ["mcp", "serve"], timeout=60)


async def claude_mcp_reset_project_choices() -> Tuple[str, str, int]:
    """Run `claude mcp reset-project-choices`."""
    return await run_provider_subcommand("claude_code", ["mcp", "reset-project-choices"])


async def claude_mcp_add_from_desktop() -> Tuple[str, str, int]:
    """Run `claude mcp add-from-claude-desktop`."""
    return await run_provider_subcommand("claude_code", ["mcp", "add-from-claude-desktop"])


async def claude_list_agents() -> Tuple[str, str, int]:
    """Run `claude agents` — list available agents."""
    return await run_provider_subcommand("claude_code", ["agents"])


async def claude_update() -> Tuple[str, str, int]:
    """Run `claude update`."""
    return await run_provider_subcommand("claude_code", ["update"], timeout=120)


async def claude_install(target: Optional[str] = None) -> Tuple[str, str, int]:
    """Run `claude install [target]`."""
    cmd = ["install"]
    if target:
        cmd.append(target)
    return await run_provider_subcommand("claude_code", cmd, timeout=120)


async def claude_setup_token() -> Tuple[str, str, int]:
    """Run `claude setup-token`."""
    return await run_provider_subcommand("claude_code", ["setup-token"], timeout=30)


# ---------------------------------------------------------------------------
# Gemini CLI subcommand wrappers
# ---------------------------------------------------------------------------

async def gemini_mcp_list() -> Tuple[str, str, int]:
    """Run `gemini mcp list`."""
    return await run_provider_subcommand("gemini_cli", ["mcp", "list"])


async def gemini_mcp_add(name: str, command_or_url: str) -> Tuple[str, str, int]:
    """Run `gemini mcp add <name> <command_or_url>`."""
    return await run_provider_subcommand("gemini_cli", ["mcp", "add", name, command_or_url])


async def gemini_mcp_remove(name: str) -> Tuple[str, str, int]:
    """Run `gemini mcp remove <name>`."""
    return await run_provider_subcommand("gemini_cli", ["mcp", "remove", name])


async def gemini_mcp_enable(name: str) -> Tuple[str, str, int]:
    """Run `gemini mcp enable <name>`."""
    return await run_provider_subcommand("gemini_cli", ["mcp", "enable", name])


async def gemini_mcp_disable(name: str) -> Tuple[str, str, int]:
    """Run `gemini mcp disable <name>`."""
    return await run_provider_subcommand("gemini_cli", ["mcp", "disable", name])


async def gemini_extensions_list() -> Tuple[str, str, int]:
    """Run `gemini extensions list`."""
    return await run_provider_subcommand("gemini_cli", ["extensions", "list"])


async def gemini_extensions_install(name: str) -> Tuple[str, str, int]:
    """Run `gemini extensions install <name>`."""
    return await run_provider_subcommand("gemini_cli", ["extensions", "install", name], timeout=60)


async def gemini_extensions_uninstall(name: str) -> Tuple[str, str, int]:
    """Run `gemini extensions uninstall <name>`."""
    return await run_provider_subcommand("gemini_cli", ["extensions", "uninstall", name])


async def gemini_skills_list() -> Tuple[str, str, int]:
    """Run `gemini skills list`."""
    return await run_provider_subcommand("gemini_cli", ["skills", "list"])


async def gemini_hooks_migrate() -> Tuple[str, str, int]:
    """Run `gemini hooks migrate` — migrate hooks from Claude Code."""
    return await run_provider_subcommand("gemini_cli", ["hooks", "migrate"])


async def gemini_list_sessions() -> Tuple[str, str, int]:
    """Run `gemini --list-sessions`."""
    return await run_provider_subcommand("gemini_cli", ["--list-sessions"])
