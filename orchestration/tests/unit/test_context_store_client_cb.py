#  Orchestration Engine - Context Store Client Circuit Breaker Tests
#
#  Tests for the circuit breaker logic in ContextStoreClient: state transitions
#  (Closed -> Open -> Half-Open -> Closed), failure counting, and recovery.
#
#  Depends on: backend/services/context_store_client.py
#  Used by:    pytest

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.services.context_store_client import (
    FAILURE_THRESHOLD,
    RECOVERY_WINDOW,
    ContextStoreClient,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client() -> ContextStoreClient:
    """Create a ContextStoreClient with a known base URL."""
    return ContextStoreClient(base_url="http://fake:5102", timeout=1.0)


def _mock_post_failure(client: ContextStoreClient) -> None:
    """Inject a mock httpx client that always raises ConnectError."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.is_closed = False
    mock_http.post.side_effect = httpx.ConnectError("connection refused")
    client._client = mock_http


def _mock_post_success(client: ContextStoreClient, json_data: dict | None = None) -> None:
    """Inject a mock httpx client that returns a successful response."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.is_closed = False
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = json_data or {"nodes": []}
    resp.raise_for_status = MagicMock()
    mock_http.post.return_value = resp
    client._client = mock_http


# ---------------------------------------------------------------------------
# Tests: Closed state (normal operation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_starts_in_closed_state():
    """New client starts with circuit closed (requests allowed)."""
    client = _make_client()
    assert client._consecutive_failures == 0
    assert client._circuit_open_until == 0.0
    assert not client._is_circuit_open()


@pytest.mark.asyncio
async def test_failures_below_threshold_keep_circuit_closed():
    """Fewer than FAILURE_THRESHOLD failures don't open the circuit."""
    client = _make_client()
    _mock_post_failure(client)

    for _ in range(FAILURE_THRESHOLD - 1):
        result = await client.preview("test query")
        assert result is None

    assert client._consecutive_failures == FAILURE_THRESHOLD - 1
    assert client._circuit_open_until == 0.0
    assert not client._is_circuit_open()


@pytest.mark.asyncio
async def test_success_resets_failure_count():
    """A successful call resets the consecutive failure counter."""
    client = _make_client()
    _mock_post_failure(client)

    # Accumulate some failures
    for _ in range(3):
        await client.preview("test")

    assert client._consecutive_failures == 3

    # Now succeed
    _mock_post_success(client, {"nodes": []})
    result = await client.preview("test")
    assert result is not None
    assert client._consecutive_failures == 0


# ---------------------------------------------------------------------------
# Tests: Closed -> Open transition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_opens_after_threshold_failures():
    """Circuit opens after exactly FAILURE_THRESHOLD consecutive failures."""
    client = _make_client()
    _mock_post_failure(client)

    for _ in range(FAILURE_THRESHOLD):
        await client.preview("test")

    assert client._consecutive_failures == FAILURE_THRESHOLD
    assert client._circuit_open_until > 0
    assert client._is_circuit_open()


@pytest.mark.asyncio
async def test_open_circuit_short_circuits_requests():
    """While circuit is open, requests return None without hitting HTTP."""
    client = _make_client()
    _mock_post_failure(client)

    # Trip the breaker
    for _ in range(FAILURE_THRESHOLD):
        await client.preview("test")

    # Replace with a success mock — should NOT be called
    _mock_post_success(client)
    mock_http = client._client

    result = await client.preview("test")
    assert result is None
    mock_http.post.assert_not_called()


@pytest.mark.asyncio
async def test_open_circuit_affects_all_methods():
    """All API methods (create_node, create_edge, update_attributes) are
    short-circuited when the breaker is open."""
    client = _make_client()
    _mock_post_failure(client)

    for _ in range(FAILURE_THRESHOLD):
        await client.preview("test")

    assert client._is_circuit_open()

    _mock_post_success(client)
    mock_http = client._client

    assert await client.create_node("parent", {"name": "x"}) is None
    assert await client.create_edge({"from": "a", "to": "b"}) is False
    assert await client.update_attributes("node1", {"k": "v"}) is False
    mock_http.post.assert_not_called()
    mock_http.put.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Open -> Half-Open -> Closed transition
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_half_open_after_recovery_window():
    """After RECOVERY_WINDOW elapses, the circuit enters half-open state
    and allows a probe request."""
    client = _make_client()
    _mock_post_failure(client)

    for _ in range(FAILURE_THRESHOLD):
        await client.preview("test")

    assert client._is_circuit_open()

    # Fast-forward past the recovery window
    with patch("backend.services.context_store_client.time") as mock_time:
        mock_time.monotonic.return_value = client._circuit_open_until + 1.0
        assert not client._is_circuit_open()
        # _circuit_open_until should be reset to 0 (half-open state)
        assert client._circuit_open_until == 0.0


@pytest.mark.asyncio
async def test_half_open_success_closes_circuit():
    """A successful probe in half-open state fully closes the circuit."""
    client = _make_client()
    _mock_post_failure(client)

    # Trip the breaker
    for _ in range(FAILURE_THRESHOLD):
        await client.preview("test")

    open_until = client._circuit_open_until

    # Simulate time past recovery window, then succeed
    _mock_post_success(client, {"nodes": [{"id": "1"}]})
    with patch("backend.services.context_store_client.time") as mock_time:
        mock_time.monotonic.return_value = open_until + 1.0
        result = await client.preview("test")

    assert result is not None
    assert len(result.nodes) == 1
    assert client._consecutive_failures == 0
    assert client._circuit_open_until == 0.0


@pytest.mark.asyncio
async def test_half_open_failure_reopens_circuit():
    """A failed probe in half-open state re-opens the circuit."""
    client = _make_client()
    _mock_post_failure(client)

    # Trip the breaker
    for _ in range(FAILURE_THRESHOLD):
        await client.preview("test")

    open_until = client._circuit_open_until

    # Simulate time past recovery, but request still fails
    with patch("backend.services.context_store_client.time") as mock_time:
        # First call to monotonic: _is_circuit_open check (past window)
        # Subsequent calls: inside preview (timing + _record_failure)
        future_time = open_until + 1.0
        mock_time.monotonic.return_value = future_time
        result = await client.preview("test")

    assert result is None
    # Failure count incremented beyond threshold, circuit re-opened
    assert client._consecutive_failures == FAILURE_THRESHOLD + 1
    assert client._circuit_open_until > 0


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_interleaved_success_prevents_opening():
    """A success in the middle of failures resets the counter,
    preventing the circuit from opening."""
    client = _make_client()
    _mock_post_failure(client)

    # 4 failures (just under threshold of 5)
    for _ in range(FAILURE_THRESHOLD - 1):
        await client.preview("test")

    # One success resets
    _mock_post_success(client)
    await client.preview("test")
    assert client._consecutive_failures == 0

    # 4 more failures — still shouldn't open
    _mock_post_failure(client)
    for _ in range(FAILURE_THRESHOLD - 1):
        await client.preview("test")

    assert not client._is_circuit_open()
