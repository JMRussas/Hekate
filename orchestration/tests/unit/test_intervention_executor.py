#  Intervention Executor — Unit Tests
#
#  Tests all four intervention actions (retry_task, release_claim,
#  skip_task, reorder_wave), HTTP error handling, timeout/connection
#  failures, missing task_id fallback, and bus publishing.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

pytestmark = pytest.mark.anyio

from backend.services.sentinel.bus import SentinelBus
from backend.services.sentinel.intervention_executor import (
    InterventionExecutor,
    InterventionResult,
    _extract_task_id,
    _resp_detail,
    _safe_json,
)
from backend.services.sentinel.models import SentinelObservation, Severity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_obs(**overrides) -> SentinelObservation:
    defaults = dict(
        observation_id="obs-exec-001",
        category="task_stuck",
        message="Task stuck for 400s",
        severity=Severity.WARNING,
        project_id="proj-001",
        task_id="task-001",
        details={"rule": "task_stuck"},
    )
    defaults.update(overrides)
    return SentinelObservation(**defaults)


def _mock_response(status_code: int = 200, json_data=None, text: str = "") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text or ""
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_bus():
    bus = AsyncMock(spec=SentinelBus)
    bus.publish = AsyncMock(return_value=1)
    return bus


@pytest.fixture
def executor(mock_bus):
    """Executor with a mocked HTTP client."""
    ex = InterventionExecutor(
        base_url="http://localhost:5200",
        bus=mock_bus,
        auth_token="test-token",
    )
    # Inject a mocked httpx client
    mock_client = AsyncMock()
    mock_client.is_closed = False
    ex._http_client = mock_client
    return ex


# ---------------------------------------------------------------------------
# _extract_task_id
# ---------------------------------------------------------------------------

class TestExtractTaskId:

    def test_from_task_id_field(self):
        obs = _make_obs(task_id="task-direct")
        assert _extract_task_id(obs) == "task-direct"

    def test_from_details_failed_ids(self):
        obs = _make_obs(
            task_id=None,
            details={"failed_task_ids": ["t1", "t2", "t3"]},
        )
        assert _extract_task_id(obs) == "t3"  # last one

    def test_returns_none_when_missing(self):
        obs = _make_obs(task_id=None, details={})
        assert _extract_task_id(obs) is None


# ---------------------------------------------------------------------------
# _resp_detail / _safe_json
# ---------------------------------------------------------------------------

class TestResponseHelpers:

    def test_resp_detail(self):
        resp = _mock_response(status_code=500, text="Internal Server Error")
        detail = _resp_detail(resp)
        assert "500" in detail
        assert "Internal Server Error" in detail

    def test_resp_detail_empty_text(self):
        resp = _mock_response(status_code=404, text="")
        detail = _resp_detail(resp)
        assert "404" in detail

    def test_safe_json_success(self):
        resp = _mock_response(json_data={"id": "abc"})
        assert _safe_json(resp) == {"id": "abc"}

    def test_safe_json_failure(self):
        resp = _mock_response()
        resp.json.side_effect = ValueError("bad json")
        assert _safe_json(resp) == {}


# ---------------------------------------------------------------------------
# retry_task
# ---------------------------------------------------------------------------

class TestRetryTask:

    async def test_success(self, executor):
        executor._http_client.post = AsyncMock(
            return_value=_mock_response(200, json_data={"status": "queued"})
        )
        obs = _make_obs()

        result = await executor.retry_task(obs)

        assert result.action == "retry_task"
        assert result.success is True
        assert result.task_id == "task-001"
        assert "re-queued" in result.detail

    async def test_api_error(self, executor):
        executor._http_client.post = AsyncMock(
            return_value=_mock_response(500, text="server error")
        )
        result = await executor.retry_task(_make_obs())

        assert result.success is False
        assert "500" in result.detail

    async def test_timeout(self, executor):
        executor._http_client.post = AsyncMock(
            side_effect=httpx.TimeoutException("timed out")
        )
        result = await executor.retry_task(_make_obs())

        assert result.success is False
        assert "TimeoutException" in result.detail

    async def test_connect_error(self, executor):
        executor._http_client.post = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        result = await executor.retry_task(_make_obs())

        assert result.success is False
        assert "ConnectError" in result.detail

    async def test_no_task_id(self, executor):
        obs = _make_obs(task_id=None, details={})
        result = await executor.retry_task(obs)

        assert result.success is False
        assert "no task_id" in result.detail

    async def test_uses_auth_header(self, executor):
        executor._http_client.post = AsyncMock(
            return_value=_mock_response(200, json_data={})
        )
        await executor.retry_task(_make_obs())

        call_kwargs = executor._http_client.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("Authorization") == "Bearer test-token"


# ---------------------------------------------------------------------------
# release_claim
# ---------------------------------------------------------------------------

class TestReleaseClaim:

    async def test_success(self, executor):
        executor._http_client.post = AsyncMock(
            return_value=_mock_response(200, json_data={})
        )
        result = await executor.release_claim(_make_obs())

        assert result.success is True
        assert result.action == "release_claim"
        assert "claim released" in result.detail

    async def test_api_error(self, executor):
        executor._http_client.post = AsyncMock(
            return_value=_mock_response(409, text="conflict")
        )
        result = await executor.release_claim(_make_obs())

        assert result.success is False
        assert "409" in result.detail

    async def test_timeout(self, executor):
        executor._http_client.post = AsyncMock(
            side_effect=httpx.TimeoutException("timeout")
        )
        result = await executor.release_claim(_make_obs())

        assert result.success is False

    async def test_no_task_id(self, executor):
        obs = _make_obs(task_id=None, details={})
        result = await executor.release_claim(obs)

        assert result.success is False
        assert "no task_id" in result.detail


# ---------------------------------------------------------------------------
# skip_task
# ---------------------------------------------------------------------------

class TestSkipTask:

    async def test_success_publishes_bus_event(self, executor, mock_bus):
        executor._http_client.patch = AsyncMock(
            return_value=_mock_response(200, json_data={})
        )
        obs = _make_obs()

        result = await executor.skip_task(obs)

        assert result.success is True
        assert "cancelled" in result.detail
        # Verify bus publish was called for dependent unblocking
        mock_bus.publish.assert_awaited_once()
        published_msg = mock_bus.publish.call_args[0][0]
        assert published_msg.topic == "stall_notification"
        assert published_msg.payload["type"] == "task_skipped"
        assert published_msg.payload["task_id"] == "task-001"

    async def test_api_error(self, executor):
        executor._http_client.patch = AsyncMock(
            return_value=_mock_response(422, text="unprocessable")
        )
        result = await executor.skip_task(_make_obs())

        assert result.success is False

    async def test_timeout(self, executor):
        executor._http_client.patch = AsyncMock(
            side_effect=httpx.ConnectError("refused")
        )
        result = await executor.skip_task(_make_obs())

        assert result.success is False
        assert "ConnectError" in result.detail

    async def test_no_task_id(self, executor):
        obs = _make_obs(task_id=None, details={})
        result = await executor.skip_task(obs)

        assert result.success is False

    async def test_sends_cancel_payload(self, executor):
        executor._http_client.patch = AsyncMock(
            return_value=_mock_response(200, json_data={})
        )
        await executor.skip_task(_make_obs())

        call_kwargs = executor._http_client.patch.call_args
        assert call_kwargs.kwargs.get("json") == {"status": "cancelled"}


# ---------------------------------------------------------------------------
# reorder_wave
# ---------------------------------------------------------------------------

class TestReorderWave:

    async def test_success_publishes_proposal(self, executor, mock_bus):
        obs = _make_obs(
            category="wave_stalled",
            details={"wave": 2, "stalled_task_ids": ["t1", "t2"]},
        )

        result = await executor.reorder_wave(obs)

        assert result.success is True
        assert result.action == "reorder_wave"
        assert "wave 2" in result.detail
        assert result.metadata["wave"] == 2
        assert result.metadata["stalled_task_ids"] == ["t1", "t2"]

        mock_bus.publish.assert_awaited_once()
        msg = mock_bus.publish.call_args[0][0]
        assert msg.topic == "intervention_proposal"
        assert msg.payload["type"] == "reorder_wave_proposal"
        assert msg.payload["wave"] == 2

    async def test_no_wave_in_details(self, executor):
        obs = _make_obs(details={})
        result = await executor.reorder_wave(obs)

        assert result.success is False
        assert "no wave" in result.detail

    async def test_empty_stalled_ids(self, executor, mock_bus):
        obs = _make_obs(details={"wave": 1, "stalled_task_ids": []})
        result = await executor.reorder_wave(obs)

        assert result.success is True
        msg = mock_bus.publish.call_args[0][0]
        assert msg.payload["stalled_task_ids"] == []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:

    async def test_close_closes_client(self, executor):
        mock_client = executor._http_client
        mock_client.is_closed = False
        mock_client.aclose = AsyncMock()

        await executor.close()

        mock_client.aclose.assert_awaited_once()
        assert executor._http_client is None

    async def test_close_idempotent_when_already_closed(self, executor):
        executor._http_client.is_closed = True
        await executor.close()
        # Should not call aclose on already-closed client

    async def test_close_when_no_client(self, mock_bus):
        ex = InterventionExecutor(base_url="http://localhost:5200", bus=mock_bus)
        await ex.close()  # Should not raise

    def test_headers_with_token(self, executor):
        assert executor._headers() == {"Authorization": "Bearer test-token"}

    def test_headers_without_token(self, mock_bus):
        ex = InterventionExecutor(base_url="http://localhost:5200", bus=mock_bus)
        assert ex._headers() == {}

    def test_get_client_creates_new(self, mock_bus):
        ex = InterventionExecutor(base_url="http://localhost:5200", bus=mock_bus)
        client = ex._get_client()
        assert client is not None
        assert isinstance(client, httpx.AsyncClient)

    def test_base_url_strips_trailing_slash(self, mock_bus):
        ex = InterventionExecutor(base_url="http://localhost:5200/", bus=mock_bus)
        assert ex._base_url == "http://localhost:5200"


# ---------------------------------------------------------------------------
# Fallback: task_id from details.failed_task_ids
# ---------------------------------------------------------------------------

class TestTaskIdFallback:

    async def test_retry_uses_failed_task_ids_fallback(self, executor):
        executor._http_client.post = AsyncMock(
            return_value=_mock_response(200, json_data={})
        )
        obs = _make_obs(
            task_id=None,
            details={"failed_task_ids": ["ft-1", "ft-2", "ft-3"]},
        )

        result = await executor.retry_task(obs)

        assert result.success is True
        assert result.task_id == "ft-3"
        # Verify the URL used the correct task ID
        url_arg = executor._http_client.post.call_args[0][0]
        assert "ft-3" in url_arg

    async def test_release_uses_failed_task_ids_fallback(self, executor):
        executor._http_client.post = AsyncMock(
            return_value=_mock_response(200, json_data={})
        )
        obs = _make_obs(
            task_id=None,
            details={"failed_task_ids": ["ft-1"]},
        )
        result = await executor.release_claim(obs)

        assert result.success is True
        assert result.task_id == "ft-1"
