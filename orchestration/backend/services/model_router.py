#  Orchestration Engine - Model Router
#
#  Selects the cheapest model that can handle a task, and calculates costs.
#
#  Depends on: backend/config.py, backend/services/model_discovery.py, backend/services/provider_quota.py
#  Used by:    services/planner.py, services/decomposer.py, services/executor.py

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.config import MODEL_PRICING, cfg
from backend.models.enums import ModelTier

if TYPE_CHECKING:
    from backend.services.model_discovery import ModelDiscoveryService
    from backend.services.provider_quota import ProviderQuotaManager

logger = logging.getLogger("orchestration.model_router")

# Track which unknown models we've already warned about (avoid log spam)
_warned_models: set[str] = set()


def _reset_warned_models():
    """Clear the warned-models set. Used by test fixtures to prevent state leak."""
    _warned_models.clear()


# ---------------------------------------------------------------------------
# Model ID resolution
# ---------------------------------------------------------------------------

# Hardcoded fallbacks — used only when config has no override and discovery
# cache is empty (e.g. before startup discovery completes or on API failure).
_DEFAULT_MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}

_DEFAULT_CLI_FALLBACKS = {
    "gemini_cli": "gemini-2.5-pro",
    "codex_cli": "gpt-5.4",
    "ollama": "qwen2.5-coder:14b",
}

# Injected at startup by app.py lifespan via set_discovery_service().
_discovery: ModelDiscoveryService | None = None


def set_discovery_service(svc: ModelDiscoveryService) -> None:
    """Wire in the ModelDiscoveryService singleton. Called once at startup."""
    global _discovery
    _discovery = svc


def get_model_id(tier: ModelTier) -> str:
    """Resolve a model tier to the actual model ID.

    Priority: 1) config.json override  2) discovery cache  3) hardcoded fallback
    """
    if tier == ModelTier.CLAUDE_CODE:
        return "claude-code-cli"

    # 1. Config override — explicit config values always win.
    if tier == ModelTier.GEMINI_CLI:
        override = cfg("gemini_cli.model", "")
    elif tier == ModelTier.CODEX_CLI:
        override = cfg("codex_cli.model", "")
    elif tier == ModelTier.OLLAMA:
        override = cfg("ollama.default_model", "")
    else:
        override = cfg(f"anthropic.models.{tier.value}", "")
    if override:
        return override

    # 2. Discovery cache — check if the preferred fallback model is available.
    #    Don't blindly return models[0] — discovery may return a model that
    #    the CLI tool can't use (e.g., gpt-4o when we want gpt-5.4).
    #    Instead, check if our preferred model is in the discovered list.
    if _discovery is not None:
        available = _discovery.get_available_models()
        if tier == ModelTier.GEMINI_CLI:
            models = available.get("google", [])
            preferred = _DEFAULT_CLI_FALLBACKS["gemini_cli"]
            if preferred in models:
                return preferred
            if models:
                return models[0]
        elif tier == ModelTier.CODEX_CLI:
            models = available.get("openai", [])
            preferred = _DEFAULT_CLI_FALLBACKS["codex_cli"]
            if preferred in models:
                return preferred
            if models:
                return models[0]
        elif tier == ModelTier.OLLAMA:
            models = available.get("ollama", [])
            preferred = _DEFAULT_CLI_FALLBACKS["ollama"]
            if preferred in models:
                return preferred
            if models:
                return models[0]
        else:
            # Anthropic — find first discovered model whose ID contains the tier name.
            for m in available.get("anthropic", []):
                if tier.value in m:
                    return m

    # 3. Hardcoded fallbacks.
    if tier == ModelTier.GEMINI_CLI:
        return _DEFAULT_CLI_FALLBACKS["gemini_cli"]
    if tier == ModelTier.CODEX_CLI:
        return _DEFAULT_CLI_FALLBACKS["codex_cli"]
    if tier == ModelTier.OLLAMA:
        return _DEFAULT_CLI_FALLBACKS["ollama"]
    return _DEFAULT_MODELS.get(tier.value, f"claude-{tier.value}")


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

def calculate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Calculate the USD cost for a given token usage."""
    pricing = MODEL_PRICING.get(model, {})
    if not pricing:
        if model not in _warned_models:
            logger.warning("Unknown model '%s' — cost recorded as $0.00", model)
            _warned_models.add(model)
        return 0.0

    input_cost = (prompt_tokens / 1_000_000) * pricing.get("input_per_mtok", 0)
    output_cost = (completion_tokens / 1_000_000) * pricing.get("output_per_mtok", 0)
    return round(input_cost + output_cost, 6)


def estimate_task_cost(
    tier: ModelTier,
    estimated_input_tokens: int,
    max_output_tokens: int,
) -> float:
    """Estimate the worst-case cost for a task before execution."""
    if tier in (ModelTier.OLLAMA, ModelTier.CLAUDE_CODE, ModelTier.GEMINI_CLI, ModelTier.CODEX_CLI):
        return 0.0  # Ollama is local, CLI tools are subscription-billed
    model_id = get_model_id(tier)
    return calculate_cost(model_id, estimated_input_tokens, max_output_tokens)


# ---------------------------------------------------------------------------
# Recommended tier selection
# ---------------------------------------------------------------------------

# Mapping: (task_type, complexity) -> recommended model tier
# Default: CLI-first routing — subscription CLIs are $0/call vs API billing.
# Ollama for simple tasks (free, local), CLI for medium/complex.
# API tiers (haiku/sonnet) available but not default — use config override.
# NOTE: CODEX_CLI disabled — ChatGPT subscription doesn't support any model
# in codex exec mode (gpt-5.4, o4-mini, o3 all return 401/unsupported).
# All codex_cli entries routed to GEMINI_CLI until OpenAI fixes this.
_TIER_MAP: dict[tuple[str, str], ModelTier] = {
    # Code tasks
    ("code", "simple"): ModelTier.GEMINI_CLI,
    ("code", "medium"): ModelTier.CLAUDE_CODE,
    ("code", "complex"): ModelTier.CLAUDE_CODE,
    # Research
    ("research", "simple"): ModelTier.GEMINI_CLI,
    ("research", "medium"): ModelTier.GEMINI_CLI,
    ("research", "complex"): ModelTier.GEMINI_CLI,
    # Analysis
    ("analysis", "simple"): ModelTier.OLLAMA,
    ("analysis", "medium"): ModelTier.GEMINI_CLI,
    ("analysis", "complex"): ModelTier.CLAUDE_CODE,
    # Asset generation
    ("asset", "simple"): ModelTier.OLLAMA,
    ("asset", "medium"): ModelTier.OLLAMA,
    ("asset", "complex"): ModelTier.OLLAMA,
    # Integration
    ("integration", "simple"): ModelTier.GEMINI_CLI,
    ("integration", "medium"): ModelTier.CLAUDE_CODE,
    ("integration", "complex"): ModelTier.CLAUDE_CODE,
    # Documentation
    ("documentation", "simple"): ModelTier.OLLAMA,
    ("documentation", "medium"): ModelTier.GEMINI_CLI,
    ("documentation", "complex"): ModelTier.GEMINI_CLI,
    # Claude Code CLI — for tasks requiring file I/O, git, MCP tools
    ("code_cli", "simple"): ModelTier.CLAUDE_CODE,
    ("code_cli", "medium"): ModelTier.CLAUDE_CODE,
    ("code_cli", "complex"): ModelTier.CLAUDE_CODE,
    # Game development task types
    ("game_design", "simple"): ModelTier.GEMINI_CLI,
    ("game_design", "medium"): ModelTier.GEMINI_CLI,
    ("game_design", "complex"): ModelTier.CLAUDE_CODE,
    ("game_content", "simple"): ModelTier.GEMINI_CLI,
    ("game_content", "medium"): ModelTier.GEMINI_CLI,
    ("game_content", "complex"): ModelTier.GEMINI_CLI,
    ("game_code", "simple"): ModelTier.GEMINI_CLI,
    ("game_code", "medium"): ModelTier.CLAUDE_CODE,
    ("game_code", "complex"): ModelTier.CLAUDE_CODE,
    ("game_ui", "simple"): ModelTier.CLAUDE_CODE,
    ("game_ui", "medium"): ModelTier.CLAUDE_CODE,
    ("game_ui", "complex"): ModelTier.CLAUDE_CODE,
    ("game_build_verify", "simple"): ModelTier.CLAUDE_CODE,
    ("game_build_verify", "medium"): ModelTier.CLAUDE_CODE,
    ("game_build_verify", "complex"): ModelTier.CLAUDE_CODE,
}


def recommend_tier(task_type: str, complexity: str) -> ModelTier:
    """Get the recommended model tier for a task type and complexity."""
    return _TIER_MAP.get((task_type, complexity), ModelTier.CLAUDE_CODE)


# Maps ModelTier to the provider name used in provider_quotas config.
# API tiers (haiku/sonnet/opus) share the "claude_code" quota key since they
# go through the same Anthropic account.
# Single source of truth — imported by executor.py, task_lifecycle.py.
TIER_TO_PROVIDER: dict[ModelTier, str] = {
    ModelTier.CLAUDE_CODE: "claude_code",
    ModelTier.GEMINI_CLI: "gemini_cli",
    ModelTier.CODEX_CLI: "codex_cli",
    ModelTier.HAIKU: "claude_code",
    ModelTier.SONNET: "claude_code",
    ModelTier.OPUS: "claude_code",
    ModelTier.OLLAMA: "ollama",
}


def get_provider_for_tier(tier: ModelTier) -> str:
    """Get the provider name for a model tier."""
    return TIER_TO_PROVIDER.get(tier, tier.value)

# Fallback chain tried (in order) when the recommended cloud tier is hot.
_CLOUD_FALLBACK_ORDER = [
    ModelTier.CLAUDE_CODE,
    ModelTier.GEMINI_CLI,
    ModelTier.CODEX_CLI,
]


async def get_available_tiers(
    task_type: str,
    complexity: str,
    quota_manager: ProviderQuotaManager | None = None,
) -> ModelTier:
    """Quota-aware tier selection.

    Starts from recommend_tier() and checks provider availability.  If the
    recommended tier's provider is warned (>= warn_pct), falls back through
    the cloud fallback chain.  If all cloud providers are hot, returns Ollama.

    When quota_manager is None, behaves identically to recommend_tier().
    """
    recommended = recommend_tier(task_type, complexity)

    if quota_manager is None:
        return recommended

    # Ollama has no limits — always available.
    if recommended == ModelTier.OLLAMA:
        return recommended

    provider = TIER_TO_PROVIDER.get(recommended, "")
    if provider and await quota_manager.is_provider_available(provider):
        return recommended

    # Recommended provider is hot — try alternatives in fallback order.
    for fallback in _CLOUD_FALLBACK_ORDER:
        if fallback == recommended:
            continue
        fallback_provider = TIER_TO_PROVIDER.get(fallback, "")
        if fallback_provider and await quota_manager.is_provider_available(fallback_provider):
            logger.info(
                "Provider '%s' warned — routing %s/%s to %s",
                provider, task_type, complexity, fallback.value,
            )
            return fallback

    # All cloud providers are hot — force Ollama.
    logger.warning(
        "All cloud providers warned — forcing Ollama fallback for %s/%s",
        task_type, complexity,
    )
    return ModelTier.OLLAMA


# ---------------------------------------------------------------------------
# Tools by task type
# ---------------------------------------------------------------------------

_TOOLS_MAP: dict[str, list[str]] = {
    "code": ["search_knowledge", "lookup_type", "local_llm", "read_file", "write_file"],
    "research": ["search_knowledge", "lookup_type", "local_llm"],
    "analysis": ["search_knowledge", "local_llm", "read_file"],
    "asset": ["local_llm", "generate_image"],
    "integration": ["read_file", "write_file", "local_llm"],
    "documentation": ["search_knowledge", "local_llm", "read_file", "write_file"],
    # Game development task types
    "game_design": ["search_knowledge", "local_llm", "read_file", "write_file"],
    "game_content": ["search_knowledge", "lookup_type", "local_llm", "write_file"],
    "game_code": ["search_knowledge", "lookup_type", "local_llm", "read_file", "write_file"],
    "game_ui": ["search_knowledge", "lookup_type", "read_file", "write_file"],
    "game_build_verify": ["read_file"],
}


def recommend_tools(task_type: str) -> list[str]:
    """Get the recommended tool set for a task type."""
    return _TOOLS_MAP.get(task_type, ["search_knowledge", "local_llm"])
