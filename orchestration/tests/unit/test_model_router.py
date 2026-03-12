#  Orchestration Engine - Model Router Tests
#
#  Unit tests for model tier selection, cost calculation, and tool recommendations.
#  Pure functions — no I/O, no database.
#
#  Depends on: backend/services/model_router.py, backend/models/enums.py
#  Used by:    pytest

from unittest.mock import patch

from backend.models.enums import ModelTier
from backend.services.model_router import (
    calculate_cost,
    estimate_task_cost,
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
    def test_research_simple_is_ollama(self):
        assert recommend_tier("research", "simple") == ModelTier.OLLAMA

    def test_code_medium_is_sonnet(self):
        assert recommend_tier("code", "medium") == ModelTier.SONNET

    def test_asset_complex_is_ollama(self):
        """Asset tasks should use Ollama regardless of complexity."""
        assert recommend_tier("asset", "complex") == ModelTier.OLLAMA

    def test_unknown_type_defaults_to_haiku(self):
        assert recommend_tier("unknown_type", "medium") == ModelTier.HAIKU

    def test_unknown_complexity_defaults_to_haiku(self):
        assert recommend_tier("code", "extreme") == ModelTier.HAIKU


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
