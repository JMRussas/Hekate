"""TDD tests for planning sub-agent (L2+ deepening).

Tests drive the implementation of:
  1. submit_plan_children — new Prometheus MCP tool, writes child nodes to DB
  2. _peer_plan_node — WebSocket conversation to gateway for peer planning
  3. Fallback to gateway HTTP if WebSocket fails
  4. Integration: athena_deepen uses _peer_plan_node for level >= 2

All tests written BEFORE implementation. Run with:
  cd Odin && pytest tests/test_planning_agent.py -v
"""

from __future__ import annotations

import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_node(
    node_id="node1",
    index_path="1.1",
    level=2,
    status="stub",
    parent_index="1",
    project_id="proj1",
    plan_id="plan1",
):
    return {
        "id": node_id,
        "plan_id": plan_id,
        "project_id": project_id,
        "index_path": index_path,
        "level": level,
        "status": status,
        "title": "Structured Logging",
        "content_json": json.dumps({
            "title": "Structured Logging",
            "description": "Add structlog to all services",
            "affected_files": ["backend/app.py"],
            "complexity": "medium",
        }),
        "project_context": "Project: Hekate\n\nRequirements: make it prod quality",
        "parent_index": parent_index,
    }


def _sql_key(sql: str) -> str:
    """Normalize SQL for FakeDB key matching."""
    return " ".join(sql.split())


class _NormDict(dict):
    def __setitem__(self, key, value):
        sql, params = key
        super().__setitem__((_sql_key(sql), params), value)

    def get(self, key, default=None):
        sql, params = key
        return super().get((_sql_key(sql), params), default)


class FakeDB:
    def __init__(self, rows=None):
        self._rows = _NormDict()
        self.writes = []
        for k, v in (rows or {}).items():
            self._rows[k] = v

    async def fetchone(self, sql, params=()):
        return self._rows.get((sql, params))

    async def fetchall(self, sql, params=()):
        return self._rows.get((sql, params)) or []

    async def execute_write(self, sql, params=()):
        self.writes.append((sql.strip()[:80], params))


# ===========================================================================
# 1. submit_plan_children — Prometheus API endpoint
# ===========================================================================

class TestSubmitPlanChildren:
    """
    submit_plan_children is called by the planning agent via MCP.
    It writes child plan_nodes to the DB via the engine API.

    Expected behavior:
    - POST /api/plan-nodes/{node_id}/children with children array
    - Returns {"saved": N, "node_ids": [...]}
    - Engine writes child rows and emits plan_node_created events
    """

    @pytest.mark.asyncio
    async def test_saves_children_returns_count(self):
        """Agent calls submit_plan_children, children are saved, count returned."""
        # This tests the ENGINE API endpoint (not the MCP tool directly)
        # We test the engine route handler directly

        from gods.handlers.athena_deepen import _save_plan_children

        db = FakeDB()
        # Simulate node exists
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = make_node()

        children = [
            {"title": "Add structlog", "description": "Install and configure structlog",
             "affected_files": ["backend/app.py"], "task_type": "code"},
            {"title": "Add correlation ID middleware", "description": "Propagate X-Request-ID",
             "affected_files": ["backend/middleware/logging.py"], "task_type": "code"},
        ]

        result = await _save_plan_children("node1", children, db)

        assert result["saved"] == 2
        assert len(result["node_ids"]) == 2
        # Children should be written to DB
        insert_writes = [w for w in db.writes if "INSERT" in w[0] and "plan_nodes" in w[0]]
        assert len(insert_writes) == 2

    @pytest.mark.asyncio
    async def test_assigns_correct_index_paths(self):
        """Children should get index paths like parent.1, parent.2, etc."""
        from gods.handlers.athena_deepen import _save_plan_children

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = \
            make_node(index_path="2.3")
        # No existing children
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "2.3"))] = {"cnt": 0}

        children = [{"title": "Child A", "task_type": "code", "description": "..."}]
        result = await _save_plan_children("node1", children, db)

        # The written row should have index_path = "2.3.1"
        insert_write = next(w for w in db.writes if "INSERT" in w[0] and "plan_nodes" in w[0])
        params = insert_write[1]
        # index_path is the 4th parameter (id, plan_id, project_id, index_path, ...)
        assert "2.3.1" in params

    @pytest.mark.asyncio
    async def test_returns_empty_on_missing_node(self):
        """If node not found, return error dict."""
        from gods.handlers.athena_deepen import _save_plan_children

        db = FakeDB()  # no rows
        result = await _save_plan_children("missing_node", [], db)
        assert result.get("error") is not None

    @pytest.mark.asyncio
    async def test_increments_from_existing_children(self):
        """New children should continue numbering after existing children."""
        from gods.handlers.athena_deepen import _save_plan_children

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = \
            make_node(index_path="1.1")
        # 2 children already exist
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1.1"))] = {"cnt": 2}

        children = [{"title": "Third child", "task_type": "code", "description": "..."}]
        result = await _save_plan_children("node1", children, db)

        insert_write = next(w for w in db.writes if "INSERT" in w[0] and "plan_nodes" in w[0])
        params = insert_write[1]
        assert "1.1.3" in params  # starts at 3, not 1


# ===========================================================================
# 2. _peer_plan_node — WebSocket conversation to gateway
# ===========================================================================

class TestPeerPlanNode:
    """
    _peer_plan_node connects to the gateway WebSocket and does a two-turn
    conversation: turn 1 = planning, turn 2 = gap check.
    Returns DB rows saved by the agent via submit_plan_children.
    """

    @pytest.mark.asyncio
    async def test_returns_children_saved_by_agent(self):
        """After peer planning, children in DB are returned."""
        from gods.handlers.athena_deepen import _peer_plan_node

        node = make_node(level=2)
        db = FakeDB()
        saved_children = [
            {**make_node(node_id="c1", index_path="1.1.1", level=3, parent_index="1.1"), "title": "C1"},
            {**make_node(node_id="c2", index_path="1.1.2", level=3, parent_index="1.1"), "title": "C2"},
        ]
        db._rows[("SELECT * FROM plan_nodes WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1.1"))] = saved_children

        raw_events = [
            json.dumps({"type": "conversation_id", "conversation_id": "abc"}),
            json.dumps({"type": "token", "text": "Planning..."}),
            json.dumps({"type": "tool_call", "name": "mcp__prometheus__submit_plan_children", "input": {}, "id": "1"}),
            json.dumps({"type": "done"}),
            json.dumps({"type": "token", "text": "No gaps."}),
            json.dumps({"type": "done"}),
        ]

        class FakeWS:
            def __init__(self):
                self._events = iter(raw_events)
            async def send(self, _): pass
            def __aiter__(self): return self
            async def __anext__(self):
                try:
                    return next(self._events)
                except StopIteration:
                    raise StopAsyncIteration
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with patch("websockets.connect", return_value=FakeWS()):
            result = await _peer_plan_node(node, db)

        assert len(result) == 2
        assert result[0]["title"] == "C1"

    @pytest.mark.asyncio
    async def test_falls_back_to_http_on_ws_error(self):
        """When WebSocket raises, falls back to HTTP _call_gateway."""
        from gods.handlers.athena_deepen import _peer_plan_node

        node = make_node(level=2)
        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1.1"))] = []

        gateway_children = [{"title": "Fallback child", "task_type": "code", "description": "..."}]

        with patch("websockets.connect", side_effect=OSError("refused")):
            with patch("gods.handlers.athena_deepen._call_gateway",
                       new=AsyncMock(return_value=json.dumps(gateway_children))) as mock_gw:
                with patch("gods.handlers.athena_deepen.extract_json", return_value=gateway_children):
                    result = await _peer_plan_node(node, db)

        assert mock_gw.called
        assert len(result) == 1
        assert result[0]["title"] == "Fallback child"


# ===========================================================================
# 3. Fallback behavior
# ===========================================================================

class TestPlanningAgentFallback:
    """
    When WebSocket fails, _peer_plan_node falls back to HTTP gateway.
    Never leave a node without an attempt to generate children.
    """

    @pytest.mark.asyncio
    async def test_fallback_returns_http_children(self):
        """WebSocket failure → HTTP fallback returns children."""
        from gods.handlers.athena_deepen import _peer_plan_node

        node = make_node(level=2)
        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1.1"))] = []

        gateway_children = [{"title": "HTTP fallback", "task_type": "code", "description": "..."}]

        with patch("websockets.connect", side_effect=OSError("refused")):
            with patch("gods.handlers.athena_deepen._call_gateway",
                       new=AsyncMock(return_value=json.dumps(gateway_children))):
                with patch("gods.handlers.athena_deepen.extract_json", return_value=gateway_children):
                    result = await _peer_plan_node(node, db)

        assert any(c["title"] == "HTTP fallback" for c in result)

    @pytest.mark.asyncio
    async def test_no_fallback_when_ws_saves_children(self):
        """If agent saved children via WS, HTTP gateway should NOT be called."""
        from gods.handlers.athena_deepen import _peer_plan_node

        node = make_node(level=2)
        db = FakeDB()
        saved = [{**make_node(node_id="c1", index_path="1.1.1", level=3, parent_index="1.1"), "title": "Agent child"}]
        db._rows[("SELECT * FROM plan_nodes WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1.1"))] = saved

        raw_events = [
            json.dumps({"type": "conversation_id", "conversation_id": "abc"}),
            json.dumps({"type": "done"}),
            json.dumps({"type": "done"}),
        ]

        class FakeWS:
            def __init__(self):
                self._events = iter(raw_events)
            async def send(self, _): pass
            def __aiter__(self): return self
            async def __anext__(self):
                try:
                    return next(self._events)
                except StopIteration:
                    raise StopAsyncIteration
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        with patch("websockets.connect", return_value=FakeWS()):
            with patch("gods.handlers.athena_deepen._call_gateway", new=AsyncMock()) as mock_gw:
                result = await _peer_plan_node(node, db)

        mock_gw.assert_not_called()
        assert result[0]["title"] == "Agent child"


# ===========================================================================
# 4. Integration: athena_deepen uses agent for level >= 2
# ===========================================================================

class TestAthenaDeepenUsesAgent:
    """
    athena_deepen should use _peer_plan_node for L2+ nodes
    and direct gateway calls for L0-L1 nodes.
    """

    def _stub_node(self, level, node_id="node1", index_path=None):
        ip = index_path or ".".join(["1"] * (level + 1))
        return {
            "id": node_id,
            "plan_id": "plan1",
            "project_id": "proj1",
            "index_path": ip,
            "level": level,
            "status": "stub",
            "title": f"L{level} node",
            "content_json": "{}",
            "project_context": "context",
            "parent_index": ".".join(ip.split(".")[:-1]) or None,
        }

    @pytest.mark.asyncio
    async def test_l0_uses_gateway_not_agent(self):
        """L0 nodes should use gateway, not spawn an agent."""
        import gods.handlers.athena_deepen as mod
        mod._in_flight_nodes.clear()

        from gods.pipeline import Event

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = self._stub_node(0)
        db._rows[("SELECT index_path, level, title, content_json, parent_index\n            "
                  "FROM plan_nodes WHERE project_id = $1 AND index_path = $2", ("proj1", None))] = None
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1"))] = {"cnt": 0}

        children = [{"title": "Child", "task_type": "code", "description": "..."}]

        with patch("gods.handlers.athena_deepen._peer_plan_node", new=AsyncMock()) as mock_agent:
            with patch("gods.handlers.athena_deepen._call_gateway", new=AsyncMock(return_value=json.dumps(children))):
                with patch("gods.handlers.athena_deepen.extract_json", side_effect=[children, []]):
                    event = Event("plan_node_created", {
                        "project_id": "proj1", "node_id": "node1",
                        "index_path": "1", "level": 0, "plan_id": "plan1",
                    }, source="test")
                    await mod.athena_deepen(event, db)

        mock_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_l1_uses_gateway_not_agent(self):
        """L1 nodes should use gateway, not spawn an agent."""
        import gods.handlers.athena_deepen as mod
        mod._in_flight_nodes.clear()

        from gods.pipeline import Event

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = self._stub_node(1, index_path="1.1")
        db._rows[("SELECT index_path, level, title, content_json, parent_index\n            "
                  "FROM plan_nodes WHERE project_id = $1 AND index_path = $2", ("proj1", "1"))] = None
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1.1"))] = {"cnt": 0}

        children = [{"title": "Child", "task_type": "code", "description": "..."}]

        with patch("gods.handlers.athena_deepen._peer_plan_node", new=AsyncMock()) as mock_agent:
            with patch("gods.handlers.athena_deepen._call_gateway", new=AsyncMock(return_value=json.dumps(children))):
                with patch("gods.handlers.athena_deepen.extract_json", side_effect=[children, []]):
                    event = Event("plan_node_created", {
                        "project_id": "proj1", "node_id": "node1",
                        "index_path": "1.1", "level": 1, "plan_id": "plan1",
                    }, source="test")
                    await mod.athena_deepen(event, db)

        mock_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_l2_uses_agent(self):
        """L2 nodes should use _peer_plan_node."""
        import gods.handlers.athena_deepen as mod
        mod._in_flight_nodes.clear()

        from gods.pipeline import Event

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = self._stub_node(2, index_path="1.1")
        db._rows[("SELECT index_path, level, title, content_json, parent_index\n            "
                  "FROM plan_nodes WHERE project_id = $1 AND index_path = $2", ("proj1", "1"))] = None

        agent_children = [
            {**self._stub_node(3, node_id="c1", index_path="1.1.1"), "title": "L3 child"},
        ]

        with patch("gods.handlers.athena_deepen._peer_plan_node",
                   new=AsyncMock(return_value=agent_children)) as mock_agent:
            event = Event("plan_node_created", {
                "project_id": "proj1", "node_id": "node1",
                "index_path": "1.1", "level": 2, "plan_id": "plan1",
            }, source="test")
            result = await mod.athena_deepen(event, db)

        mock_agent.assert_called_once()
        # Should emit plan_node_created for each agent child
        child_emits = [e for e in result if e.event_type == "plan_node_created"]
        assert len(child_emits) >= 1

    @pytest.mark.asyncio
    async def test_l3_l4_l5_all_use_agent(self):
        """L3, L4 nodes should also use _peer_plan_node. L5 emits executable."""
        import gods.handlers.athena_deepen as mod

        from gods.pipeline import Event

        for level in [3, 4]:
            mod._in_flight_nodes.clear()

            db = FakeDB()
            ip = ".".join(["1"] * (level + 1))
            db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = \
                self._stub_node(level, index_path=ip)
            parent_ip = ".".join(ip.split(".")[:-1])
            db._rows[("SELECT index_path, level, title, content_json, parent_index\n            "
                      "FROM plan_nodes WHERE project_id = $1 AND index_path = $2",
                      ("proj1", parent_ip))] = None

            agent_children = [
                {**self._stub_node(level + 1, node_id="c1", index_path=f"{ip}.1"), "title": "Child"},
            ]

            with patch("gods.handlers.athena_deepen._peer_plan_node",
                       new=AsyncMock(return_value=agent_children)) as mock_agent:
                event = Event("plan_node_created", {
                    "project_id": "proj1", "node_id": "node1",
                    "index_path": ip, "level": level, "plan_id": "plan1",
                }, source="test")
                await mod.athena_deepen(event, db)

            mock_agent.assert_called_once(), f"L{level} should use agent"
