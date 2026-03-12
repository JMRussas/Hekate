#  Orchestration Engine - Tool Registry Unit Tests
#
#  Tests for the injectable ToolRegistry class.
#
#  Depends on: backend/tools/registry.py
#  Used by:    pytest

from unittest.mock import patch

from backend.tools.registry import ToolRegistry


class TestToolRegistry:
    def test_registers_default_tools(self):
        registry = ToolRegistry()
        names = registry.all_names()
        assert "read_file" in names
        assert "write_file" in names
        assert "search_knowledge" in names
        assert "lookup_type" in names
        assert "local_llm" in names
        assert "generate_image" in names

    def test_get_existing_tool(self):
        registry = ToolRegistry()
        tool = registry.get("read_file")
        assert tool is not None
        assert tool.name == "read_file"

    def test_get_nonexistent_returns_none(self):
        registry = ToolRegistry()
        assert registry.get("no_such_tool") is None

    def test_get_many(self):
        registry = ToolRegistry()
        tools = registry.get_many(["read_file", "write_file"])
        assert len(tools) == 2
        assert {t.name for t in tools} == {"read_file", "write_file"}

    def test_get_many_skips_unknown(self):
        registry = ToolRegistry()
        tools = registry.get_many(["read_file", "nope", "write_file"])
        assert len(tools) == 2

    def test_get_many_empty_list(self):
        registry = ToolRegistry()
        tools = registry.get_many([])
        assert tools == []

    def test_all_names_returns_list(self):
        registry = ToolRegistry()
        names = registry.all_names()
        assert isinstance(names, list)
        assert len(names) >= 6

    def test_no_failed_tools_on_success(self):
        registry = ToolRegistry()
        assert registry.failed_tools == []

    def test_partial_registration_on_broken_tool(self, caplog):
        """A broken tool should be skipped; other tools still register."""
        import logging

        # Make GenerateImageTool raise during init
        with patch(
            "backend.tools.comfyui.GenerateImageTool.__init__",
            side_effect=RuntimeError("comfyui unavailable"),
        ):
            with caplog.at_level(logging.WARNING, logger="orchestration.tools.registry"):
                registry = ToolRegistry()

        # The broken tool should be in failed_tools
        assert "GenerateImageTool" in registry.failed_tools
        # Other tools should still be registered
        assert "read_file" in registry.all_names()
        assert "write_file" in registry.all_names()
        assert "local_llm" in registry.all_names()
        # Warning should have been logged
        assert "Failed to register tool GenerateImageTool" in caplog.text
