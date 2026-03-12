#  Orchestration Engine - Model Router
#
#  Selects the cheapest model that can handle a task, and calculates costs.
#
#  Depends on: backend/config.py
#  Used by:    services/planner.py, services/decomposer.py, services/executor.py

import logging

from backend.config import MODEL_PRICING, cfg
from backend.models.enums import ModelTier

logger = logging.getLogger("orchestration.model_router")

# Track which unknown models we've already warned about (avoid log spam)
_warned_models: set[str] = set()


def _reset_warned_models():
    """Clear the warned-models set. Used by test fixtures to prevent state leak."""
    _warned_models.clear()


# ---------------------------------------------------------------------------
# Model ID resolution
# ---------------------------------------------------------------------------

# Fallback model IDs when config is missing — must be valid Anthropic model IDs
_DEFAULT_MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}


def get_model_id(tier: ModelTier) -> str:
    """Resolve a model tier to the actual model ID from config."""
    if tier == ModelTier.OLLAMA:
        return cfg("ollama.default_model", "qwen2.5-coder:14b")
    return cfg(f"anthropic.models.{tier.value}", _DEFAULT_MODELS.get(tier.value, f"claude-{tier.value}"))


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
    if tier == ModelTier.OLLAMA:
        return 0.0
    model_id = get_model_id(tier)
    return calculate_cost(model_id, estimated_input_tokens, max_output_tokens)


# ---------------------------------------------------------------------------
# Recommended tier selection
# ---------------------------------------------------------------------------

# Mapping: (task_type, complexity) -> recommended model tier
_TIER_MAP: dict[tuple[str, str], ModelTier] = {
    # Code tasks
    ("code", "simple"): ModelTier.HAIKU,
    ("code", "medium"): ModelTier.SONNET,
    ("code", "complex"): ModelTier.SONNET,
    # Research
    ("research", "simple"): ModelTier.OLLAMA,
    ("research", "medium"): ModelTier.HAIKU,
    ("research", "complex"): ModelTier.SONNET,
    # Analysis
    ("analysis", "simple"): ModelTier.OLLAMA,
    ("analysis", "medium"): ModelTier.HAIKU,
    ("analysis", "complex"): ModelTier.SONNET,
    # Asset generation
    ("asset", "simple"): ModelTier.OLLAMA,
    ("asset", "medium"): ModelTier.OLLAMA,
    ("asset", "complex"): ModelTier.OLLAMA,
    # Integration
    ("integration", "simple"): ModelTier.HAIKU,
    ("integration", "medium"): ModelTier.HAIKU,
    ("integration", "complex"): ModelTier.SONNET,
    # Documentation
    ("documentation", "simple"): ModelTier.OLLAMA,
    ("documentation", "medium"): ModelTier.HAIKU,
    ("documentation", "complex"): ModelTier.SONNET,
}


def recommend_tier(task_type: str, complexity: str) -> ModelTier:
    """Get the recommended model tier for a task type and complexity."""
    return _TIER_MAP.get((task_type, complexity), ModelTier.HAIKU)


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
}


def recommend_tools(task_type: str) -> list[str]:
    """Get the recommended tool set for a task type."""
    return _TOOLS_MAP.get(task_type, ["search_knowledge", "local_llm"])
