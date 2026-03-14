#  Orchestration Engine - C# Planner Prompt Tests
#
#  Tests for the C# reflection-based planning strategy.
#
#  Depends on: backend/services/planner.py, backend/services/llm_router.py
#  Used by:    CI

from unittest.mock import AsyncMock, patch

from backend.services.llm_router import LLMResponse
from backend.services.planner import (
    PlannerService,
    _build_csharp_system_prompt,
    _build_system_prompt,
)
from backend.models.enums import PlanningRigor


class TestCsharpSystemPrompt:
    def test_includes_type_map(self):
        type_map = "class MyApp.UserService\n  public async Task<User> GetUser(Guid id)"
        prompt = _build_csharp_system_prompt(type_map)
        assert "<reflected_types>" in prompt
        assert "UserService" in prompt
        assert "GetUser" in prompt

    def test_includes_csharp_preamble(self):
        prompt = _build_csharp_system_prompt("some types")
        assert "C# code architect" in prompt
        assert "method-level implementation tasks" in prompt

    def test_includes_task_schema(self):
        prompt = _build_csharp_system_prompt("types")
        assert "target_signature" in prompt
        assert "target_class" in prompt
        assert "available_methods" in prompt
        assert "constructor_params" in prompt

    def test_includes_strategy_rules(self):
        prompt = _build_csharp_system_prompt("types")
        assert "50 lines" in prompt
        assert "assembly task" in prompt
        assert "dotnet build" in prompt

    def test_does_not_include_generic_preamble(self):
        prompt = _build_csharp_system_prompt("types")
        # Should NOT have the generic planner's task_type list
        assert "task_type \"research\"" not in prompt

    def test_generic_prompt_unchanged(self):
        """Verify the generic prompt path still works."""
        prompt = _build_system_prompt(PlanningRigor.L2)
        assert "project planner" in prompt
        assert "reflected_types" not in prompt


def _make_planner_db_mock(config_json):
    """Create a mock db that returns the right values for PlannerService.generate()."""
    project_row = {
        "id": "proj1",
        "name": "Test",
        "requirements": "Build a user service",
        "config_json": config_json,
        "status": "draft",
    }
    version_row = {"v": 0}
    mock_db = AsyncMock()
    mock_db.fetchone = AsyncMock(side_effect=[project_row, version_row])
    mock_db.execute_write = AsyncMock()
    return mock_db


class TestPlannerServiceCsharpStrategy:
    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_csharp_strategy_calls_reflection(self, mock_call_llm):
        """When decomposition_strategy is csharp_reflection, planner calls reflection."""
        mock_call_llm.return_value = LLMResponse(
            text='{"summary": "test", "phases": []}', provider="test", model="test-model",
        )
        config = '{"decomposition_strategy": "csharp_reflection", "csproj_path": "/fake/Test.csproj"}'
        mock_db = _make_planner_db_mock(config)
        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        planner = PlannerService(db=mock_db, budget=mock_budget)

        with patch.object(planner, "_get_csharp_type_map", new_callable=AsyncMock) as mock_reflect:
            mock_reflect.return_value = "class Foo\n  public void Bar()"
            await planner.generate("proj1")

        mock_reflect.assert_called_once()

    @patch("backend.services.planner.call_llm", new_callable=AsyncMock)
    async def test_csharp_strategy_fallback_on_reflection_failure(self, mock_call_llm):
        """If reflection fails, falls back to generic planner."""
        mock_call_llm.return_value = LLMResponse(
            text='{"summary": "test", "tasks": []}', provider="test", model="test-model",
        )
        config = '{"decomposition_strategy": "csharp_reflection", "csproj_path": "/fake/Test.csproj"}'
        mock_db = _make_planner_db_mock(config)
        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        planner = PlannerService(db=mock_db, budget=mock_budget)

        with patch.object(planner, "_get_csharp_type_map", new_callable=AsyncMock) as mock_reflect:
            mock_reflect.return_value = None  # Reflection failed
            await planner.generate("proj1")

        # call_llm(system_prompt, user_msg, ...) — check system_prompt
        system_prompt = mock_call_llm.call_args[0][0]
        assert "reflected_types" not in system_prompt

    async def test_get_csharp_type_map_no_paths(self):
        """Returns None when no assembly/csproj paths are configured."""
        planner = PlannerService(db=AsyncMock(), budget=AsyncMock())
        result = await planner._get_csharp_type_map({})
        assert result is None

    async def test_get_csharp_type_map_with_assembly(self):
        """Calls reflect_assembly when assembly_path is provided."""
        planner = PlannerService(db=AsyncMock(), budget=AsyncMock())

        mock_data = {"assembly_name": "Test", "classes": []}
        with patch("backend.tools.dotnet_reflection.reflect_assembly", new_callable=AsyncMock) as mock_reflect, \
             patch("backend.tools.dotnet_reflection.format_type_map") as mock_format:
            mock_reflect.return_value = mock_data
            mock_format.return_value = "formatted"

            result = await planner._get_csharp_type_map({"assembly_path": "/fake.dll"})
            assert result == "formatted"
