"""TDD tests for the conversation runtime.

Tests are written BEFORE implementation. They define the expected behavior
of ClaudeConversation, the session store, SSE endpoint, WebSocket endpoint,
HTTP backward compatibility, and the peer planning flow.

Run with:
  cd llm-gateway && pytest tests/test_conversation.py -v
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_stream_json_lines(*events: dict) -> list[bytes]:
    """Build a list of stream-json output lines as the CLI would emit them."""
    return [json.dumps(e).encode() + b"\n" for e in events]


def assistant_event(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def tool_use_event(tool: str, input_: dict, id_: str = "tu1") -> dict:
    return {"type": "tool_use", "id": id_, "name": tool, "input": input_}


def tool_result_event(id_: str = "tu1", output: str = "ok") -> dict:
    return {"type": "tool_result", "tool_use_id": id_, "content": output}


def result_event(exit_code: int = 0, cost: float = 0.001) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "exit_code": exit_code,
        "total_cost_usd": cost,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


class FakeProcess:
    """Minimal fake asyncio subprocess for testing ClaudeConversation."""

    def __init__(self, lines: list[bytes]):
        self.pid = 12345
        self.returncode = None
        self._lines = iter(lines)
        self.stdin = FakeStdin()
        self.stdout = FakeStdout(lines)
        self.stderr = FakeStderr()
        self._killed = False

    def kill(self):
        self._killed = True
        self.returncode = -9

    async def wait(self):
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


class FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, data: bytes):
        self.written.append(data)

    async def drain(self):
        pass


class FakeStdout:
    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)
        self._pos = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._pos >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._pos]
        self._pos += 1
        return line


class FakeStderr:
    async def read(self):
        return b""


# ===========================================================================
# 1. ClaudeConversation
# ===========================================================================

class TestClaudeConversation:
    """Tests for the core ClaudeConversation class in conversation.py."""

    @pytest.mark.asyncio
    async def test_send_single_message_returns_text(self):
        """send() yields text events from the assistant response."""
        from conversation import ClaudeConversation

        lines = make_stream_json_lines(
            assistant_event("Hello, I am Claude."),
            result_event(),
        )
        fake_proc = FakeProcess(lines)

        conv = ClaudeConversation.__new__(ClaudeConversation)
        conv.conversation_id = "test-conv-1"
        conv._process = fake_proc
        conv._last_activity = time.time()
        conv._slow_threshold = 120
        conv._start_time = time.time()

        events = []
        async for event in conv.send("Hello"):
            events.append(event)

        text_events = [e for e in events if e["type"] == "token"]
        assert len(text_events) > 0
        assert any("Hello" in e["text"] for e in text_events)

        done_events = [e for e in events if e["type"] == "done"]
        assert len(done_events) == 1

    @pytest.mark.asyncio
    async def test_send_writes_to_stdin(self):
        """send() writes the user message to the process stdin as stream-json."""
        from conversation import ClaudeConversation

        lines = make_stream_json_lines(
            assistant_event("ok"),
            result_event(),
        )
        fake_proc = FakeProcess(lines)

        conv = ClaudeConversation.__new__(ClaudeConversation)
        conv.conversation_id = "test-conv-2"
        conv._process = fake_proc
        conv._last_activity = time.time()
        conv._slow_threshold = 120
        conv._start_time = time.time()

        async for _ in conv.send("Do the thing"):
            pass

        assert len(fake_proc.stdin.written) > 0
        written = b"".join(fake_proc.stdin.written)
        payload = json.loads(written.decode())
        assert payload.get("type") == "user"
        assert "Do the thing" in payload.get("message", "")

    @pytest.mark.asyncio
    async def test_tool_use_events_emitted(self):
        """Tool use from Claude is surfaced as tool_call events."""
        from conversation import ClaudeConversation

        lines = make_stream_json_lines(
            tool_use_event("submit_plan_children", {"node_id": "n1", "children": []}),
            tool_result_event(),
            assistant_event("Done."),
            result_event(),
        )
        fake_proc = FakeProcess(lines)

        conv = ClaudeConversation.__new__(ClaudeConversation)
        conv.conversation_id = "test-conv-3"
        conv._process = fake_proc
        conv._last_activity = time.time()
        conv._slow_threshold = 120
        conv._start_time = time.time()

        events = []
        async for event in conv.send("Plan this node"):
            events.append(event)

        tool_events = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_events) == 1
        assert tool_events[0]["name"] == "submit_plan_children"

    @pytest.mark.asyncio
    async def test_result_event_includes_cost(self):
        """result event surfaces cost_usd."""
        from conversation import ClaudeConversation

        lines = make_stream_json_lines(
            assistant_event("Done."),
            result_event(cost=0.0042),
        )
        fake_proc = FakeProcess(lines)

        conv = ClaudeConversation.__new__(ClaudeConversation)
        conv.conversation_id = "test-conv-4"
        conv._process = fake_proc
        conv._last_activity = time.time()
        conv._slow_threshold = 120
        conv._start_time = time.time()

        events = []
        async for event in conv.send("Hello"):
            events.append(event)

        result_events = [e for e in events if e["type"] == "result"]
        assert len(result_events) == 1
        assert result_events[0]["cost_usd"] == pytest.approx(0.0042)

    @pytest.mark.asyncio
    async def test_gap_monitoring_fires_slow_event(self):
        """When gap between lines exceeds threshold, a slow event is emitted."""
        from conversation import ClaudeConversation

        # We'll patch time.time to simulate a gap
        base_time = 1000.0
        times = iter([
            base_time,        # _last_activity init
            base_time,        # start_time
            base_time + 130,  # first line arrives 130s later → gap > 120s threshold
            base_time + 130,  # result event
        ])

        lines = make_stream_json_lines(
            assistant_event("Eventually..."),
            result_event(),
        )
        fake_proc = FakeProcess(lines)

        conv = ClaudeConversation.__new__(ClaudeConversation)
        conv.conversation_id = "test-conv-5"
        conv._process = fake_proc
        conv._last_activity = base_time
        conv._slow_threshold = 120
        conv._start_time = base_time

        events = []
        with patch("conversation.time") as mock_time:
            mock_time.time.side_effect = list(times) + [base_time + 130] * 10
            async for event in conv.send("Go"):
                events.append(event)

        slow_events = [e for e in events if e["type"] == "slow"]
        assert len(slow_events) >= 1
        assert slow_events[0]["gap_s"] >= 120

    @pytest.mark.asyncio
    async def test_gap_monitoring_does_not_kill_process(self):
        """A slow gap is flagged but the process is NOT killed."""
        from conversation import ClaudeConversation

        lines = make_stream_json_lines(
            assistant_event("Eventually..."),
            result_event(),
        )
        fake_proc = FakeProcess(lines)

        conv = ClaudeConversation.__new__(ClaudeConversation)
        conv.conversation_id = "test-conv-6"
        conv._process = fake_proc
        conv._last_activity = time.time() - 200  # simulate 200s since last activity
        conv._slow_threshold = 120
        conv._start_time = time.time() - 200

        async for _ in conv.send("Go"):
            pass

        assert not fake_proc._killed, "Process must NOT be killed on slow gap"

    @pytest.mark.asyncio
    async def test_close_terminates_process(self):
        """close() kills the subprocess and marks it closed."""
        from conversation import ClaudeConversation

        fake_proc = FakeProcess([])
        fake_proc.returncode = None

        conv = ClaudeConversation.__new__(ClaudeConversation)
        conv.conversation_id = "test-conv-7"
        conv._process = fake_proc
        conv._last_activity = time.time()
        conv._slow_threshold = 120
        conv._start_time = time.time()

        await conv.close()

        assert fake_proc._killed, "Process should be killed on close()"

    @pytest.mark.asyncio
    async def test_last_activity_updated_on_each_line(self):
        """_last_activity is updated each time a line is received."""
        from conversation import ClaudeConversation

        lines = make_stream_json_lines(
            assistant_event("line 1"),
            assistant_event("line 2"),
            result_event(),
        )
        fake_proc = FakeProcess(lines)

        conv = ClaudeConversation.__new__(ClaudeConversation)
        conv.conversation_id = "test-conv-8"
        conv._process = fake_proc
        initial_time = 1000.0
        conv._last_activity = initial_time
        conv._slow_threshold = 120
        conv._start_time = initial_time

        async for _ in conv.send("Go"):
            pass

        assert conv._last_activity > initial_time


# ===========================================================================
# 2. Session Store
# ===========================================================================

class TestSessionStore:
    """Tests for sessions.py — get_or_create, close_session, idle cleanup."""

    @pytest.mark.asyncio
    async def test_get_or_create_new_session(self):
        """None conversation_id → new session created, returns id."""
        from sessions import get_or_create, close_session

        with patch("sessions.ClaudeConversation") as MockConv:
            mock_conv = AsyncMock()
            mock_conv.conversation_id = "new-id-1"
            MockConv.create = AsyncMock(return_value=mock_conv)

            conv, cid = await get_or_create(None)
            assert cid == "new-id-1"
            assert conv is mock_conv

        await close_session("new-id-1")

    @pytest.mark.asyncio
    async def test_get_or_create_existing_session(self):
        """Existing conversation_id → returns same session object."""
        from sessions import get_or_create, close_session, _sessions

        mock_conv = AsyncMock()
        mock_conv.conversation_id = "existing-1"
        mock_conv._last_activity = time.time()
        _sessions["existing-1"] = mock_conv

        conv, cid = await get_or_create("existing-1")
        assert conv is mock_conv
        assert cid == "existing-1"

        await close_session("existing-1")

    @pytest.mark.asyncio
    async def test_close_session_removes_from_store(self):
        """close_session removes the session and closes the conversation."""
        from sessions import close_session, _sessions

        mock_conv = AsyncMock()
        mock_conv.conversation_id = "to-close"
        _sessions["to-close"] = mock_conv

        await close_session("to-close")

        assert "to-close" not in _sessions
        mock_conv.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_idle_removes_stale_sessions(self):
        """cleanup_idle removes sessions idle longer than max_idle_seconds."""
        from sessions import cleanup_idle, _sessions

        old_conv = AsyncMock()
        old_conv.conversation_id = "old-session"
        old_conv._last_activity = time.time() - 700  # 700s idle, > 600s threshold

        fresh_conv = AsyncMock()
        fresh_conv.conversation_id = "fresh-session"
        fresh_conv._last_activity = time.time() - 60  # 60s idle, under threshold

        _sessions["old-session"] = old_conv
        _sessions["fresh-session"] = fresh_conv

        await cleanup_idle(max_idle_seconds=600)

        assert "old-session" not in _sessions
        assert "fresh-session" in _sessions
        old_conv.close.assert_called_once()
        fresh_conv.close.assert_not_called()

        # cleanup
        _sessions.pop("fresh-session", None)

    @pytest.mark.asyncio
    async def test_unknown_conversation_id_creates_new(self):
        """Unknown conversation_id (not in store) creates a new session."""
        from sessions import get_or_create, close_session

        with patch("sessions.ClaudeConversation") as MockConv:
            mock_conv = AsyncMock()
            mock_conv.conversation_id = "new-from-unknown"
            MockConv.create = AsyncMock(return_value=mock_conv)

            conv, cid = await get_or_create("unknown-id-xyz")
            assert cid == "new-from-unknown"

        await close_session("new-from-unknown")


# ===========================================================================
# 3. SSE Endpoint
# ===========================================================================

class TestSSEEndpoint:
    """Tests for POST /v1/conversation/stream."""

    def _parse_sse(self, body: str) -> list[dict]:
        """Parse SSE body into list of {event, data} dicts."""
        events = []
        current = {}
        for line in body.splitlines():
            if line.startswith("event:"):
                current["event"] = line[len("event:"):].strip()
            elif line.startswith("data:"):
                current["data"] = json.loads(line[len("data:"):].strip())
            elif line == "" and current:
                events.append(current)
                current = {}
        if current:
            events.append(current)
        return events

    def test_new_conversation_returns_conversation_id(self):
        """POST without conversation_id returns conversation_id SSE event."""
        from fastapi.testclient import TestClient
        from server import app

        async def fake_send(message):
            yield {"type": "token", "text": "hello"}
            yield {"type": "done"}

        mock_conv = MagicMock()
        mock_conv.conversation_id = "new-sse-1"
        mock_conv.send = fake_send

        with patch("server.get_or_create", new=AsyncMock(return_value=(mock_conv, "new-sse-1"))):
            with patch("server.close_session", new=AsyncMock()):
                client = TestClient(app)
                resp = client.post(
                    "/v1/conversation/stream",
                    json={"message": "hello"},
                    headers={"Accept": "text/event-stream"},
                )

        assert resp.status_code == 200
        events = self._parse_sse(resp.text)
        cid_events = [e for e in events if e.get("event") == "conversation_id"]
        assert len(cid_events) == 1
        assert cid_events[0]["data"]["conversation_id"] == "new-sse-1"

    def test_token_events_stream_during_generation(self):
        """token events arrive in the SSE stream."""
        from fastapi.testclient import TestClient
        from server import app

        async def fake_send(message):
            yield {"type": "token", "text": "Hello "}
            yield {"type": "token", "text": "world"}
            yield {"type": "done"}

        mock_conv = MagicMock()
        mock_conv.conversation_id = "sse-tokens"
        mock_conv.send = fake_send

        with patch("server.get_or_create", new=AsyncMock(return_value=(mock_conv, "sse-tokens"))):
            with patch("server.close_session", new=AsyncMock()):
                client = TestClient(app)
                resp = client.post(
                    "/v1/conversation/stream",
                    json={"message": "hi", "conversation_id": "sse-tokens"},
                )

        events = self._parse_sse(resp.text)
        token_events = [e for e in events if e.get("event") == "token"]
        assert len(token_events) == 2

    def test_done_event_always_emitted(self):
        """done event is always the last event, even on error."""
        from fastapi.testclient import TestClient
        from server import app

        async def fake_send_error(message):
            raise RuntimeError("Claude crashed")
            yield  # make it a generator

        mock_conv = MagicMock()
        mock_conv.conversation_id = "sse-error"
        mock_conv.send = fake_send_error

        with patch("server.get_or_create", new=AsyncMock(return_value=(mock_conv, "sse-error"))):
            with patch("server.close_session", new=AsyncMock()):
                client = TestClient(app)
                resp = client.post(
                    "/v1/conversation/stream",
                    json={"message": "hi"},
                )

        events = self._parse_sse(resp.text)
        done_events = [e for e in events if e.get("event") == "done"]
        assert len(done_events) == 1, "done must always be emitted, even on error"

    def test_tool_call_events_emitted(self):
        """tool_call events from Claude are forwarded to SSE stream."""
        from fastapi.testclient import TestClient
        from server import app

        async def fake_send(message):
            yield {"type": "tool_call", "name": "submit_plan_children", "input": {}}
            yield {"type": "tool_result", "name": "submit_plan_children", "resultLength": 3}
            yield {"type": "token", "text": "Done."}
            yield {"type": "done"}

        mock_conv = MagicMock()
        mock_conv.conversation_id = "sse-tools"
        mock_conv.send = fake_send

        with patch("server.get_or_create", new=AsyncMock(return_value=(mock_conv, "sse-tools"))):
            with patch("server.close_session", new=AsyncMock()):
                client = TestClient(app)
                resp = client.post(
                    "/v1/conversation/stream",
                    json={"message": "plan this"},
                )

        events = self._parse_sse(resp.text)
        tool_events = [e for e in events if e.get("event") == "tool_call"]
        assert len(tool_events) == 1


# ===========================================================================
# 4. WebSocket Endpoint
# ===========================================================================

class TestWebSocketEndpoint:
    """Tests for WS /v1/conversation."""

    def test_connect_and_send_message_receives_tokens(self):
        """WS connect → send message → receive token + done events."""
        from fastapi.testclient import TestClient
        from server import app

        async def fake_send(message):
            yield {"type": "token", "text": "Hi from Claude"}
            yield {"type": "done"}

        mock_conv = MagicMock()
        mock_conv.conversation_id = "ws-conv-1"
        mock_conv.send = fake_send

        with patch("server.get_or_create", new=AsyncMock(return_value=(mock_conv, "ws-conv-1"))):
            with patch("server.close_session", new=AsyncMock()):
                client = TestClient(app)
                with client.websocket_connect("/v1/conversation") as ws:
                    ws.send_json({"type": "message", "content": "hello"})
                    received = []
                    while True:
                        msg = ws.receive_json()
                        received.append(msg)
                        if msg.get("type") == "done":
                            break

        token_msgs = [m for m in received if m.get("type") == "token"]
        assert len(token_msgs) == 1
        assert token_msgs[0]["text"] == "Hi from Claude"

    def test_connect_sends_conversation_id(self):
        """First WS message back includes conversation_id."""
        from fastapi.testclient import TestClient
        from server import app

        async def fake_send(message):
            yield {"type": "done"}

        mock_conv = MagicMock()
        mock_conv.conversation_id = "ws-cid-1"
        mock_conv.send = fake_send

        with patch("server.get_or_create", new=AsyncMock(return_value=(mock_conv, "ws-cid-1"))):
            with patch("server.close_session", new=AsyncMock()):
                client = TestClient(app)
                with client.websocket_connect("/v1/conversation") as ws:
                    ws.send_json({"type": "message", "content": "hi"})
                    first = ws.receive_json()

        assert first.get("type") == "conversation_id"
        assert first.get("conversation_id") == "ws-cid-1"

    def test_disconnect_triggers_close(self):
        """WebSocket disconnect triggers close_session."""
        from fastapi.testclient import TestClient
        from server import app

        async def fake_send(message):
            yield {"type": "done"}

        mock_conv = MagicMock()
        mock_conv.conversation_id = "ws-close-1"
        mock_conv.send = fake_send
        close_mock = AsyncMock()

        with patch("server.get_or_create", new=AsyncMock(return_value=(mock_conv, "ws-close-1"))):
            with patch("server.close_session", close_mock):
                client = TestClient(app)
                with client.websocket_connect("/v1/conversation") as ws:
                    ws.send_json({"type": "message", "content": "hi"})
                    # drain
                    while True:
                        msg = ws.receive_json()
                        if msg.get("type") == "done":
                            break
                # ws closes here

        close_mock.assert_called_once_with("ws-close-1")

    def test_multi_turn_in_same_connection(self):
        """Two messages in same WS connection both get responses."""
        from fastapi.testclient import TestClient
        from server import app

        call_count = [0]

        async def fake_send(message):
            call_count[0] += 1
            yield {"type": "token", "text": f"Response {call_count[0]}"}
            yield {"type": "done"}

        mock_conv = MagicMock()
        mock_conv.conversation_id = "ws-multi-1"
        mock_conv.send = fake_send

        with patch("server.get_or_create", new=AsyncMock(return_value=(mock_conv, "ws-multi-1"))):
            with patch("server.close_session", new=AsyncMock()):
                client = TestClient(app)
                with client.websocket_connect("/v1/conversation") as ws:
                    def drain():
                        msgs = []
                        while True:
                            m = ws.receive_json()
                            msgs.append(m)
                            if m.get("type") == "done":
                                return msgs

                    ws.send_json({"type": "message", "content": "first"})
                    drain()
                    ws.send_json({"type": "message", "content": "second"})
                    drain()

        assert call_count[0] == 2, "send() must be called once per message"


# ===========================================================================
# 5. HTTP Backward Compatibility
# ===========================================================================

class TestHTTPBackwardCompat:
    """POST /v1/chat must work exactly as before."""

    def test_existing_chat_endpoint_returns_text(self):
        """POST /v1/chat returns {text, provider, model} as before."""
        from fastapi.testclient import TestClient
        from server import app

        async def fake_send(message):
            yield {"type": "token", "text": "Hello from Claude"}
            yield {"type": "result", "cost_usd": 0.001}
            yield {"type": "done"}

        mock_conv = MagicMock()
        mock_conv.conversation_id = "http-compat-1"
        mock_conv.send = fake_send

        with patch("server.get_or_create", new=AsyncMock(return_value=(mock_conv, "http-compat-1"))):
            with patch("server.close_session", new=AsyncMock()):
                client = TestClient(app)
                resp = client.post("/v1/chat", json={
                    "provider": "claude",
                    "system_prompt": "You are helpful.",
                    "user_message": "Hello",
                })

        assert resp.status_code == 200
        data = resp.json()
        assert "text" in data
        assert data["text"] == "Hello from Claude"
        assert data["provider"] == "claude"

    def test_response_format_unchanged(self):
        """/v1/chat response has same keys as before the upgrade."""
        from fastapi.testclient import TestClient
        from server import app

        async def fake_send(message):
            yield {"type": "token", "text": "ok"}
            yield {"type": "done"}

        mock_conv = MagicMock()
        mock_conv.conversation_id = "http-compat-2"
        mock_conv.send = fake_send

        with patch("server.get_or_create", new=AsyncMock(return_value=(mock_conv, "http-compat-2"))):
            with patch("server.close_session", new=AsyncMock()):
                client = TestClient(app)
                resp = client.post("/v1/chat", json={
                    "provider": "claude",
                    "system_prompt": "sys",
                    "user_message": "hi",
                    "model": "claude-sonnet-4-6",
                })

        data = resp.json()
        assert set(data.keys()) >= {"text", "provider"}  # model is optional


# ===========================================================================
# 6. Peer Planning Integration
# ===========================================================================

class TestPeerPlanning:
    """
    The peer planning flow: agent plans, submits children, then is asked
    "Any gaps?" — all in one session, full context retained.
    """

    @pytest.mark.asyncio
    async def test_plan_then_gap_check_same_session(self):
        """
        Two sends on same session:
        1. Planning prompt → submit_plan_children tool call
        2. "Any gaps?" → either another submit_plan_children or text response

        Both turns use the same conversation object (same process, same context).
        """
        from sessions import get_or_create, close_session

        turn = [0]

        async def fake_send(message):
            turn[0] += 1
            if turn[0] == 1:
                # First turn: planning
                yield {"type": "tool_call", "name": "submit_plan_children",
                       "input": {"node_id": "n1", "children": [{"title": "Child 1"}]}}
                yield {"type": "tool_result", "name": "submit_plan_children", "resultLength": 1}
                yield {"type": "token", "text": "I've submitted 1 child."}
                yield {"type": "done"}
            else:
                # Second turn: gap check (no new children)
                yield {"type": "token", "text": "The plan looks complete. No gaps."}
                yield {"type": "done"}

        mock_conv = AsyncMock()
        mock_conv.conversation_id = "peer-plan-1"
        mock_conv.send = fake_send

        with patch("sessions.ClaudeConversation") as MockConv:
            MockConv.create = AsyncMock(return_value=mock_conv)

            conv1, cid1 = await get_or_create(None)
            assert cid1 == "peer-plan-1"

            # First turn: planning
            first_turn_events = []
            async for event in conv1.send("Deepen this node: auth module"):
                first_turn_events.append(event)

            tool_calls = [e for e in first_turn_events if e["type"] == "tool_call"]
            assert len(tool_calls) == 1
            assert tool_calls[0]["name"] == "submit_plan_children"

            # Resume same session
            conv2, cid2 = await get_or_create(cid1)
            assert cid2 == cid1, "Must resume same session"
            assert conv2 is conv1, "Must be same conversation object"

            # Second turn: gap check
            second_turn_events = []
            async for event in conv2.send("Did you miss anything? Any gaps?"):
                second_turn_events.append(event)

            # Either more tool calls (more children) or text (no gaps)
            assert any(e["type"] in ("tool_call", "token") for e in second_turn_events)

            # Both turns used same process — turn counter is 2
            assert turn[0] == 2, "send() must have been called twice on same object"

        await close_session("peer-plan-1")

    @pytest.mark.asyncio
    async def test_gap_check_can_submit_additional_children(self):
        """
        If the gap check reveals missing work, the agent calls submit_plan_children
        a second time in the same session.
        """
        from sessions import get_or_create, close_session

        async def fake_send(message):
            if "gaps" in message.lower():
                yield {"type": "tool_call", "name": "submit_plan_children",
                       "input": {"node_id": "n1", "children": [{"title": "Missed child"}]}}
                yield {"type": "done"}
            else:
                yield {"type": "tool_call", "name": "submit_plan_children",
                       "input": {"node_id": "n1", "children": [{"title": "Child 1"}]}}
                yield {"type": "done"}

        mock_conv = AsyncMock()
        mock_conv.conversation_id = "peer-plan-2"
        mock_conv.send = fake_send

        with patch("sessions.ClaudeConversation") as MockConv:
            MockConv.create = AsyncMock(return_value=mock_conv)

            conv, cid = await get_or_create(None)

            events1 = []
            async for e in conv.send("Plan the node"):
                events1.append(e)

            events2 = []
            async for e in conv.send("Any gaps?"):
                events2.append(e)

            gap_tool_calls = [e for e in events2 if e["type"] == "tool_call"]
            assert len(gap_tool_calls) == 1, "Gap check must be able to submit more children"

        await close_session("peer-plan-2")
