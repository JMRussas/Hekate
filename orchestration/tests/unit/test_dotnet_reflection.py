#  Orchestration Engine - .NET Reflection Tool Tests
#
#  Tests for the Python wrapper around the dotnet-reflector.
#  Uses mocked subprocess calls so CI doesn't need .NET SDK.
#
#  Depends on: conftest.py, backend/tools/dotnet_reflection.py
#  Used by:    CI

import json
from unittest.mock import AsyncMock, patch

import pytest

from backend.tools.dotnet_reflection import (
    DotNetReflectionTool,
    build_project,
    format_type_map,
    reflect_assembly,
)


# Sample reflection output matching the reflector's JSON schema
SAMPLE_REFLECTION = {
    "assembly_name": "MyApp",
    "classes": [
        {
            "name": "UserService",
            "namespace": "MyApp.Services",
            "kind": "class",
            "base_class": None,
            "interfaces": ["IUserService"],
            "constructors": [
                {"parameters": [{"name": "db", "type": "IDbContext"}, {"name": "logger", "type": "ILogger"}]}
            ],
            "methods": [
                {
                    "name": "GetUser",
                    "return_type": "Task<User>",
                    "parameters": [{"name": "id", "type": "Guid"}],
                    "accessibility": "public",
                    "is_async": True,
                    "is_static": None,
                    "signature": "public async Task<User> GetUser(Guid id)",
                },
                {
                    "name": "ValidateEmail",
                    "return_type": "bool",
                    "parameters": [{"name": "email", "type": "string"}],
                    "accessibility": "private",
                    "is_async": None,
                    "is_static": None,
                    "signature": "private bool ValidateEmail(string email)",
                },
            ],
            "properties": [
                {"name": "CacheEnabled", "type": "bool", "has_getter": True, "has_setter": True}
            ],
        },
        {
            "name": "IUserService",
            "namespace": "MyApp.Services",
            "kind": "interface",
            "methods": [
                {
                    "name": "GetUser",
                    "return_type": "Task<User>",
                    "parameters": [{"name": "id", "type": "Guid"}],
                    "accessibility": "public",
                    "is_async": True,
                    "is_static": None,
                    "signature": "public async Task<User> GetUser(Guid id)",
                }
            ],
        },
    ],
}


class TestFormatTypeMap:
    def test_formats_class_with_methods(self):
        result = format_type_map(SAMPLE_REFLECTION)
        assert "Assembly: MyApp" in result
        assert "class MyApp.Services.UserService : IUserService" in result
        assert "public async Task<User> GetUser(Guid id)" in result
        assert "private bool ValidateEmail(string email)" in result

    def test_formats_constructor(self):
        result = format_type_map(SAMPLE_REFLECTION)
        assert "ctor(IDbContext db, ILogger logger)" in result

    def test_formats_properties(self):
        result = format_type_map(SAMPLE_REFLECTION)
        assert "bool CacheEnabled { get; set; }" in result

    def test_formats_interface(self):
        result = format_type_map(SAMPLE_REFLECTION)
        assert "interface MyApp.Services.IUserService" in result

    def test_empty_assembly(self):
        result = format_type_map({"assembly_name": "Empty", "classes": []})
        assert "Assembly: Empty" in result


class TestReflectAssembly:
    async def test_calls_reflector_subprocess(self):
        mock_output = json.dumps(SAMPLE_REFLECTION)
        with patch(
            "backend.tools.dotnet_reflection._run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, mock_output, ""),
        ), patch(
            "backend.tools.dotnet_reflection._REFLECTOR_DIR",
        ) as mock_dir:
            # Simulate reflector DLL exists
            mock_dir.__truediv__ = lambda self, x: type("P", (), {
                "__truediv__": lambda s, y: type("P2", (), {
                    "__truediv__": lambda s2, z: type("P3", (), {
                        "__truediv__": lambda s3, w: type("P4", (), {
                            "exists": lambda s4: True, "__str__": lambda s4: "/fake/reflector.dll"
                        })()
                    })()
                })()
            })()

            result = await reflect_assembly("/fake/assembly.dll")
            assert result["assembly_name"] == "MyApp"
            assert len(result["classes"]) == 2

    async def test_raises_on_failure(self):
        with patch(
            "backend.tools.dotnet_reflection._run_subprocess",
            new_callable=AsyncMock,
            return_value=(1, "", "Some error"),
        ), patch(
            "backend.tools.dotnet_reflection._REFLECTOR_DIR",
        ) as mock_dir:
            mock_dir.__truediv__ = lambda self, x: type("P", (), {
                "__truediv__": lambda s, y: type("P2", (), {
                    "__truediv__": lambda s2, z: type("P3", (), {
                        "__truediv__": lambda s3, w: type("P4", (), {
                            "exists": lambda s4: True, "__str__": lambda s4: "/fake/reflector.dll"
                        })()
                    })()
                })()
            })()

            with pytest.raises(RuntimeError, match="Reflection failed"):
                await reflect_assembly("/fake/assembly.dll")


class TestBuildProject:
    async def test_build_success(self, tmp_path):
        # Create a fake csproj and output DLL
        csproj = tmp_path / "Test.csproj"
        csproj.write_text("<Project/>")
        dll_dir = tmp_path / "bin" / "Release" / "net8.0"
        dll_dir.mkdir(parents=True)
        (dll_dir / "Test.dll").write_text("fake")

        with patch(
            "backend.tools.dotnet_reflection._run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, "Build succeeded", ""),
        ):
            success, result = await build_project(str(csproj))
            assert success is True
            assert result.endswith("Test.dll")

    async def test_build_failure(self, tmp_path):
        csproj = tmp_path / "Test.csproj"
        csproj.write_text("<Project/>")

        with patch(
            "backend.tools.dotnet_reflection._run_subprocess",
            new_callable=AsyncMock,
            return_value=(1, "", "error CS1234: something broke"),
        ):
            success, result = await build_project(str(csproj))
            assert success is False
            assert "Build failed" in result

    async def test_missing_csproj(self):
        success, result = await build_project("/nonexistent/Test.csproj")
        assert success is False
        assert "not found" in result


class TestDotNetReflectionTool:
    def test_tool_metadata(self):
        tool = DotNetReflectionTool()
        assert tool.name == "dotnet_reflection"
        assert "assembly_path" in tool.parameters["properties"]
        assert "csproj_path" in tool.parameters["properties"]

    async def test_execute_with_assembly(self):
        mock_output = json.dumps(SAMPLE_REFLECTION)
        with patch(
            "backend.tools.dotnet_reflection._run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, mock_output, ""),
        ), patch(
            "backend.tools.dotnet_reflection._REFLECTOR_DIR",
        ) as mock_dir:
            mock_dir.__truediv__ = lambda self, x: type("P", (), {
                "__truediv__": lambda s, y: type("P2", (), {
                    "__truediv__": lambda s2, z: type("P3", (), {
                        "__truediv__": lambda s3, w: type("P4", (), {
                            "exists": lambda s4: True, "__str__": lambda s4: "/fake/reflector.dll"
                        })()
                    })()
                })()
            })()

            tool = DotNetReflectionTool()
            result = await tool.execute({"assembly_path": "/fake/assembly.dll"})
            assert "UserService" in result
            assert "GetUser" in result

    async def test_execute_no_paths_returns_error(self):
        tool = DotNetReflectionTool()
        result = await tool.execute({})
        assert "Error" in result

    async def test_to_claude_tool(self):
        tool = DotNetReflectionTool()
        claude_def = tool.to_claude_tool()
        assert claude_def["name"] == "dotnet_reflection"
        assert "input_schema" in claude_def
