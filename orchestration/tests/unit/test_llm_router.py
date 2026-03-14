#  Orchestration Engine - LLM Router Tests
#
#  Tests for CLI command resolution and provider routing.
#
#  Depends on: backend/services/llm_router.py
#  Used by:    pytest

import os
import sys
from unittest.mock import patch

import pytest

from backend.services.llm_router import _resolve_cmd


class TestResolveCmd:

    def test_returns_which_result_when_on_path(self):
        """If shutil.which finds the command, return it directly."""
        with patch("shutil.which", return_value="/usr/bin/gemini"):
            assert _resolve_cmd("gemini") == "/usr/bin/gemini"

    def test_returns_name_when_not_found_on_linux(self):
        """On non-Windows, falls through to returning the bare name."""
        with patch("shutil.which", return_value=None), \
             patch.object(sys, "platform", "linux"):
            assert _resolve_cmd("gemini") == "gemini"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only fallback")
    def test_falls_back_to_npm_global_on_windows(self, tmp_path):
        """When shutil.which fails on Windows, checks %APPDATA%/npm for .cmd files."""
        # Create a fake npm bin with a .cmd file
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        cmd_file = npm_dir / "gemini.cmd"
        cmd_file.write_text("@echo off")

        with patch("shutil.which", return_value=None), \
             patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            result = _resolve_cmd("gemini")
            assert result == str(cmd_file)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only fallback")
    def test_npm_fallback_prefers_cmd_over_exe(self, tmp_path):
        """The .cmd extension is checked before .exe."""
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        (npm_dir / "claude.cmd").write_text("@echo off")
        (npm_dir / "claude.exe").write_text("")

        with patch("shutil.which", return_value=None), \
             patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            result = _resolve_cmd("claude")
            assert result.endswith(".cmd")

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only fallback")
    def test_npm_fallback_returns_name_when_not_found(self, tmp_path):
        """If the command isn't in npm global bin either, return the bare name."""
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()

        with patch("shutil.which", return_value=None), \
             patch.dict(os.environ, {"APPDATA": str(tmp_path)}):
            assert _resolve_cmd("nonexistent") == "nonexistent"
