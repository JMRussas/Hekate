"""Tests for ComfyUI FastMCP server — tool registration and mocked API responses."""

import json
import os
import pytest
from unittest.mock import patch, AsyncMock

import httpx
from httpx import ASGITransport
from server import app, COMFYUI_URL, WORKFLOWS_DIR


def _mock_response(status_code: int, json_data=None, text: str = ""):
    """Create an httpx.Response with a request object so raise_for_status works."""
    request = httpx.Request("GET", "http://mock")
    if json_data is not None:
        return httpx.Response(status_code, json=json_data, request=request)
    return httpx.Response(status_code, text=text, request=request)


def _mock_client(get_return=None, post_return=None, get_side_effect=None):
    """Build an AsyncMock that acts as httpx.AsyncClient context manager."""
    instance = AsyncMock()
    if get_side_effect:
        instance.get = AsyncMock(side_effect=get_side_effect)
    elif get_return:
        instance.get = AsyncMock(return_value=get_return)
    if post_return:
        instance.post = AsyncMock(return_value=post_return)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    return instance


@pytest.fixture
def client():
    """HTTPX async client wired to the FastAPI app (no live server needed)."""
    transport = ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ── Tool Registration ────────────────────────────────────────────────

class TestToolRegistration:
    """Verify all four required tools are registered as routes."""

    def _route_paths(self):
        return [route.path for route in app.routes]

    def test_list_workflows_registered(self):
        assert "/tools/list_workflows" in self._route_paths()

    def test_queue_status_registered(self):
        assert "/tools/queue_status" in self._route_paths()

    def test_generate_image_registered(self):
        assert "/tools/generate_image" in self._route_paths()

    def test_get_result_registered(self):
        assert "/tools/get_result" in self._route_paths()

    def test_health_registered(self):
        assert "/health" in self._route_paths()


# ── list_workflows ───────────────────────────────────────────────────

class TestListWorkflows:

    @pytest.mark.anyio
    async def test_returns_workflow_names(self, client, tmp_path):
        """Workflows dir with two JSON files returns their stem names."""
        (tmp_path / "landscape.json").write_text("{}")
        (tmp_path / "portrait.json").write_text("{}")
        (tmp_path / "readme.txt").write_text("")  # non-json ignored

        with patch("server.WORKFLOWS_DIR", str(tmp_path)):
            resp = await client.get("/tools/list_workflows")

        assert resp.status_code == 200
        names = sorted(resp.json())
        assert names == ["landscape", "portrait"]

    @pytest.mark.anyio
    async def test_empty_dir(self, client, tmp_path):
        with patch("server.WORKFLOWS_DIR", str(tmp_path)):
            resp = await client.get("/tools/list_workflows")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.anyio
    async def test_missing_dir(self, client):
        with patch("server.WORKFLOWS_DIR", "/nonexistent/path"):
            resp = await client.get("/tools/list_workflows")
        assert resp.status_code == 200
        assert resp.json() == []


# ── queue_status ─────────────────────────────────────────────────────

class TestQueueStatus:

    MOCK_QUEUE = {
        "queue_running": [["abc123", 1, {}, {}]],
        "queue_pending": [],
    }

    @pytest.mark.anyio
    async def test_returns_queue_data(self, client):
        mock = _mock_client(get_return=_mock_response(200, self.MOCK_QUEUE))

        with patch("server.httpx.AsyncClient", return_value=mock):
            resp = await client.get("/tools/queue_status")

        assert resp.status_code == 200
        data = resp.json()
        assert "queue_running" in data
        assert len(data["queue_running"]) == 1

    @pytest.mark.anyio
    async def test_comfyui_unreachable(self, client):
        mock = _mock_client(get_side_effect=httpx.RequestError("connection refused"))

        with patch("server.httpx.AsyncClient", return_value=mock):
            resp = await client.get("/tools/queue_status")

        assert resp.status_code == 500


# ── generate_image ───────────────────────────────────────────────────

class TestGenerateImage:

    MOCK_PROMPT_RESP = {"prompt_id": "abc-123", "number": 5}

    @pytest.mark.anyio
    async def test_queues_job(self, client, tmp_path):
        """POST with valid params loads workflow, injects params, queues to ComfyUI."""
        wf_path = tmp_path / "default.json"
        wf_path.write_text(json.dumps({
            "3": {"class_type": "KSampler", "inputs": {"seed": 0, "positive": ["6", 0]}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        }))

        mock = _mock_client(post_return=_mock_response(200, self.MOCK_PROMPT_RESP))

        with patch("server.WORKFLOWS_DIR", str(tmp_path)), \
             patch("server.httpx.AsyncClient", return_value=mock):
            resp = await client.post("/tools/generate_image", json={
                "prompt": "a mountain at sunset",
                "workflow_name": "default",
                "width": 1024,
                "height": 768,
                "seed": 42,
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["prompt_id"] == "abc-123"
        assert data["status"] == "queued"

        # Verify the POST payload had injected params
        call_args = mock.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        workflow = payload["prompt"]
        assert workflow["6"]["inputs"]["text"] == "a mountain at sunset"
        assert workflow["3"]["inputs"]["seed"] == 42
        assert workflow["5"]["inputs"]["width"] == 1024
        assert workflow["5"]["inputs"]["height"] == 768

    @pytest.mark.anyio
    async def test_missing_workflow(self, client, tmp_path):
        """Requesting a nonexistent workflow returns 404."""
        with patch("server.WORKFLOWS_DIR", str(tmp_path)):
            resp = await client.post("/tools/generate_image", json={
                "prompt": "test",
                "workflow_name": "nonexistent",
            })
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_comfyui_unreachable(self, client, tmp_path):
        """ComfyUI down returns 500."""
        wf_path = tmp_path / "default.json"
        wf_path.write_text(json.dumps({
            "3": {"class_type": "KSampler", "inputs": {"seed": 0, "positive": ["6", 0]}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        }))

        mock = _mock_client()
        mock.post = AsyncMock(side_effect=httpx.RequestError("refused"))

        with patch("server.WORKFLOWS_DIR", str(tmp_path)), \
             patch("server.httpx.AsyncClient", return_value=mock):
            resp = await client.post("/tools/generate_image", json={
                "prompt": "test",
                "workflow_name": "default",
            })
        assert resp.status_code == 500


# ── get_result ───────────────────────────────────────────────────────

class TestGetResult:

    @pytest.mark.anyio
    async def test_completed_job(self, client):
        """Completed job returns images with URLs."""
        history = {
            "abc-123": {
                "status": {"completed": True},
                "outputs": {
                    "9": {
                        "images": [
                            {"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}
                        ]
                    }
                },
            }
        }
        mock = _mock_client(get_return=_mock_response(200, history))

        with patch("server.httpx.AsyncClient", return_value=mock):
            resp = await client.get("/tools/get_result", params={"job_id": "abc-123"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert len(data["images"]) == 1
        assert "ComfyUI_00001_.png" in data["images"][0]["filename"]
        assert "url" in data["images"][0]

    @pytest.mark.anyio
    async def test_pending_job(self, client):
        """Job not in history yet returns pending status."""
        mock = _mock_client(get_return=_mock_response(200, {}))

        with patch("server.httpx.AsyncClient", return_value=mock):
            resp = await client.get("/tools/get_result", params={"job_id": "xyz-999"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending"

    @pytest.mark.anyio
    async def test_running_job(self, client):
        """Job in history but not completed returns running status."""
        history = {
            "abc-456": {
                "status": {"completed": False},
                "outputs": {},
            }
        }
        mock = _mock_client(get_return=_mock_response(200, history))

        with patch("server.httpx.AsyncClient", return_value=mock):
            resp = await client.get("/tools/get_result", params={"job_id": "abc-456"})

        assert resp.status_code == 200
        assert resp.json()["status"] == "running"


# ── health ───────────────────────────────────────────────────────────

class TestHealth:

    @pytest.mark.anyio
    async def test_healthy(self, client):
        mock = _mock_client(get_return=_mock_response(200, text="ok"))

        with patch("server.httpx.AsyncClient", return_value=mock):
            resp = await client.get("/health")

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.anyio
    async def test_unhealthy(self, client):
        mock = _mock_client(get_side_effect=httpx.RequestError("refused"))

        with patch("server.httpx.AsyncClient", return_value=mock):
            resp = await client.get("/health")

        assert resp.status_code == 503
