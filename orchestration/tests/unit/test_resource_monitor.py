#  Orchestration Engine - Resource Monitor Tests
#
#  Tests for health check helpers and ResourceMonitor class.
#
#  Depends on: backend/services/resource_monitor.py
#  Used by:    pytest

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from backend.models.enums import ResourceStatus
from backend.services.resource_monitor import (
    ResourceDef,
    ResourceMonitor,
    _check_http,
    _check_resource,
    _check_tcp,
)


# ---------------------------------------------------------------------------
# _check_tcp
# ---------------------------------------------------------------------------

class TestCheckTcp:
    async def test_success(self):
        """Successful TCP connection returns True."""
        mock_writer = MagicMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        with patch("backend.services.resource_monitor.asyncio.wait_for",
                    new_callable=AsyncMock, return_value=(MagicMock(), mock_writer)):
            result = await _check_tcp("localhost", 11434)
        assert result is True
        mock_writer.close.assert_called_once()

    async def test_connection_refused(self):
        """Connection refused returns False."""
        with patch("backend.services.resource_monitor.asyncio.wait_for",
                    new_callable=AsyncMock, side_effect=ConnectionRefusedError):
            result = await _check_tcp("localhost", 11434)
        assert result is False

    async def test_timeout(self):
        """Timeout returns False."""
        with patch("backend.services.resource_monitor.asyncio.wait_for",
                    new_callable=AsyncMock, side_effect=asyncio.TimeoutError):
            result = await _check_tcp("localhost", 11434)
        assert result is False


# ---------------------------------------------------------------------------
# _check_http
# ---------------------------------------------------------------------------

class TestCheckHttp:
    async def test_success_json(self):
        """200 with JSON body returns (True, parsed_dict)."""
        client = AsyncMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"models": []}
        client.get = AsyncMock(return_value=resp)

        ok, data = await _check_http("http://localhost:11434/api/tags", client)
        assert ok is True
        assert data == {"models": []}

    async def test_success_non_json(self):
        """200 with non-JSON body returns (True, {})."""
        client = AsyncMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        client.get = AsyncMock(return_value=resp)

        ok, data = await _check_http("http://localhost:11434/api/tags", client)
        assert ok is True
        assert data == {}

    async def test_server_error(self):
        """500 status returns (False, {})."""
        client = AsyncMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.status_code = 500
        client.get = AsyncMock(return_value=resp)

        ok, data = await _check_http("http://localhost:11434/api/tags", client)
        assert ok is False
        assert data == {}

    async def test_connection_error(self):
        """Connection error returns (False, {})."""
        client = AsyncMock(spec=httpx.AsyncClient)
        client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        ok, data = await _check_http("http://localhost:11434/api/tags", client)
        assert ok is False
        assert data == {}


# ---------------------------------------------------------------------------
# _check_resource
# ---------------------------------------------------------------------------

class TestCheckResource:
    async def test_anthropic_with_key(self):
        """Claude API with key configured → ONLINE."""
        res = ResourceDef(id="anthropic_api", name="Claude API",
                          host="api.anthropic.com", port=443,
                          health_url=None, category="api")
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch("backend.services.resource_monitor.ANTHROPIC_API_KEY", "sk-test"):
            state = await _check_resource(res, client)
        assert state.status == ResourceStatus.ONLINE
        assert state.method == "api_key"
        assert state.details["key_configured"] is True

    async def test_anthropic_without_key(self):
        """Claude API without key → OFFLINE."""
        res = ResourceDef(id="anthropic_api", name="Claude API",
                          host="api.anthropic.com", port=443,
                          health_url=None, category="api")
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch("backend.services.resource_monitor.ANTHROPIC_API_KEY", ""):
            state = await _check_resource(res, client)
        assert state.status == ResourceStatus.OFFLINE
        assert state.details["key_configured"] is False

    async def test_ollama_http_success(self):
        """Ollama with health URL → ONLINE, extracts model names."""
        res = ResourceDef(id="ollama_local", name="Ollama (local)",
                          host="localhost", port=11434,
                          health_url="http://localhost:11434/api/tags")
        client = AsyncMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"models": [{"name": "qwen2.5-coder:14b"}]}
        client.get = AsyncMock(return_value=resp)

        state = await _check_resource(res, client)
        assert state.status == ResourceStatus.ONLINE
        assert state.method == "http"
        assert state.details["models"] == ["qwen2.5-coder:14b"]

    async def test_tcp_fallback(self):
        """No health URL, TCP fallback → ONLINE."""
        res = ResourceDef(id="custom_svc", name="Custom",
                          host="localhost", port=9999,
                          health_url=None)
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch("backend.services.resource_monitor._check_tcp",
                    new_callable=AsyncMock, return_value=True):
            state = await _check_resource(res, client)
        assert state.status == ResourceStatus.ONLINE
        assert state.method == "tcp"

    async def test_all_checks_fail(self):
        """No health URL, TCP fails → OFFLINE."""
        res = ResourceDef(id="custom_svc", name="Custom",
                          host="localhost", port=9999,
                          health_url=None)
        client = AsyncMock(spec=httpx.AsyncClient)

        with patch("backend.services.resource_monitor._check_tcp",
                    new_callable=AsyncMock, return_value=False):
            state = await _check_resource(res, client)
        assert state.status == ResourceStatus.OFFLINE
        assert state.method == "none"


# ---------------------------------------------------------------------------
# ResourceMonitor class
# ---------------------------------------------------------------------------

class TestResourceMonitor:
    async def test_check_all_populates_cache(self):
        """check_all() populates cache; get()/is_available() return correct values."""
        monitor = ResourceMonitor()
        # Replace resources with a single test resource (reset cache too)
        monitor._resources = [
            ResourceDef(id="anthropic_api", name="Claude API",
                        host="api.anthropic.com", port=443,
                        health_url=None, category="api")
        ]
        monitor._states = {}

        with patch("backend.services.resource_monitor.ANTHROPIC_API_KEY", "sk-test"):
            states = await monitor.check_all()

        assert len(states) == 1
        assert states[0].status == ResourceStatus.ONLINE

        # get() returns cached state
        cached = monitor.get("anthropic_api")
        assert cached is not None
        assert cached.status == ResourceStatus.ONLINE

        # is_available() reflects status
        assert monitor.is_available("anthropic_api") is True
        assert monitor.is_available("nonexistent") is False

        # get_all() returns list
        assert len(monitor.get_all()) == 1

        # Cleanup
        if monitor._http:
            await monitor._http.aclose()

    async def test_cache_seeded_with_checking_status(self):
        """get_all() returns all resources with 'checking' status before any check runs."""
        monitor = ResourceMonitor()
        states = monitor.get_all()
        assert len(states) > 0
        for s in states:
            assert s.status == ResourceStatus.CHECKING

    async def test_start_stop_background(self):
        """start_background creates task; stop_background cancels and closes client."""
        monitor = ResourceMonitor()
        monitor._resources = []

        await monitor.start_background()
        assert monitor._task is not None
        assert monitor._http is not None

        await monitor.stop_background()
        assert monitor._http is None

    async def test_check_loop_handles_errors(self):
        """_check_loop logs errors without crashing."""
        monitor = ResourceMonitor()
        monitor._http = AsyncMock(spec=httpx.AsyncClient)

        call_count = 0

        async def mock_check_all():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient failure")
            # Let second call succeed to prove loop continues

        monitor.check_all = mock_check_all

        with patch("backend.services.resource_monitor.RESOURCE_CHECK_INTERVAL", 0.01):
            task = asyncio.create_task(monitor._check_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Loop continued past the error
        assert call_count >= 2
