#  Orchestration Engine - Context Store Client API Interaction Tests
#
#  Tests for the HTTP API methods in ContextStoreClient: preview(),
#  create_node(), create_edge(). Verifies correct URLs, HTTP methods,
#  JSON payloads, headers, and error handling.
#
#  Depends on: backend/services/context_store_client.py
#  Used by:    pytest

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from backend.services.context_store_client import ContextStoreClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(base_url: str = "http://fake:5102") -> ContextStoreClient:
    """Create a ContextStoreClient with a known base URL."""
    return ContextStoreClient(base_url=base_url, timeout=1.0)


def _mock_http(
    client: ContextStoreClient,
    *,
    json_data: dict | list | None = None,
    status_code: int = 200,
) -> AsyncMock:
    """Inject a mock httpx.AsyncClient and return it for assertions."""
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.is_closed = False
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status = MagicMock()
    mock.post.return_value = resp
    mock.put.return_value = resp
    client._client = mock
    return mock


# ---------------------------------------------------------------------------
# Tests: preview()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preview_sends_correct_url_and_payload():
    """preview() POSTs to /api/preview with query and maxNodes."""
    client = _make_client()
    mock = _mock_http(client, json_data={"nodes": [{"id": "n1"}]})

    result = await client.preview("find auth modules", max_nodes=10)

    mock.post.assert_called_once_with(
        "http://fake:5102/api/preview",
        json={"query": "find auth modules", "maxNodes": 10},
    )
    assert result is not None
    assert result.nodes == [{"id": "n1"}]
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_preview_default_max_nodes():
    """preview() uses maxNodes=20 by default."""
    client = _make_client()
    mock = _mock_http(client, json_data={"nodes": []})

    await client.preview("test query")

    _, kwargs = mock.post.call_args
    assert kwargs["json"]["maxNodes"] == 20


@pytest.mark.asyncio
async def test_preview_extracts_nodes_from_results_key():
    """preview() handles response where nodes are under 'results' key."""
    client = _make_client()
    _mock_http(client, json_data={"results": [{"id": "r1"}, {"id": "r2"}]})

    result = await client.preview("test")

    assert result is not None
    assert len(result.nodes) == 2
    assert result.nodes[0]["id"] == "r1"


@pytest.mark.asyncio
async def test_preview_extracts_flat_list_response():
    """preview() handles response that is a flat list of nodes."""
    client = _make_client()
    _mock_http(client, json_data=[{"id": "a"}, {"id": "b"}])

    result = await client.preview("test")

    assert result is not None
    assert len(result.nodes) == 2


@pytest.mark.asyncio
async def test_preview_returns_none_on_timeout():
    """preview() returns None on TimeoutException."""
    client = _make_client()
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.is_closed = False
    mock.post.side_effect = httpx.TimeoutException("timed out")
    client._client = mock

    result = await client.preview("test")

    assert result is None
    assert client._consecutive_failures == 1


@pytest.mark.asyncio
async def test_preview_returns_none_on_http_error():
    """preview() returns None on HTTP 500."""
    client = _make_client()
    _mock_http(client, status_code=500)

    result = await client.preview("test")

    assert result is None
    assert client._consecutive_failures == 1


@pytest.mark.asyncio
async def test_preview_returns_none_on_connect_error():
    """preview() returns None when server is unreachable."""
    client = _make_client()
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.is_closed = False
    mock.post.side_effect = httpx.ConnectError("connection refused")
    client._client = mock

    result = await client.preview("test")

    assert result is None
    assert client._consecutive_failures == 1


# ---------------------------------------------------------------------------
# Tests: create_node()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_node_sends_correct_url_and_payload():
    """create_node() POSTs to /api/node/{parent_id}/children."""
    client = _make_client()
    payload = {"name": "task-result", "type": "execution_outcome"}
    mock = _mock_http(client, json_data={"id": "new-node-1"})

    result = await client.create_node("parent-123", payload)

    mock.post.assert_called_once_with(
        "http://fake:5102/api/node/parent-123/children",
        json=payload,
    )
    assert result == "new-node-1"


@pytest.mark.asyncio
async def test_create_node_falls_back_to_payload_id():
    """create_node() returns payload id when response has no 'id' field."""
    client = _make_client()
    payload = {"id": "my-id", "name": "test"}
    _mock_http(client, json_data={})

    result = await client.create_node("parent", payload)

    assert result == "my-id"


@pytest.mark.asyncio
async def test_create_node_returns_none_on_timeout():
    """create_node() returns None on timeout."""
    client = _make_client()
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.is_closed = False
    mock.post.side_effect = httpx.TimeoutException("timed out")
    client._client = mock

    result = await client.create_node("parent", {"name": "x"})

    assert result is None
    assert client._consecutive_failures == 1


@pytest.mark.asyncio
async def test_create_node_returns_none_on_http_error():
    """create_node() returns None on HTTP 422 (validation error)."""
    client = _make_client()
    _mock_http(client, status_code=422)

    result = await client.create_node("parent", {"bad": "payload"})

    assert result is None


# ---------------------------------------------------------------------------
# Tests: create_edge()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_edge_sends_correct_url_and_payload():
    """create_edge() POSTs to /api/graph/edges."""
    client = _make_client()
    edge = {"fromId": "node-a", "toId": "node-b", "label": "PRODUCED_BY"}
    mock = _mock_http(client, json_data={})

    result = await client.create_edge(edge)

    mock.post.assert_called_once_with(
        "http://fake:5102/api/graph/edges",
        json=edge,
    )
    assert result is True


@pytest.mark.asyncio
async def test_create_edge_returns_false_on_error():
    """create_edge() returns False on any exception."""
    client = _make_client()
    mock = AsyncMock(spec=httpx.AsyncClient)
    mock.is_closed = False
    mock.post.side_effect = httpx.ConnectError("connection refused")
    client._client = mock

    result = await client.create_edge({"from": "a", "to": "b"})

    assert result is False
    assert client._consecutive_failures == 1


@pytest.mark.asyncio
async def test_create_edge_returns_false_on_http_error():
    """create_edge() returns False on HTTP 500."""
    client = _make_client()
    _mock_http(client, status_code=500)

    result = await client.create_edge({"from": "a", "to": "b"})

    assert result is False


# ---------------------------------------------------------------------------
# Tests: base URL handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_base_url_trailing_slash_stripped():
    """Trailing slash on base URL is stripped to avoid double slashes."""
    client = _make_client(base_url="http://fake:5102/")
    mock = _mock_http(client, json_data={"nodes": []})

    await client.preview("test")

    url = mock.post.call_args[0][0]
    assert url == "http://fake:5102/api/preview"
    assert "//" not in url.split("://")[1]


# ---------------------------------------------------------------------------
# Tests: success resets circuit breaker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_successful_preview_resets_failure_count():
    """A successful preview() call resets the failure counter."""
    client = _make_client()

    # Accumulate failures first
    fail_mock = AsyncMock(spec=httpx.AsyncClient)
    fail_mock.is_closed = False
    fail_mock.post.side_effect = httpx.ConnectError("refused")
    client._client = fail_mock

    for _ in range(3):
        await client.preview("test")
    assert client._consecutive_failures == 3

    # Now succeed
    _mock_http(client, json_data={"nodes": []})
    await client.preview("test")
    assert client._consecutive_failures == 0


@pytest.mark.asyncio
async def test_successful_create_node_resets_failure_count():
    """A successful create_node() call resets the failure counter."""
    client = _make_client()
    client._consecutive_failures = 3

    _mock_http(client, json_data={"id": "new"})
    await client.create_node("parent", {"name": "test"})

    assert client._consecutive_failures == 0


@pytest.mark.asyncio
async def test_successful_create_edge_resets_failure_count():
    """A successful create_edge() call resets the failure counter."""
    client = _make_client()
    client._consecutive_failures = 3

    _mock_http(client, json_data={})
    await client.create_edge({"from": "a", "to": "b"})

    assert client._consecutive_failures == 0
