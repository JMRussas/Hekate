"""Tests for gods/tooling.py — language detection and tooling availability.

RED PHASE: gods/tooling.py does not exist yet.

Detects project language from file extensions, checks which analysis
services are available, and provides tooling flags for suggest_target_level().
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from gods.tooling import (
    detect_languages,
    check_tooling_availability,
    get_tooling_flags,
    ToolingInfo,
)


# ---------------------------------------------------------------------------
# detect_languages — from file extensions in project
# ---------------------------------------------------------------------------

class TestDetectLanguages:
    def test_python_project(self):
        files = [
            "backend/server.py",
            "backend/models.py",
            "tests/test_server.py",
            "requirements.txt",
        ]
        langs = detect_languages(files)
        assert "python" in langs
        assert langs["python"] > 0

    def test_csharp_project(self):
        files = [
            "Api/Program.cs",
            "Api/Controllers/HealthController.cs",
            "Api/Api.csproj",
        ]
        langs = detect_languages(files)
        assert "csharp" in langs

    def test_typescript_project(self):
        files = [
            "src/App.tsx",
            "src/components/Button.ts",
            "package.json",
            "tsconfig.json",
        ]
        langs = detect_languages(files)
        assert "typescript" in langs

    def test_cpp_project(self):
        files = [
            "src/main.cpp",
            "include/engine.h",
            "src/renderer.cc",
        ]
        langs = detect_languages(files)
        assert "cpp" in langs

    def test_mixed_project(self):
        files = [
            "backend/server.py",
            "frontend/src/App.tsx",
            "Api/Program.cs",
        ]
        langs = detect_languages(files)
        assert "python" in langs
        assert "typescript" in langs
        assert "csharp" in langs

    def test_primary_language(self):
        """Primary = most files."""
        files = [
            "a.py", "b.py", "c.py", "d.py",
            "e.ts",
        ]
        langs = detect_languages(files)
        assert langs["python"] > langs.get("typescript", 0)

    def test_empty_files(self):
        langs = detect_languages([])
        assert len(langs) == 0

    def test_no_code_files(self):
        files = ["README.md", "LICENSE", ".gitignore"]
        langs = detect_languages(files)
        assert len(langs) == 0


# ---------------------------------------------------------------------------
# check_tooling_availability — query services
# ---------------------------------------------------------------------------

class TestCheckToolingAvailability:
    @pytest.mark.asyncio
    async def test_all_services_up(self):
        with patch("gods.tooling._check_service", new_callable=AsyncMock) as mock:
            mock.return_value = True
            info = await check_tooling_availability()
            assert info.has_roslyn is True
            assert info.has_jedi is True
            assert info.has_ts_compiler is True

    @pytest.mark.asyncio
    async def test_roslyn_down(self):
        async def _check(url):
            return "5110" not in url  # roslyn is down
        with patch("gods.tooling._check_service", side_effect=_check):
            info = await check_tooling_availability()
            assert info.has_roslyn is False
            assert info.has_jedi is True

    @pytest.mark.asyncio
    async def test_all_down(self):
        with patch("gods.tooling._check_service", new_callable=AsyncMock) as mock:
            mock.return_value = False
            info = await check_tooling_availability()
            assert info.has_roslyn is False
            assert info.has_jedi is False
            assert info.has_ts_compiler is False


# ---------------------------------------------------------------------------
# get_tooling_flags — combines detection + availability for a project
# ---------------------------------------------------------------------------

class TestGetToolingFlags:
    @pytest.mark.asyncio
    async def test_python_project_with_jedi(self):
        files = ["server.py", "models.py"]
        with patch("gods.tooling.check_tooling_availability", new_callable=AsyncMock) as mock:
            mock.return_value = ToolingInfo(has_roslyn=True, has_jedi=True, has_ts_compiler=True)
            flags = await get_tooling_flags(files)
            # Python project — jedi matters, roslyn doesn't
            assert flags["has_jedi"] is True
            assert flags["has_roslyn"] is False  # not relevant for python

    @pytest.mark.asyncio
    async def test_csharp_project_with_roslyn(self):
        files = ["Program.cs", "Controller.cs"]
        with patch("gods.tooling.check_tooling_availability", new_callable=AsyncMock) as mock:
            mock.return_value = ToolingInfo(has_roslyn=True, has_jedi=True, has_ts_compiler=True)
            flags = await get_tooling_flags(files)
            assert flags["has_roslyn"] is True
            assert flags["has_jedi"] is False  # not relevant for C#

    @pytest.mark.asyncio
    async def test_typescript_project_with_compiler(self):
        files = ["App.tsx", "index.ts"]
        with patch("gods.tooling.check_tooling_availability", new_callable=AsyncMock) as mock:
            mock.return_value = ToolingInfo(has_roslyn=True, has_jedi=True, has_ts_compiler=True)
            flags = await get_tooling_flags(files)
            assert flags["has_ts_compiler"] is True
            assert flags["has_roslyn"] is False

    @pytest.mark.asyncio
    async def test_mixed_project_gets_all_relevant(self):
        files = ["server.py", "App.tsx", "Program.cs"]
        with patch("gods.tooling.check_tooling_availability", new_callable=AsyncMock) as mock:
            mock.return_value = ToolingInfo(has_roslyn=True, has_jedi=True, has_ts_compiler=True)
            flags = await get_tooling_flags(files)
            assert flags["has_jedi"] is True
            assert flags["has_ts_compiler"] is True
            assert flags["has_roslyn"] is True

    @pytest.mark.asyncio
    async def test_python_project_jedi_down(self):
        files = ["server.py"]
        with patch("gods.tooling.check_tooling_availability", new_callable=AsyncMock) as mock:
            mock.return_value = ToolingInfo(has_roslyn=True, has_jedi=False, has_ts_compiler=True)
            flags = await get_tooling_flags(files)
            assert flags["has_jedi"] is False  # jedi is down
