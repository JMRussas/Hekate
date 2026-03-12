#  Orchestration Engine - C# Planner Prompt Tests
#
#  Tests for the C# reflection-based planning strategy.
#
#  Depends on: backend/services/planner.py
#  Used by:    CI

from unittest.mock import AsyncMock, patch

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


def _make_anthropic_mock(response_text='{"summary": "test", "phases": []}'):
    """Create a mock anthropic module + client."""
    mock_anthropic = AsyncMock()
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_response.content = [AsyncMock(text=response_text)]
    mock_response.usage = AsyncMock(input_tokens=100, output_tokens=200)
    mock_client.messages.create = AsyncMock(return_value=mock_response)
    mock_client.close = AsyncMock()
    mock_anthropic.AsyncAnthropic.return_value = mock_client
    return mock_anthropic, mock_client


class TestPlannerServiceCsharpStrategy:
    async def test_csharp_strategy_calls_reflection(self):
        """When decomposition_strategy is csharp_reflection, planner calls reflection."""
        config = '{"decomposition_strategy": "csharp_reflection", "csproj_path": "/fake/Test.csproj"}'
        mock_db = _make_planner_db_mock(config)
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)

        planner = PlannerService(db=mock_db, budget=mock_budget)

        with patch.object(planner, "_get_csharp_type_map", new_callable=AsyncMock) as mock_reflect:
            mock_reflect.return_value = "class Foo\n  public void Bar()"

            with patch("backend.services.planner.anthropic") as mock_anthropic_mod:
                mock_anthropic, mock_client = _make_anthropic_mock()
                mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

                await planner.generate("proj1")

            mock_reflect.assert_called_once()

    async def test_csharp_strategy_fallback_on_reflection_failure(self):
        """If reflection fails, falls back to generic planner."""
        config = '{"decomposition_strategy": "csharp_reflection", "csproj_path": "/fake/Test.csproj"}'
        mock_db = _make_planner_db_mock(config)
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)

        planner = PlannerService(db=mock_db, budget=mock_budget)

        with patch.object(planner, "_get_csharp_type_map", new_callable=AsyncMock) as mock_reflect:
            mock_reflect.return_value = None  # Reflection failed

            with patch("backend.services.planner.anthropic") as mock_anthropic_mod:
                mock_anthropic, mock_client = _make_anthropic_mock('{"summary": "test", "tasks": []}')
                mock_anthropic_mod.AsyncAnthropic.return_value = mock_client

                await planner.generate("proj1")

            # Should have called create with a generic prompt (no reflected_types)
            call_kwargs = mock_client.messages.create.call_args[1]
            assert "reflected_types" not in call_kwargs["system"]

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
