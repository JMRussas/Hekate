#  Orchestration Engine - MCP Server Tests
#
#  Tests for MCP server configuration and creation.
#
#  Depends on: backend/mcp/server.py
#  Used by:    CI

import json
import os
import tempfile

import pytest
from pathlib import Path


class TestMcpServer:
    def test_server_creation_from_config(self):
        """Test that the FastMCP server can be created from a valid config."""
        config = {
            "api_url": "http://localhost:5200",
            "api_key": "orch_test_key_123",
            "timeout": 30,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir=tempfile.gettempdir()
        ) as f:
            json.dump(config, f)
            config_path = f.name

        try:
            from backend.mcp.server import create_server
            server = create_server(config_path=Path(config_path))
            assert server is not None
        finally:
            os.unlink(config_path)

    def test_missing_config_exits(self):
        """Test that missing config file calls sys.exit."""
        from backend.mcp.server import create_server

        with pytest.raises(SystemExit):
            create_server(config_path=Path("/nonexistent/config.json"))

    def test_missing_api_key_exits(self):
        """Test that config without api_key calls sys.exit."""
        config = {"api_url": "http://localhost:5200"}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir=tempfile.gettempdir()
        ) as f:
            json.dump(config, f)
            config_path = f.name

        try:
            from backend.mcp.server import create_server

            with pytest.raises(SystemExit):
                create_server(config_path=Path(config_path))
        finally:
            os.unlink(config_path)
