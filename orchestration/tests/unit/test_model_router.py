#  Orchestration Engine - Model Router Tests
#
#  Unit tests for model tier selection, cost calculation, and tool recommendations.
#  Pure functions — no I/O, no database.
#
#  Depends on: backend/services/model_router.py, backend/models/enums.py
#  Used by:    pytest

from unittest.mock import AsyncMock, patch

import pytest

from backend.models.enums import ModelTier
from backend.services.model_router import (
    _BASE_TIER_MAP,
    _STRATEGY_MAPS,
    _STRATEGY_OVERRIDES,
    calculate_cost,
    estimate_task_cost,
    get_available_tiers,
    get_model_id,
    recommend_tier,
    recommend_tools,
)


# ---------------------------------------------------------------------------
# calculate_cost
# ---------------------------------------------------------------------------

class TestCalculateCost:
    def test_known_model_nonzero(self):
        """Known model with tokens should produce a positive cost."""
        pricing = {
            "claude-sonnet-4-6": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
        }
        with patch("backend.services.model_router.MODEL_PRICING", pricing):
            cost = calculate_cost("claude-sonnet-4-6", 1000, 500)
        assert cost > 0

    def test_unknown_model_returns_zero(self):
        """Unknown model should return 0 cost."""
        with patch("backend.services.model_router.MODEL_PRICING", {}):
            assert calculate_cost("no-such-model", 1000, 500) == 0.0

    def test_unknown_model_logs_warning(self, caplog):
        """Unknown model should log a warning (once per model)."""
        import logging
        from backend.services.model_router import _warned_models
        _warned_models.discard("fake-model-abc")  # ensure clean state
        with patch("backend.services.model_router.MODEL_PRICING", {}):
            with caplog.at_level(logging.WARNING, logger="orchestration.model_router"):
                calculate_cost("fake-model-abc", 1000, 500)
                calculate_cost("fake-model-abc", 2000, 1000)
        # Should warn only once despite two calls
        warnings = [r for r in caplog.records if "fake-model-abc" in r.message]
        assert len(warnings) == 1
        _warned_models.discard("fake-model-abc")  # cleanup

    def test_zero_tokens(self):
        """Zero tokens should produce zero cost even for a priced model."""
        pricing = {
            "claude-sonnet-4-6": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
        }
        with patch("backend.services.model_router.MODEL_PRICING", pricing):
            assert calculate_cost("claude-sonnet-4-6", 0, 0) == 0.0

    def test_linear_scaling(self):
        """Cost should scale linearly with token count."""
        pricing = {
            "test-model": {"input_per_mtok": 10.0, "output_per_mtok": 50.0},
        }
        with patch("backend.services.model_router.MODEL_PRICING", pricing):
            cost_1x = calculate_cost("test-model", 1000, 1000)
            cost_2x = calculate_cost("test-model", 2000, 2000)
        assert abs(cost_2x - 2 * cost_1x) < 1e-9

    def test_output_more_expensive_than_input(self):
        """Output tokens should cost more than input tokens for typical pricing."""
        pricing = {
            "test-model": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
        }
        with patch("backend.services.model_router.MODEL_PRICING", pricing):
            input_only = calculate_cost("test-model", 1_000_000, 0)
            output_only = calculate_cost("test-model", 0, 1_000_000)
        assert output_only > input_only

    def test_exact_values(self):
        """Verify exact cost calculation for known inputs."""
        pricing = {
            "claude-haiku-4-5-20251001": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
        }
        with patch("backend.services.model_router.MODEL_PRICING", pricing):
            # 1M input * $1/Mtok + 1M output * $5/Mtok = $6.00
            cost = calculate_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
        assert cost == 6.0


# ---------------------------------------------------------------------------
# estimate_task_cost
# ---------------------------------------------------------------------------

class TestEstimateTaskCost:
    def test_ollama_always_free(self):
        """Ollama tasks should always return 0 cost."""
        assert estimate_task_cost(ModelTier.OLLAMA, 10_000, 4096) == 0.0

    def test_claude_tier_returns_positive(self):
        """Claude tiers should return positive estimated cost."""
        pricing = {
            "claude-sonnet-4-6": {"input_per_mtok": 3.0, "output_per_mtok": 15.0},
        }
        with patch("backend.services.model_router.MODEL_PRICING", pricing):
            with patch("backend.services.model_router.cfg", return_value="claude-sonnet-4-6"):
                cost = estimate_task_cost(ModelTier.SONNET, 1500, 4096)
        assert cost > 0


# ---------------------------------------------------------------------------
# recommend_tier
# ---------------------------------------------------------------------------

class TestRecommendTier:
    """Tests use default routing_strategy='best'."""

    def test_research_simple_is_gemini(self):
        """Best strategy: research/simple → Gemini (strong at search)."""
        assert recommend_tier("research", "simple") == ModelTier.GEMINI_CLI

    def test_code_simple_is_claude_code(self):
        """Best strategy: code/simple → Claude Code (strongest code executor)."""
        assert recommend_tier("code", "simple") == ModelTier.CLAUDE_CODE

    def test_code_medium_is_claude_code(self):
        assert recommend_tier("code", "medium") == ModelTier.CLAUDE_CODE

    def test_asset_complex_is_ollama(self):
        """Asset tasks should use Ollama regardless of complexity."""
        assert recommend_tier("asset", "complex") == ModelTier.OLLAMA

    def test_unknown_type_defaults_to_claude_code(self):
        assert recommend_tier("unknown_type", "medium") == ModelTier.CLAUDE_CODE

    def test_unknown_complexity_defaults_to_claude_code(self):
        assert recommend_tier("code", "extreme") == ModelTier.CLAUDE_CODE


# ---------------------------------------------------------------------------
# get_model_id
# ---------------------------------------------------------------------------

class TestGetModelId:
    def test_ollama_returns_config_model(self):
        with patch("backend.services.model_router.cfg", return_value="qwen2.5-coder:14b"):
            result = get_model_id(ModelTier.OLLAMA)
        assert result == "qwen2.5-coder:14b"

    def test_sonnet_returns_valid_id(self):
        with patch("backend.services.model_router.cfg", return_value="claude-sonnet-4-6"):
            result = get_model_id(ModelTier.SONNET)
        assert "sonnet" in result

    def test_haiku_returns_valid_id(self):
        with patch("backend.services.model_router.cfg", return_value="claude-haiku-4-5-20251001"):
            result = get_model_id(ModelTier.HAIKU)
        assert "haiku" in result


# ---------------------------------------------------------------------------
# recommend_tools
# ---------------------------------------------------------------------------

class TestRecommendTools:
    def test_code_includes_file_tools(self):
        tools = recommend_tools("code")
        assert "read_file" in tools
        assert "write_file" in tools

    def test_research_includes_rag(self):
        tools = recommend_tools("research")
        assert "search_knowledge" in tools

    def test_asset_includes_image(self):
        tools = recommend_tools("asset")
        assert "generate_image" in tools

    def test_unknown_type_has_defaults(self):
        tools = recommend_tools("nonexistent_type")
        assert len(tools) > 0
        assert "search_knowledge" in tools
        assert "local_llm" in tools


# ---------------------------------------------------------------------------
# Routing strategy selection
# ---------------------------------------------------------------------------

class TestRoutingStrategy:
    """Test configurable routing_strategy (best/cheapest/balanced)."""

    def test_best_routes_code_simple_to_claude(self):
        with patch("backend.services.model_router.cfg", return_value="best"):
            assert recommend_tier("code", "simple") == ModelTier.CLAUDE_CODE

    def test_cheapest_routes_code_simple_to_gemini(self):
        with patch("backend.services.model_router.cfg", return_value="cheapest"):
            assert recommend_tier("code", "simple") == ModelTier.GEMINI_CLI

    def test_balanced_routes_code_simple_to_gemini(self):
        with patch("backend.services.model_router.cfg", return_value="balanced"):
            assert recommend_tier("code", "simple") == ModelTier.GEMINI_CLI

    def test_cheapest_routes_analysis_simple_to_ollama(self):
        with patch("backend.services.model_router.cfg", return_value="cheapest"):
            assert recommend_tier("analysis", "simple") == ModelTier.OLLAMA

    def test_best_routes_analysis_simple_to_gemini(self):
        with patch("backend.services.model_router.cfg", return_value="best"):
            assert recommend_tier("analysis", "simple") == ModelTier.GEMINI_CLI

    def test_balanced_routes_analysis_simple_to_gemini(self):
        with patch("backend.services.model_router.cfg", return_value="balanced"):
            assert recommend_tier("analysis", "simple") == ModelTier.GEMINI_CLI

    def test_cheapest_routes_docs_simple_to_ollama(self):
        with patch("backend.services.model_router.cfg", return_value="cheapest"):
            assert recommend_tier("documentation", "simple") == ModelTier.OLLAMA

    def test_best_routes_docs_simple_to_gemini(self):
        with patch("backend.services.model_router.cfg", return_value="best"):
            assert recommend_tier("documentation", "simple") == ModelTier.GEMINI_CLI

    def test_all_strategies_agree_on_complex_code(self):
        """All strategies should route complex code to Claude Code."""
        for strategy in ("best", "cheapest", "balanced"):
            with patch("backend.services.model_router.cfg", return_value=strategy):
                assert recommend_tier("code", "complex") == ModelTier.CLAUDE_CODE

    def test_all_strategies_agree_on_assets(self):
        """All strategies should route assets to Ollama."""
        for strategy in ("best", "cheapest", "balanced"):
            with patch("backend.services.model_router.cfg", return_value=strategy):
                assert recommend_tier("asset", "medium") == ModelTier.OLLAMA

    def test_unknown_strategy_falls_back_to_best(self):
        with patch("backend.services.model_router.cfg", return_value="nonexistent"):
            # Should behave like "best" — code/simple → CLAUDE_CODE
            assert recommend_tier("code", "simple") == ModelTier.CLAUDE_CODE

    def test_default_strategy_is_best(self):
        """When config has no routing_strategy, default should be 'best'."""
        # recommend_tier calls cfg("routing_strategy", "best") — default arg is "best"
        assert recommend_tier("code", "simple") == ModelTier.CLAUDE_CODE


# ---------------------------------------------------------------------------
# Map merging and materialization
# ---------------------------------------------------------------------------

class TestStrategyMapMerging:
    """Test that base map + overrides produce correct materialized maps."""

    def test_all_strategies_have_36_entries(self):
        for name, tier_map in _STRATEGY_MAPS.items():
            assert len(tier_map) == 36, f"Strategy '{name}' has {len(tier_map)} entries, expected 36"

    def test_three_strategies_exist(self):
        assert set(_STRATEGY_MAPS.keys()) == {"best", "cheapest", "balanced"}

    def test_overrides_are_subset_of_materialized(self):
        """Every override key should appear in the materialized map."""
        for name, overrides in _STRATEGY_OVERRIDES.items():
            materialized = _STRATEGY_MAPS[name]
            for key, tier in overrides.items():
                assert key in materialized
                assert materialized[key] == tier

    def test_base_entries_present_when_not_overridden(self):
        """Base map entries should be in materialized map when no override exists."""
        for name, overrides in _STRATEGY_OVERRIDES.items():
            materialized = _STRATEGY_MAPS[name]
            for key, tier in _BASE_TIER_MAP.items():
                if key not in overrides:
                    assert materialized[key] == tier, (
                        f"Strategy '{name}': base entry {key} should be {tier}, got {materialized.get(key)}"
                    )

    def test_strategies_differ_on_code_simple(self):
        """The key differentiator: code/simple varies by strategy."""
        assert _STRATEGY_MAPS["best"][("code", "simple")] == ModelTier.CLAUDE_CODE
        assert _STRATEGY_MAPS["cheapest"][("code", "simple")] == ModelTier.GEMINI_CLI
        assert _STRATEGY_MAPS["balanced"][("code", "simple")] == ModelTier.GEMINI_CLI

    def test_all_tiers_are_valid_model_tiers(self):
        """Every value in every strategy map should be a valid ModelTier."""
        for name, tier_map in _STRATEGY_MAPS.items():
            for key, tier in tier_map.items():
                assert isinstance(tier, ModelTier), f"Strategy '{name}': {key} → {tier} is not a ModelTier"


# ---------------------------------------------------------------------------
# Quota-aware fallback (get_available_tiers)
# ---------------------------------------------------------------------------

class TestGetAvailableTiers:
    """Test that get_available_tiers respects routing strategy + quota fallback."""

    @pytest.mark.asyncio
    async def test_no_quota_manager_returns_recommended(self):
        """Without quota manager, behaves like recommend_tier."""
        result = await get_available_tiers("code", "simple", quota_manager=None)
        assert result == recommend_tier("code", "simple")

    @pytest.mark.asyncio
    async def test_ollama_always_returns_ollama(self):
        """Ollama has no limits — should always return Ollama."""
        quota = AsyncMock()
        with patch("backend.services.model_router.cfg", return_value="cheapest"):
            result = await get_available_tiers("analysis", "simple", quota_manager=quota)
        assert result == ModelTier.OLLAMA
        quota.is_provider_available.assert_not_called()

    @pytest.mark.asyncio
    async def test_available_provider_returns_recommended(self):
        """When recommended provider is available, return it."""
        quota = AsyncMock()
        quota.is_provider_available.return_value = True
        result = await get_available_tiers("code", "simple", quota_manager=quota)
        assert result == ModelTier.CLAUDE_CODE  # "best" default

    @pytest.mark.asyncio
    async def test_hot_provider_falls_back(self):
        """When recommended provider is hot, fall back to next available."""
        quota = AsyncMock()
        # Claude Code hot, Gemini available
        async def is_available(provider):
            return provider != "claude_code"
        quota.is_provider_available.side_effect = is_available

        result = await get_available_tiers("code", "simple", quota_manager=quota)
        assert result == ModelTier.GEMINI_CLI

    @pytest.mark.asyncio
    async def test_all_hot_falls_back_to_ollama(self):
        """When all cloud providers are hot, force Ollama."""
        quota = AsyncMock()
        quota.is_provider_available.return_value = False
        result = await get_available_tiers("code", "simple", quota_manager=quota)
        assert result == ModelTier.OLLAMA
