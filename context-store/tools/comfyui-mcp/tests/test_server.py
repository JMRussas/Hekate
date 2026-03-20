"""Tests for the ComfyUI FastMCP server.

Validates tool registration and correct handling of mocked ComfyUI API responses.
All httpx calls are mocked — no live ComfyUI backend required.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src import server


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """All four required tools must be registered on the FastMCP instance."""

    def _tool_names(self):
        return {t.name for t in server.mcp._tool_manager.list_tools()}

    def test_generate_image_registered(self):
        assert "generate_image" in self._tool_names()

    def test_list_workflows_registered(self):
        assert "list_workflows" in self._tool_names()

    def test_queue_status_registered(self):
        assert "queue_status" in self._tool_names()

    def test_get_result_registered(self):
        assert "get_result" in self._tool_names()

    def test_exactly_four_tools(self):
        assert self._tool_names() == {
            "generate_image",
            "list_workflows",
            "queue_status",
            "get_result",
        }


# ---------------------------------------------------------------------------
# list_workflows
# ---------------------------------------------------------------------------


class TestListWorkflows:

    @pytest.mark.asyncio
    async def test_returns_workflow_files(self, tmp_path):
        wf = {"1": {"class_type": "KSampler", "inputs": {}}}
        (tmp_path / "landscape.json").write_text(json.dumps(wf))
        (tmp_path / "portrait.json").write_text(json.dumps(wf))
        (tmp_path / "readme.txt").write_text("")  # non-json ignored

        with patch.object(server, "WORKFLOWS_DIR", str(tmp_path)):
            result = await server.list_workflows()

        assert result["count"] == 2
        names = sorted(w["name"] for w in result["workflows"])
        assert names == ["landscape", "portrait"]

    @pytest.mark.asyncio
    async def test_reports_node_count(self, tmp_path):
        wf = {"1": {}, "2": {}, "3": {}}
        (tmp_path / "big.json").write_text(json.dumps(wf))

        with patch.object(server, "WORKFLOWS_DIR", str(tmp_path)):
            result = await server.list_workflows()

        assert result["workflows"][0]["node_count"] == 3

    @pytest.mark.asyncio
    async def test_empty_dir(self, tmp_path):
        with patch.object(server, "WORKFLOWS_DIR", str(tmp_path)):
            result = await server.list_workflows()
        assert result["count"] == 0
        assert result["workflows"] == []

    @pytest.mark.asyncio
    async def test_missing_dir(self, tmp_path):
        with patch.object(server, "WORKFLOWS_DIR", str(tmp_path / "nope")):
            result = await server.list_workflows()
        assert result["count"] == 0
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handles_bad_json(self, tmp_path):
        (tmp_path / "broken.json").write_text("{not json")
        with patch.object(server, "WORKFLOWS_DIR", str(tmp_path)):
            result = await server.list_workflows()
        assert result["count"] == 1
        assert "error" in result["workflows"][0]


# ---------------------------------------------------------------------------
# queue_status
# ---------------------------------------------------------------------------


class TestQueueStatus:

    @pytest.mark.asyncio
    async def test_returns_counts(self):
        mock_resp = httpx.Response(
            200,
            json={
                "queue_pending": [[0, "aaa", {}, {}, []]],
                "queue_running": [[0, "bbb", {}, {}, []]],
            },
            request=httpx.Request("GET", "http://test/queue"),
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            result = await server.queue_status()

        assert result["pending_count"] == 1
        assert result["running_count"] == 1
        assert result["pending"][0]["prompt_id"] == "aaa"
        assert result["running"][0]["prompt_id"] == "bbb"

    @pytest.mark.asyncio
    async def test_empty_queue(self):
        mock_resp = httpx.Response(
            200,
            json={"queue_pending": [], "queue_running": []},
            request=httpx.Request("GET", "http://test/queue"),
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            result = await server.queue_status()

        assert result["pending_count"] == 0
        assert result["running_count"] == 0

    @pytest.mark.asyncio
    async def test_comfyui_unreachable(self):
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("connection refused"),
        ):
            result = await server.queue_status()

        assert "error" in result
        assert result["pending_count"] == 0
        assert result["running_count"] == 0


# ---------------------------------------------------------------------------
# generate_image
# ---------------------------------------------------------------------------


class TestGenerateImage:

    @staticmethod
    def _sample_workflow():
        return {
            "3": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 20}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        }

    @pytest.mark.asyncio
    async def test_queues_job(self, tmp_path):
        (tmp_path / "default.json").write_text(json.dumps(self._sample_workflow()))

        mock_resp = httpx.Response(
            200,
            json={"prompt_id": "pid-123"},
            request=httpx.Request("POST", "http://test/prompt"),
        )

        with (
            patch.object(server, "WORKFLOWS_DIR", str(tmp_path)),
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            result = await server.generate_image("a mountain at sunset", "default", 1024, 768, 42)

        assert result["prompt_id"] == "pid-123"
        assert result["status"] == "queued"
        assert result["seed"] == 42
        assert result["width"] == 1024
        assert result["height"] == 768
        assert result["workflow"] == "default"

    @pytest.mark.asyncio
    async def test_injects_prompt_into_workflow(self, tmp_path):
        (tmp_path / "wf.json").write_text(json.dumps(self._sample_workflow()))

        captured_payload = {}

        async def capture_post(url, **kwargs):
            captured_payload.update(kwargs.get("json", {}))
            return httpx.Response(
                200,
                json={"prompt_id": "x"},
                request=httpx.Request("POST", url),
            )

        with (
            patch.object(server, "WORKFLOWS_DIR", str(tmp_path)),
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=capture_post),
        ):
            await server.generate_image("a cat", "wf", 768, 768, 99)

        wf = captured_payload["prompt"]
        assert wf["6"]["inputs"]["text"] == "a cat"
        assert wf["3"]["inputs"]["seed"] == 99
        assert wf["5"]["inputs"]["width"] == 768
        assert wf["5"]["inputs"]["height"] == 768

    @pytest.mark.asyncio
    async def test_random_seed_when_negative(self, tmp_path):
        (tmp_path / "wf.json").write_text(json.dumps(self._sample_workflow()))

        mock_resp = httpx.Response(
            200,
            json={"prompt_id": "x"},
            request=httpx.Request("POST", "http://test/prompt"),
        )

        with (
            patch.object(server, "WORKFLOWS_DIR", str(tmp_path)),
            patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp),
        ):
            result = await server.generate_image("test", "wf", seed=-1)

        assert result["seed"] >= 0

    @pytest.mark.asyncio
    async def test_workflow_not_found(self, tmp_path):
        with patch.object(server, "WORKFLOWS_DIR", str(tmp_path)):
            result = await server.generate_image("test", "nonexistent")
        assert "error" in result
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_comfyui_unreachable(self, tmp_path):
        (tmp_path / "wf.json").write_text(json.dumps(self._sample_workflow()))

        with (
            patch.object(server, "WORKFLOWS_DIR", str(tmp_path)),
            patch(
                "httpx.AsyncClient.post",
                new_callable=AsyncMock,
                side_effect=httpx.ConnectError("refused"),
            ),
        ):
            result = await server.generate_image("test", "wf")
        assert "error" in result


# ---------------------------------------------------------------------------
# get_result
# ---------------------------------------------------------------------------


class TestGetResult:

    @pytest.mark.asyncio
    async def test_completed_job(self):
        history = {
            "pid-1": {
                "outputs": {
                    "9": {
                        "images": [
                            {"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}
                        ]
                    }
                },
                "status": {"completed": True},
            }
        }
        mock_resp = httpx.Response(
            200,
            json=history,
            request=httpx.Request("GET", "http://test/history/pid-1"),
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            result = await server.get_result("pid-1")

        assert result["status"] == "completed"
        assert result["image_count"] == 1
        assert result["images"][0]["filename"] == "ComfyUI_00001_.png"
        assert "url" in result["images"][0]

    @pytest.mark.asyncio
    async def test_failed_job(self):
        history = {
            "pid-f": {
                "outputs": {},
                "status": {"completed": False},
            }
        }
        mock_resp = httpx.Response(
            200,
            json=history,
            request=httpx.Request("GET", "http://test/history/pid-f"),
        )
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            result = await server.get_result("pid-f")

        assert result["status"] == "failed"
        assert result["image_count"] == 0

    @pytest.mark.asyncio
    async def test_still_running(self):
        """Not in history but found in queue_running."""
        history_resp = httpx.Response(200, json={}, request=httpx.Request("GET", "http://test/history/pid-2"))
        queue_resp = httpx.Response(
            200,
            json={"queue_pending": [], "queue_running": [[0, "pid-2", {}, {}, []]]},
            request=httpx.Request("GET", "http://test/queue"),
        )

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            return history_resp if call_count == 1 else queue_resp

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
            result = await server.get_result("pid-2")

        assert result["status"] == "running"

    @pytest.mark.asyncio
    async def test_still_pending(self):
        """Not in history but found in queue_pending."""
        history_resp = httpx.Response(200, json={}, request=httpx.Request("GET", "http://test/history/pid-3"))
        queue_resp = httpx.Response(
            200,
            json={"queue_pending": [[0, "pid-3", {}, {}, []]], "queue_running": []},
            request=httpx.Request("GET", "http://test/queue"),
        )

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            return history_resp if call_count == 1 else queue_resp

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
            result = await server.get_result("pid-3")

        assert result["status"] == "pending"

    @pytest.mark.asyncio
    async def test_not_found(self):
        """Not in history and not in queue."""
        history_resp = httpx.Response(200, json={}, request=httpx.Request("GET", "http://test/history/pid-4"))
        queue_resp = httpx.Response(
            200,
            json={"queue_pending": [], "queue_running": []},
            request=httpx.Request("GET", "http://test/queue"),
        )

        call_count = 0

        async def mock_get(url, **kwargs):
            nonlocal call_count
            call_count += 1
            return history_resp if call_count == 1 else queue_resp

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=mock_get):
            result = await server.get_result("pid-4")

        assert result["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_http_error(self):
        with patch(
            "httpx.AsyncClient.get",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("down"),
        ):
            result = await server.get_result("pid-x")

        assert "error" in result
        assert result["prompt_id"] == "pid-x"
