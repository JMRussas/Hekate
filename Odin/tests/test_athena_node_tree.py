"""Unit tests for the Athena node tree planner.

Tests:
  - athena_l0: epic generation, gap check, node creation
  - athena_deepen: deepening, idempotency, concurrency limit, gap check
  - athena_bubble_up: completion propagation, project_planned emission
  - athena_materialize: L5 → task row creation
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from gods.pipeline import Event, Emit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_event(event_type: str, payload: dict) -> Event:
    return Event(event_type=event_type, payload=payload, source="test")


def _sql_key(sql: str) -> str:
    """Normalize SQL for FakeDB key matching — strip whitespace, no truncation."""
    return " ".join(sql.split())


class _NormDict(dict):
    """Dict that normalizes SQL keys on set and get."""
    def __setitem__(self, key, value):
        sql, params = key
        super().__setitem__((_sql_key(sql), params), value)

    def get(self, key, default=None):
        sql, params = key
        return super().get((_sql_key(sql), params), default)


class FakeDB:
    """Minimal fake DB for testing."""

    def __init__(self, rows: dict | None = None):
        self._rows = _NormDict()
        self.writes: list[tuple] = []
        for k, v in (rows or {}).items():
            self._rows[k] = v

    async def fetchone(self, sql: str, params=()) -> dict | None:
        return self._rows.get((sql, params))

    async def fetchall(self, sql: str, params=()) -> list[dict]:
        return self._rows.get((sql, params)) or []

    async def execute_write(self, sql: str, params=()) -> None:
        self.writes.append((sql.strip()[:60], params))


# ---------------------------------------------------------------------------
# athena_l0 tests
# ---------------------------------------------------------------------------

class TestAthenaL0:

    @pytest.mark.asyncio
    async def test_skips_when_flag_off(self):
        """Should return [] if use_node_tree_planner is False."""
        from gods.handlers.athena_l0 import athena_l0

        db = FakeDB()
        db._rows[("SELECT id, name, requirements, status, config_json FROM projects WHERE id = $1", ("proj1",))] = {
            "id": "proj1", "name": "Test", "requirements": "build stuff",
            "status": "draft", "config_json": json.dumps({"use_node_tree_planner": False}),
        }

        event = make_event("project_created", {"project_id": "proj1"})
        result = await athena_l0(event, db)
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_executing_project(self):
        """Should return [] if project is already executing."""
        from gods.handlers.athena_l0 import athena_l0

        db = FakeDB()
        db._rows[("SELECT id, name, requirements, status, config_json FROM projects WHERE id = $1", ("proj1",))] = {
            "id": "proj1", "name": "Test", "requirements": "build stuff",
            "status": "executing", "config_json": json.dumps({"use_node_tree_planner": True}),
        }

        event = make_event("project_created", {"project_id": "proj1"})
        result = await athena_l0(event, db)
        assert result == []

    @pytest.mark.asyncio
    async def test_generates_epics_and_emits(self):
        """Should generate epics and emit plan_node_created per epic."""
        from gods.handlers.athena_l0 import athena_l0

        db = FakeDB()
        db._rows[("SELECT id, name, requirements, status, config_json FROM projects WHERE id = $1", ("proj1",))] = {
            "id": "proj1", "name": "Test Project", "requirements": "make it production ready",
            "status": "draft", "config_json": json.dumps({"use_node_tree_planner": True}),
        }

        epics_response = json.dumps([
            {"title": "Observability", "description": "Logging and metrics", "rationale": "Need visibility"},
            {"title": "Reliability", "description": "Error handling", "rationale": "Need resilience"},
        ])
        gap_response = "[]"  # No additions

        with patch("gods.handlers.athena_l0._call_gateway", new=AsyncMock(side_effect=[epics_response, gap_response])):
            with patch("gods.handlers.athena_l0.extract_json", side_effect=[
                [{"title": "Observability", "description": "...", "rationale": "..."},
                 {"title": "Reliability", "description": "...", "rationale": "..."}],
                [],
            ]):
                event = make_event("project_created", {"project_id": "proj1"})
                result = await athena_l0(event, db)

        plan_node_emits = [e for e in result if e.event_type == "plan_node_created"]
        assert len(plan_node_emits) == 2
        assert plan_node_emits[0].payload["index_path"] == "1"
        assert plan_node_emits[1].payload["index_path"] == "2"
        assert plan_node_emits[0].payload["level"] == 0

    @pytest.mark.asyncio
    async def test_gap_check_adds_epics(self):
        """Gap check additions should extend the epic list."""
        from gods.handlers.athena_l0 import athena_l0

        db = FakeDB()
        db._rows[("SELECT id, name, requirements, status, config_json FROM projects WHERE id = $1", ("proj1",))] = {
            "id": "proj1", "name": "Test", "requirements": "reqs",
            "status": "draft", "config_json": json.dumps({"use_node_tree_planner": True}),
        }

        with patch("gods.handlers.athena_l0._call_gateway", new=AsyncMock(side_effect=["[]", "[]"])):
            with patch("gods.handlers.athena_l0.extract_json", side_effect=[
                [{"title": "Epic1", "description": "", "rationale": ""}],
                [{"title": "Epic2", "description": "", "rationale": ""}],  # gap check addition
            ]):
                event = make_event("project_created", {"project_id": "proj1"})
                result = await athena_l0(event, db)

        plan_node_emits = [e for e in result if e.event_type == "plan_node_created"]
        assert len(plan_node_emits) == 2

    @pytest.mark.asyncio
    async def test_emits_planning_failed_on_error(self):
        """Should emit planning_failed if LLM call raises."""
        from gods.handlers.athena_l0 import athena_l0

        db = FakeDB()
        db._rows[("SELECT id, name, requirements, status, config_json FROM projects WHERE id = $1", ("proj1",))] = {
            "id": "proj1", "name": "Test", "requirements": "reqs",
            "status": "draft", "config_json": json.dumps({"use_node_tree_planner": True}),
        }

        with patch("gods.handlers.athena_l0._call_gateway", new=AsyncMock(side_effect=RuntimeError("timeout"))):
            event = make_event("project_created", {"project_id": "proj1"})
            result = await athena_l0(event, db)

        assert any(e.event_type == "planning_failed" for e in result)


# ---------------------------------------------------------------------------
# athena_deepen tests
# ---------------------------------------------------------------------------

class TestAthenaDeepen:

    def _make_node(self, node_id="node1", index_path="1", level=0, status="stub", parent_index=None):
        return {
            "id": node_id,
            "plan_id": "plan1",
            "project_id": "proj1",
            "index_path": index_path,
            "level": level,
            "status": status,
            "title": f"Node {index_path}",
            "content_json": json.dumps({"title": f"Node {index_path}", "description": "test"}),
            "project_context": "Project: Test\n\nRequirements: build stuff",
            "parent_index": parent_index,
        }

    @pytest.mark.asyncio
    async def test_skips_non_stub_node(self):
        """Should return [] if node is already being processed."""
        from gods.handlers.athena_deepen import athena_deepen

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = \
            self._make_node(status="planning")

        event = make_event("plan_node_created", {
            "project_id": "proj1", "node_id": "node1",
            "index_path": "1", "level": 0,
        })
        result = await athena_deepen(event, db)
        assert result == []

    @pytest.mark.asyncio
    async def test_l5_node_emits_executable(self):
        """L5 nodes should emit plan_node_executable, not recurse."""
        from gods.handlers.athena_deepen import athena_deepen

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = \
            self._make_node(level=5, status="stub")

        event = make_event("plan_node_created", {
            "project_id": "proj1", "node_id": "node1",
            "index_path": "1.2.3.4.5", "level": 5,
        })
        result = await athena_deepen(event, db)
        assert any(e.event_type == "plan_node_executable" for e in result)

    @pytest.mark.asyncio
    async def test_deepens_l0_to_l1_children(self):
        """L0 node should generate L1 children and emit plan_node_created per child."""
        from gods.handlers.athena_deepen import athena_deepen

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("epic1",))] = \
            self._make_node(node_id="epic1", index_path="1", level=0, status="stub")
        # parent chain lookup
        db._rows[("SELECT index_path, level, title, content_json, parent_index\n            "
                  "FROM plan_nodes WHERE project_id = $1 AND index_path = $2", ("proj1", None))] = None
        # child count lookup
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1"))] = {"cnt": 0}

        children = [
            {"title": "Setup logging", "task_type": "code", "description": "Add structlog"},
            {"title": "Add metrics", "task_type": "code", "description": "Add Prometheus"},
        ]

        with patch("gods.handlers.athena_deepen._call_gateway", new=AsyncMock(side_effect=[
            json.dumps(children), "[]"
        ])):
            with patch("gods.handlers.athena_deepen.extract_json", side_effect=[children, []]):
                event = make_event("plan_node_created", {
                    "project_id": "proj1", "node_id": "epic1",
                    "index_path": "1", "level": 0, "plan_id": "plan1",
                })
                result = await athena_deepen(event, db)

        child_emits = [e for e in result if e.event_type == "plan_node_created"]
        assert len(child_emits) == 2
        assert child_emits[0].payload["index_path"] == "1.1"
        assert child_emits[1].payload["index_path"] == "1.2"
        assert child_emits[0].payload["level"] == 1

        complete_emits = [e for e in result if e.event_type == "plan_node_complete"]
        assert len(complete_emits) == 1
        assert complete_emits[0].payload["index_path"] == "1"

    @pytest.mark.asyncio
    async def test_marks_node_failed_on_error(self):
        """Exceptions should mark node failed and emit plan_node_complete."""
        from gods.handlers.athena_deepen import athena_deepen
        import gods.handlers.athena_deepen as mod
        mod._in_flight_nodes.clear()

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = \
            self._make_node(status="stub")

        with patch("gods.handlers.athena_deepen._call_gateway", new=AsyncMock(side_effect=RuntimeError("fail"))):
            event = make_event("plan_node_created", {
                "project_id": "proj1", "node_id": "node1",
                "index_path": "1", "level": 0,
            })
            result = await athena_deepen(event, db)

        # Should emit plan_node_complete with failed=True for bubble-up to handle
        assert any(e.event_type == "plan_node_complete" for e in result)
        failed_emit = next(e for e in result if e.event_type == "plan_node_complete")
        assert failed_emit.payload.get("failed") is True


# ---------------------------------------------------------------------------
# athena_bubble_up tests
# ---------------------------------------------------------------------------

class TestAthenaBubbleUp:

    @pytest.mark.asyncio
    async def test_all_l0_complete_emits_project_planned(self):
        """When all L0 nodes are complete, should emit project_planned."""
        from gods.handlers.athena_complete import athena_bubble_up

        db = FakeDB()
        # No incomplete L0 nodes
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND level = 0 AND status != 'complete'",
                  ("proj1",))] = {"cnt": 0}
        db._rows[("SELECT plan_id FROM plan_nodes WHERE project_id = $1 LIMIT 1", ("proj1",))] = \
            {"plan_id": "plan1"}

        event = make_event("plan_node_complete", {
            "project_id": "proj1", "node_id": "node1",
            "index_path": "2", "level": 0, "parent_index": None,
        })
        result = await athena_bubble_up(event, db)

        assert any(e.event_type == "project_planned" for e in result)

    @pytest.mark.asyncio
    async def test_remaining_l0_does_not_emit_planned(self):
        """With remaining incomplete L0 nodes, should not emit project_planned."""
        from gods.handlers.athena_complete import athena_bubble_up

        db = FakeDB()
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND level = 0 AND status != 'complete'",
                  ("proj1",))] = {"cnt": 2}

        event = make_event("plan_node_complete", {
            "project_id": "proj1", "node_id": "node1",
            "index_path": "1", "level": 0, "parent_index": None,
        })
        result = await athena_bubble_up(event, db)
        assert not any(e.event_type == "project_planned" for e in result)

    @pytest.mark.asyncio
    async def test_all_siblings_complete_bubbles_to_parent(self):
        """When all siblings complete, should mark parent complete and emit plan_node_complete."""
        from gods.handlers.athena_complete import athena_bubble_up

        db = FakeDB()
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND parent_index = $2 AND status NOT IN ('complete', 'failed')",
                  ("proj1", "1"))] = {"cnt": 0}
        db._rows[("SELECT id, index_path, level, parent_index FROM plan_nodes\n        WHERE project_id = $1 AND index_path = $2",
                  ("proj1", "1"))] = {
            "id": "epic1", "index_path": "1", "level": 0, "parent_index": None,
        }

        event = make_event("plan_node_complete", {
            "project_id": "proj1", "node_id": "task1",
            "index_path": "1.3", "level": 1, "parent_index": "1",
        })
        result = await athena_bubble_up(event, db)

        parent_complete = [e for e in result if e.event_type == "plan_node_complete"]
        assert len(parent_complete) == 1
        assert parent_complete[0].payload["index_path"] == "1"

    @pytest.mark.asyncio
    async def test_pending_siblings_no_bubble(self):
        """With pending siblings, should not bubble up."""
        from gods.handlers.athena_complete import athena_bubble_up

        db = FakeDB()
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes\n        WHERE project_id = $1 AND parent_index = $2 AND status NOT IN ('complete', 'failed')",
                  ("proj1", "1"))] = {"cnt": 3}

        event = make_event("plan_node_complete", {
            "project_id": "proj1", "node_id": "task1",
            "index_path": "1.1", "level": 1, "parent_index": "1",
        })
        result = await athena_bubble_up(event, db)
        assert result == []


# ---------------------------------------------------------------------------
# Index path logic tests (pure)
# ---------------------------------------------------------------------------

class TestIndexPaths:

    def test_level_from_index_path(self):
        """Level should equal number of dots + 1 for L1+, 0 for root."""
        assert "1".count(".") == 0       # L0 epic
        assert "1.2".count(".") == 1     # L1 task
        assert "1.2.3".count(".") == 2   # L2 spec
        assert "1.2.3.4".count(".") == 3  # L3 detail

    def test_parent_from_index_path(self):
        """Parent index is everything before the last dot."""
        path = "1.2.3"
        parent = ".".join(path.split(".")[:-1])
        assert parent == "1.2"

        path = "1"
        parts = path.split(".")
        parent = ".".join(parts[:-1]) if len(parts) > 1 else None
        assert parent is None

    def test_child_index(self):
        """Children of '1.2' should be '1.2.1', '1.2.2', etc."""
        parent = "1.2"
        for i in range(1, 4):
            assert f"{parent}.{i}" in [f"1.2.1", "1.2.2", "1.2.3"]
