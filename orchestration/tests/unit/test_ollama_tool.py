#  Orchestration Engine - Ollama Tool Tests
#
#  Tests for LocalLLMTool.execute().
#
#  Depends on: backend/tools/ollama.py
#  Used by:    pytest

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from backend.tools.ollama import LocalLLMTool


# ---------------------------------------------------------------------------
# TestLocalLLMTool
# ---------------------------------------------------------------------------

class TestLocalLLMTool:

    @patch("backend.tools.ollama.OLLAMA_HOSTS", {"local": "http://localhost:11434"})
    @patch("backend.tools.ollama.OLLAMA_GENERATE_TIMEOUT", 30)
    async def test_success_with_shared_client(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "Hello from Ollama"}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        tool = LocalLLMTool(http_client=mock_http)
        result = await tool.execute({"prompt": "Say hello"})

        assert result == "Hello from Ollama"
        mock_http.post.assert_awaited_once()
        call_kwargs = mock_http.post.call_args
        assert "/api/generate" in call_kwargs.args[0]

    @patch("backend.tools.ollama.OLLAMA_HOSTS", {"local": "http://localhost:11434"})
    @patch("backend.tools.ollama.OLLAMA_GENERATE_TIMEOUT", 30)
    async def test_success_without_shared_client(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "Ephemeral output"}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()

        with patch("backend.tools.ollama.httpx.AsyncClient", return_value=mock_client):
            tool = LocalLLMTool(http_client=None)
            result = await tool.execute({"prompt": "Say hello"})

        assert result == "Ephemeral output"
        mock_client.post.assert_awaited_once()

    @patch("backend.tools.ollama.OLLAMA_HOSTS", {"local": "http://localhost:11434"})
    @patch("backend.tools.ollama.OLLAMA_GENERATE_TIMEOUT", 30)
    async def test_system_prompt_included(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        tool = LocalLLMTool(http_client=mock_http)
        await tool.execute({"prompt": "Hello", "system": "Be brief"})

        body = mock_http.post.call_args.kwargs["json"]
        assert body["system"] == "Be brief"

    @patch("backend.tools.ollama.OLLAMA_HOSTS", {"local": "http://localhost:11434"})
    @patch("backend.tools.ollama.OLLAMA_GENERATE_TIMEOUT", 30)
    async def test_no_system_excluded(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        tool = LocalLLMTool(http_client=mock_http)
        await tool.execute({"prompt": "Hello"})

        body = mock_http.post.call_args.kwargs["json"]
        assert "system" not in body

    @patch("backend.tools.ollama.OLLAMA_HOSTS", {"local": "http://localhost:11434"})
    async def test_connect_error_returns_error_string(self):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

        tool = LocalLLMTool(http_client=mock_http)
        result = await tool.execute({"prompt": "Hello"})

        assert "Error: Ollama not reachable at" in result

    @patch("backend.tools.ollama.OLLAMA_HOSTS", {"local": "http://localhost:11434"})
    @patch("backend.tools.ollama.OLLAMA_GENERATE_TIMEOUT", 30)
    async def test_generic_exception_returns_error_string(self):
        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=RuntimeError("boom"))

        tool = LocalLLMTool(http_client=mock_http)
        result = await tool.execute({"prompt": "Hello"})

        assert "Error: Ollama request failed: boom" in result

    @patch("backend.tools.ollama.OLLAMA_HOSTS", {"local": "http://local:11434", "server": "http://server:11434"})
    @patch("backend.tools.ollama.OLLAMA_GENERATE_TIMEOUT", 30)
    async def test_host_selection(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        tool = LocalLLMTool(http_client=mock_http)
        await tool.execute({"prompt": "Hello", "host": "server"})

        url = mock_http.post.call_args.args[0]
        assert "server:11434" in url

    @patch("backend.tools.ollama.OLLAMA_HOSTS", {"local": "http://localhost:11434"})
    @patch("backend.tools.ollama.OLLAMA_GENERATE_TIMEOUT", 30)
    async def test_model_override(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_resp)

        tool = LocalLLMTool(http_client=mock_http)
        await tool.execute({"prompt": "Hello", "model": "mistral"})

        body = mock_http.post.call_args.kwargs["json"]
        assert body["model"] == "mistral"
