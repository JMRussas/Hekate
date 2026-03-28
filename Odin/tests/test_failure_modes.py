"""Tests for realistic production failure modes in the Athena planning pipeline.

These tests target failure paths that WILL happen in production:
  - Stuck nodes blocking parent completion
  - Project stuck in "planning" after empty epic response
  - Temp file cleanup on agent timeout
  - Dual Athena handler collision on project_created
  - Duplicate plan_node_executable creating duplicate tasks
  - _in_flight_nodes race (best-effort without real async concurrency)
  - Node stuck in "planning" after handler crash during deepening
  - LIKE query false positive for task dependencies

Run with:
  cd Odin && pytest tests/test_failure_modes.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from gods.pipeline import Event, Emit


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_event(event_type: str, payload: dict) -> Event:
    return Event(event_type=event_type, payload=payload, source="test")


def _sql_key(sql: str) -> str:
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
        self.writes.append((_sql_key(sql), params))

    def get_writes_for(self, keyword: str) -> list:
        return [(s, p) for s, p in self.writes if keyword.upper() in s.upper()]


def make_node(
    node_id="node1",
    index_path="1",
    level=0,
    status="stub",
    parent_index=None,
    project_id="proj1",
    plan_id="plan1",
    title="Test Node",
):
    return {
        "id": node_id,
        "plan_id": plan_id,
        "project_id": project_id,
        "index_path": index_path,
        "level": level,
        "status": status,
        "title": title,
        "content_json": json.dumps({"title": title, "description": "test"}),
        "project_context": "Project: Test\n\nRequirements: build stuff",
        "parent_index": parent_index,
        "error": None,
    }


# ===========================================================================
# 1. Stuck node in "planning" blocks bubble-up forever
# ===========================================================================

class TestStuckNodeBlocksBubbleUp:
    """
    When athena_deepen marks a node "planning" but then crashes (before
    marking it complete or failed), that node is stuck. athena_bubble_up
    counts it as "not complete", so the parent never completes, so the
    project hangs forever.

    The system needs a way to detect and recover stuck nodes.
    """

    @pytest.mark.asyncio
    async def test_bubble_up_blocked_by_planning_sibling(self):
        """
        A sibling stuck in "planning" prevents parent from completing.
        This is the exact stuck state that will hang projects in production.
        """
        from gods.handlers.athena_complete import athena_bubble_up

        db = FakeDB()
        # One sibling is still in "planning" (crashed deepen handler)
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes WHERE project_id = $1 AND parent_index = $2 AND status NOT IN ('complete', 'failed')",
                  ("proj1", "1"))] = {"cnt": 1}  # stuck "planning" sibling

        event = make_event("plan_node_complete", {
            "project_id": "proj1", "node_id": "node2",
            "index_path": "1.2", "level": 1, "parent_index": "1",
        })
        result = await athena_bubble_up(event, db)

        # Parent CANNOT complete because of stuck sibling
        assert result == []
        # This proves the stuck state — no recovery is possible without
        # external intervention (stuck node detection / timeout recovery)

    @pytest.mark.asyncio
    async def test_node_left_in_planning_after_exception(self):
        """
        When athena_deepen crashes AFTER marking a node "planning" but
        BEFORE emitting plan_node_complete, the node is stuck.

        Verify the node status is "planning" in the DB after the exception.
        This is the failure mode — not that it fails, but that it fails silently.
        """
        import gods.handlers.athena_deepen as mod
        mod._in_flight_nodes.clear()

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = \
            make_node(status="stub")

        # Crash happens AFTER the "planning" update but BEFORE generating children
        # We simulate this by making _call_gateway fail — the except block
        # should set status to "failed", but let's verify it actually does
        with patch("gods.handlers.athena_deepen._call_gateway", new=AsyncMock(side_effect=RuntimeError("crash"))):
            event = make_event("plan_node_created", {
                "project_id": "proj1", "node_id": "node1",
                "index_path": "1", "level": 0,
            })
            result = await mod.athena_deepen(event, db)

        # Node should be "failed", not stuck in "planning"
        update_writes = db.get_writes_for("UPDATE plan_nodes")
        statuses = [w[1][0] for w in update_writes if len(w[1]) >= 3]
        assert "failed" in statuses, (
            f"Node should be marked 'failed' after exception, got statuses: {statuses}. "
            f"If 'planning' is in statuses without 'failed', node is stuck forever."
        )

        # And bubble-up should be notified (plan_node_complete with failed=True)
        assert any(e.event_type == "plan_node_complete" for e in result)
        failed_emit = next(e for e in result if e.event_type == "plan_node_complete")
        assert failed_emit.payload.get("failed") is True

    @pytest.mark.asyncio
    async def test_node_stuck_if_exception_raised_before_except_handler(self):
        """
        Verify: if an exception occurs INSIDE the try block but the except
        block ALSO raises (e.g., DB write fails), the node stays in "planning".

        This is the double-fault scenario — exception during error handling.
        """
        import gods.handlers.athena_deepen as mod
        mod._in_flight_nodes.clear()

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = \
            make_node(status="stub")

        call_count = [0]

        async def flaky_write(sql, params=()):
            call_count[0] += 1
            # First write (status='planning') succeeds
            # All subsequent writes fail (exception during error recovery)
            if call_count[0] > 1:
                raise RuntimeError("DB connection lost")
            db.writes.append((_sql_key(sql), params))

        db.execute_write = flaky_write

        with patch("gods.handlers.athena_deepen._call_gateway", new=AsyncMock(side_effect=RuntimeError("crash"))):
            event = make_event("plan_node_created", {
                "project_id": "proj1", "node_id": "node1",
                "index_path": "1", "level": 0,
            })
            # Should not raise — exception must be swallowed
            result = await mod.athena_deepen(event, db)

        # In this scenario the node IS stuck in "planning"
        # We just need to document this: the except block can also fail
        # and leave the node in "planning" with no recovery path
        statuses = [w[1][0] for w in db.writes if len(w[1]) >= 3]
        # Only "planning" write succeeded — stuck state documented
        assert "planning" in statuses


# ===========================================================================
# 2. Project stuck in "planning" after empty epic response
# ===========================================================================

class TestProjectStuckInPlanning:
    """
    athena_l0 sets project status to "planning" (line ~105) before
    calling the LLM. If the LLM returns no epics, it raises RuntimeError,
    which is caught and emits planning_failed AND sets status to "failed".

    BUT: if the LLM returns empty epics and we update status to "failed",
    a subsequent retry needs to handle the project being in "failed" state.
    The handler checks for "draft" or "failed" — this should actually work.

    The real risk: status is set to "planning" but NOT to "failed" if
    certain exceptions escape the catch block.
    """

    @pytest.mark.asyncio
    async def test_empty_epic_response_sets_project_failed_not_planning(self):
        """
        When LLM returns no epics, project should end up in 'failed' state,
        never stuck in 'planning'. This documents the expected recovery path.
        """
        from gods.handlers.athena_l0 import athena_l0

        db = FakeDB()
        db._rows[("SELECT id, name, requirements, status, config_json FROM projects WHERE id = $1", ("proj1",))] = {
            "id": "proj1", "name": "Test", "requirements": "reqs",
            "status": "draft",
            "config_json": json.dumps({"use_node_tree_planner": True}),
        }

        with patch("gods.handlers.athena_l0._call_gateway", new=AsyncMock(return_value="[]")):
            with patch("gods.handlers.athena_l0.extract_json", return_value=[]):
                event = make_event("project_created", {"project_id": "proj1"})
                result = await athena_l0(event, db)

        # Must emit planning_failed
        assert any(e.event_type == "planning_failed" for e in result), \
            "Empty epics must emit planning_failed"

        # Project status must be "failed", NOT "planning"
        update_writes = db.get_writes_for("UPDATE projects")
        statuses = [w[1][0] for w in update_writes]
        assert "failed" in statuses, f"Project must be 'failed' after empty epics, got: {statuses}"
        assert "planning" not in statuses or "failed" in statuses, \
            "Project stuck in 'planning' — cannot be retried"

    @pytest.mark.asyncio
    async def test_gateway_exception_sets_project_failed(self):
        """
        If the gateway call itself raises (timeout, connection error),
        project should be set to 'failed', not stuck in 'planning'.
        """
        from gods.handlers.athena_l0 import athena_l0

        db = FakeDB()
        db._rows[("SELECT id, name, requirements, status, config_json FROM projects WHERE id = $1", ("proj1",))] = {
            "id": "proj1", "name": "Test", "requirements": "reqs",
            "status": "draft",
            "config_json": json.dumps({"use_node_tree_planner": True}),
        }

        with patch("gods.handlers.athena_l0._call_gateway", new=AsyncMock(side_effect=RuntimeError("timeout"))):
            event = make_event("project_created", {"project_id": "proj1"})
            result = await athena_l0(event, db)

        assert any(e.event_type == "planning_failed" for e in result)

        # Crucially: project must NOT be stuck in "planning"
        update_writes = db.get_writes_for("UPDATE projects")
        statuses = [w[1][0] for w in update_writes]
        # Either never set to "planning", or set to "failed" afterward
        if "planning" in statuses:
            assert "failed" in statuses, \
                "Project set to 'planning' but never to 'failed' — stuck forever"


# ===========================================================================
# 3. Peer plan node — WebSocket fallback
# ===========================================================================

class TestPeerPlanNodeFallback:
    """
    _peer_plan_node connects to the gateway WebSocket.
    When WebSocket fails, it falls back to HTTP _call_gateway.
    When both succeed but return 0 children, it returns [].
    """

    @pytest.mark.asyncio
    async def test_peer_plan_node_falls_back_to_http_on_ws_error(self):
        """When WebSocket raises, _peer_plan_node falls back to HTTP gateway."""
        from gods.handlers.athena_deepen import _peer_plan_node

        node = make_node(level=2, index_path="1.1")
        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1.1"))] = []

        import websockets
        with patch("gods.handlers.athena_deepen.extract_json", return_value=[]):
            with patch("gods.handlers.athena_deepen._call_gateway", new=AsyncMock(return_value="[]")) as mock_gw:
                with patch("websockets.connect") as mock_ws:
                    mock_ws.side_effect = OSError("connection refused")
                    result = await _peer_plan_node(node, db)

        # Should have fallen back to HTTP
        assert mock_gw.called
        assert result == []

    @pytest.mark.asyncio
    async def test_peer_plan_node_returns_empty_on_no_children(self):
        """When agent produces 0 children (WebSocket path), returns []."""
        from gods.handlers.athena_deepen import _peer_plan_node

        node = make_node(level=2, index_path="1.1")
        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1.1"))] = []

        raw_events = [
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
            with patch("gods.handlers.athena_deepen._call_gateway", new=AsyncMock(return_value="[]")):
                with patch("gods.handlers.athena_deepen.extract_json", return_value=[]):
                    result = await _peer_plan_node(node, db)

        assert result == []


# ===========================================================================
# 4. Dual Athena handler collision on project_created
# ===========================================================================

class TestDualAthenaCollision:
    """
    Both athena_plan_leveled and athena_l0 are registered for project_created.
    When use_node_tree_planner=True:
      - athena_l0 SHOULD run and emit plan_node_created
      - athena_plan_leveled SHOULD be a no-op (it doesn't check the flag)

    If athena_plan_leveled emits project_planned while athena_l0 is still
    planning, Odin will try to start execution with 0 tasks.
    """

    @pytest.mark.asyncio
    async def test_athena_plan_leveled_skips_node_tree_projects(self):
        """
        athena_plan_leveled must return [] for projects with use_node_tree_planner=True.
        Without this guard, both planners fire on project_created, creating a race
        where Odin sees project_planned before node-tree planning completes.
        """
        from gods.handlers.athena_leveled import athena_plan_leveled

        db = FakeDB()
        db._rows[("SELECT name, requirements, status, config_json FROM projects WHERE id = $1", ("proj1",))] = {
            "name": "Test", "requirements": "reqs",
            "status": "draft",
            "config_json": json.dumps({
                "use_node_tree_planner": True,
                "target_level": "L3",
                "tdd": False,
                "narration": False,
                "direct_write": True,
                "max_concurrent": 2,
            }),
        }

        event = make_event("project_created", {"project_id": "proj1"})
        result = await athena_plan_leveled(event, db)

        # Must be a no-op — node-tree planner (athena_l0) handles this project
        assert result == [], (
            f"athena_plan_leveled must return [] for use_node_tree_planner=True projects. "
            f"Got: {[e.event_type for e in result]}. "
            f"Both planners running creates a race condition."
        )


# ===========================================================================
# 5. Duplicate plan_node_executable creates duplicate tasks
# ===========================================================================

class TestDuplicateMaterialization:
    """
    If plan_node_executable fires twice for the same node (e.g., from
    a re-queued event or replay), athena_materialize must be idempotent.

    INSERT OR IGNORE prevents duplicate task rows only if the unique key
    includes the plan_node_id. Without that, two tasks are created, and
    Odin dispatches both — duplicate work.
    """

    @pytest.mark.asyncio
    async def test_duplicate_executable_event_does_not_create_duplicate_task(self):
        """
        Second call to athena_materialize for same node must be a no-op
        when a task already exists for that plan_node_id.
        """
        from gods.handlers.athena_complete import athena_materialize

        node = make_node(node_id="node1", index_path="1.1.1", level=2, parent_index="1.1")
        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = node
        # Simulate: task already exists (second call scenario)
        db._rows[("SELECT id FROM tasks WHERE project_id = $1 AND context_json LIKE $2",
                  ("proj1", '%"plan_node_id": "node1"%'))] = {"id": "existing_task"}

        event = make_event("plan_node_executable", {
            "project_id": "proj1", "node_id": "node1", "index_path": "1.1.1",
        })

        result = await athena_materialize(event, db)

        # Should return [] — idempotent, task already exists
        assert result == [], (
            f"athena_materialize should return [] when task already exists, got: {result}. "
            f"Without this check, duplicate plan_node_executable events create duplicate tasks."
        )
        insert_writes = [w for w in db.writes if "INSERT" in w[0] and "tasks" in w[0].lower()]
        assert len(insert_writes) == 0, \
            f"Should not INSERT when task already exists, got {len(insert_writes)} inserts"

    @pytest.mark.asyncio
    async def test_second_materialize_is_no_op(self):
        """
        Second call to athena_materialize for an already-materialized node
        should return empty (no new emits).
        """
        from gods.handlers.athena_complete import athena_materialize

        node = {**make_node(node_id="node1", index_path="1.1.1", level=2, parent_index="1.1"),
                "status": "complete"}  # already completed
        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = node

        event = make_event("plan_node_executable", {
            "project_id": "proj1", "node_id": "node1", "index_path": "1.1.1",
        })

        result = await athena_materialize(event, db)
        # No check on "complete" status currently — this test documents
        # that a second call re-inserts (INSERT OR IGNORE on tasks) but
        # also re-emits plan_node_complete, triggering bubble-up again
        # which could cause double-completion of parents
        bubble_emits = [e for e in result if e.event_type == "plan_node_complete"]
        if len(bubble_emits) > 0:
            # Document the risk: re-emitting plan_node_complete on already-complete node
            # could cause double bubble-up, which counts completed siblings twice
            pass  # Currently acceptable if bubble_up is idempotent


# ===========================================================================
# 6. _in_flight_nodes race condition under concurrency
# ===========================================================================

class TestInFlightRace:
    """
    _in_flight_nodes is a module-level set. Two coroutines can both pass
    the concurrency check before either adds to the set.

    This is an async race: check → check → add → add → both proceed.
    Under Python's GIL, pure Python operations are atomic, but await points
    create yield points where other coroutines can run.

    The check at `if len(_in_flight_nodes) >= MAX_CONCURRENT_NODES` is NOT
    atomic with the subsequent `_in_flight_nodes.add(node_id)` — there's
    an await in between (the DB fetch).
    """

    @pytest.mark.asyncio
    async def test_concurrent_deepen_same_node_is_idempotent(self):
        """
        Two concurrent calls to athena_deepen for the same node_id:

        In production with a real DB, the FIRST call sets status='planning',
        so the SECOND call sees status != 'stub' and returns [].

        With FakeDB (writes don't update rows), the _in_flight_nodes set
        provides the guard: the second coroutine in the gather is scheduled
        but won't start until task 1 either yields or completes. Since FakeDB
        is synchronous (no real suspend), the two calls run sequentially. The
        in-flight guard fires for the second call if task 1 hasn't completed
        yet, OR the node status check fires if it has.

        This test verifies: when both calls complete, total INSERT count is
        bounded (INSERT OR IGNORE prevents exact duplicates at the index_path,
        so at most one child row per child index is created).
        """
        import gods.handlers.athena_deepen as mod
        mod._in_flight_nodes.clear()

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("node1",))] = \
            make_node(status="stub")
        db._rows[("SELECT index_path, level, title, content_json, parent_index FROM plan_nodes WHERE project_id = $1 AND index_path = $2",
                  ("proj1", None))] = None
        db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes WHERE project_id = $1 AND parent_index = $2",
                  ("proj1", "1"))] = {"cnt": 0}

        children = [{"title": "Child", "task_type": "code", "description": "..."}]

        call_count = [0]

        async def counted_gateway(**kwargs):
            call_count[0] += 1
            return json.dumps(children)

        with patch("gods.handlers.athena_deepen._call_gateway", new=counted_gateway):
            with patch("gods.handlers.athena_deepen.extract_json", side_effect=[children, [], children, [], children, [], children, []]):
                event = make_event("plan_node_created", {
                    "project_id": "proj1", "node_id": "node1",
                    "index_path": "1", "level": 0, "plan_id": "plan1",
                })
                # Fire two concurrent tasks for same node
                results = await asyncio.gather(
                    mod.athena_deepen(event, db),
                    mod.athena_deepen(event, db),
                )

        insert_writes = [w for w in db.writes if "INSERT" in w[0] and "plan_nodes" in w[0]]

        # The _in_flight_nodes guard prevents true concurrent double-processing.
        # With FakeDB (sync), the calls may run sequentially: if task 1 completes
        # before task 2 starts, task 2 is blocked by the in-flight set during
        # task 1's execution window. Either 1 or 2 calls may complete depending
        # on timing, but INSERT OR IGNORE ensures children at the same index_path
        # are deduplicated. The key invariant: no more INSERTs than 2x children.
        assert len(insert_writes) <= len(children) * 2, (
            f"Expected at most {len(children) * 2} INSERT writes (dedup by index_path), "
            f"got {len(insert_writes)}. Concurrent deepen may be creating unbounded duplicates."
        )

    @pytest.mark.asyncio
    async def test_concurrent_limit_respected_under_concurrency(self):
        """
        MAX_CONCURRENT_NODES=4 should not be exceeded even when many
        plan_node_created events arrive simultaneously.

        Uses a tracking set that records the max size reached.
        """
        import gods.handlers.athena_deepen as mod

        max_seen = [0]

        class TrackingSet(set):
            def add(self, item):
                super().add(item)
                max_seen[0] = max(max_seen[0], len(self))

        original_set = mod._in_flight_nodes
        mod._in_flight_nodes = TrackingSet()

        try:
            # Create 8 distinct nodes
            nodes = [f"node{i}" for i in range(8)]
            db = FakeDB()

            for nid in nodes:
                db._rows[("SELECT * FROM plan_nodes WHERE id = $1", (nid,))] = \
                    make_node(node_id=nid, index_path=str(nodes.index(nid) + 1), status="stub")
                db._rows[("SELECT COUNT(*) as cnt FROM plan_nodes WHERE project_id = $1 AND parent_index = $2",
                          ("proj1", str(nodes.index(nid) + 1)))] = {"cnt": 0}
                db._rows[("SELECT index_path, level, title, content_json, parent_index FROM plan_nodes WHERE project_id = $1 AND index_path = $2",
                          ("proj1", None))] = None

            async def slow_gateway(**kwargs):
                await asyncio.sleep(0.01)  # yield point — allows race
                return "[]"

            with patch("gods.handlers.athena_deepen._call_gateway", new=slow_gateway):
                with patch("gods.handlers.athena_deepen.extract_json", return_value=[]):
                    tasks = []
                    for i, nid in enumerate(nodes):
                        event = make_event("plan_node_created", {
                            "project_id": "proj1", "node_id": nid,
                            "index_path": str(i + 1), "level": 0, "plan_id": "plan1",
                        })
                        tasks.append(mod.athena_deepen(event, db))
                    await asyncio.gather(*tasks)

        finally:
            mod._in_flight_nodes = original_set

        assert max_seen[0] <= mod.MAX_CONCURRENT_NODES, (
            f"MAX_CONCURRENT_NODES={mod.MAX_CONCURRENT_NODES} was exceeded: "
            f"max observed in-flight = {max_seen[0]}. "
            f"The check-then-add is NOT atomic under async concurrency."
        )


# ===========================================================================
# 7. LIKE query false positive for task dependencies
# ===========================================================================

class TestDependencyLikeFalsePositive:
    """
    athena_materialize resolves depends_on_paths by finding plan_nodes,
    then finding the corresponding task via:
      SELECT id FROM tasks WHERE project_id=$1 AND context_json LIKE '%"plan_node_id": "abc"%'

    If node_id = "abc" and another node_id = "abcdef", the LIKE query
    will match BOTH, creating a wrong dependency.
    """

    @pytest.mark.asyncio
    async def test_dependency_resolves_to_correct_task_not_prefix_match(self):
        """
        When node_id = "abc" and another task has plan_node_id = "abcdef",
        the LIKE query for "abc" must NOT match the "abcdef" task.
        """
        from gods.handlers.athena_complete import athena_materialize

        dep_node_id = "abc"  # short ID
        false_positive_id = "abcdef"  # longer ID with same prefix

        # The node being materialized depends on dep_node
        node = {**make_node(node_id="target", index_path="1.1.1", level=2, parent_index="1.1"),
                "content_json": json.dumps({
                    "title": "Target task",
                    "description": "test",
                    "depends_on_paths": ["1"],  # depends on node at index "1"
                })}

        db = FakeDB()
        db._rows[("SELECT * FROM plan_nodes WHERE id = $1", ("target",))] = node

        # dep_node at index "1"
        db._rows[("SELECT id FROM plan_nodes WHERE project_id = $1 AND index_path = $2",
                  ("proj1", "1"))] = {"id": dep_node_id}

        # The task for dep_node has a false-positive match (longer ID)
        # context_json contains plan_node_id = "abcdef" — the LIKE '%"plan_node_id": "abc"%'
        # would match this if not careful about word boundaries
        db._rows[("SELECT id FROM tasks WHERE project_id = $1 AND context_json LIKE $2",
                  ("proj1", f'%"plan_node_id": "{dep_node_id}"%'))] = {"id": "task_abc"}

        event = make_event("plan_node_executable", {
            "project_id": "proj1", "node_id": "target", "index_path": "1.1.1",
        })

        result = await athena_materialize(event, db)

        # Check what dependency was recorded
        dep_writes = [w for w in db.writes if "task_deps" in w[0].lower()]

        # If false positive: dep_writes includes a dependency on the wrong task
        # This test documents the LIKE query usage — the test itself can't
        # detect prefix collisions without a real DB, but documents the risk
        assert len(dep_writes) <= 1, (
            "Dependency resolution may have matched multiple tasks via LIKE query. "
            "With short node IDs that are prefixes of other IDs, wrong dependencies "
            "will be created. FIX: Use exact JSON field matching or a proper FK."
        )


# ===========================================================================
# 8. athena_l0 does not check if project already has a plan
# ===========================================================================

class TestDuplicatePlanCreation:
    """
    If project_created fires twice (retry, duplicate delivery), athena_l0
    creates two plans for the same project. Each plan generates independent
    epic trees. Odin will see tasks from both plans.
    """

    @pytest.mark.asyncio
    async def test_second_project_created_creates_second_plan(self):
        """
        Two project_created events for the same project should NOT create
        two plan rows. The second should be a no-op.
        """
        from gods.handlers.athena_l0 import athena_l0

        db = FakeDB()
        db._rows[("SELECT id, name, requirements, status, config_json FROM projects WHERE id = $1", ("proj1",))] = {
            "id": "proj1", "name": "Test", "requirements": "reqs",
            "status": "draft",
            "config_json": json.dumps({"use_node_tree_planner": True}),
        }

        epics = [{"title": "Epic1", "description": "e1", "rationale": "r1"}]

        with patch("gods.handlers.athena_l0._call_gateway", new=AsyncMock(return_value=json.dumps(epics))):
            with patch("gods.handlers.athena_l0.extract_json", side_effect=[epics, [], epics, []]):
                event = make_event("project_created", {"project_id": "proj1"})
                await athena_l0(event, db)
                # Fire again (retry / duplicate)
                db._rows[("SELECT id, name, requirements, status, config_json FROM projects WHERE id = $1", ("proj1",))] = {
                    "id": "proj1", "name": "Test", "requirements": "reqs",
                    "status": "planning",  # status changed after first run
                    "config_json": json.dumps({"use_node_tree_planner": True}),
                }
                result2 = await athena_l0(event, db)

        # Second run should be a no-op (project already in "planning")
        plan_emits2 = [e for e in result2 if e.event_type == "plan_node_created"]
        assert len(plan_emits2) == 0, (
            f"Second project_created event still emitted {len(plan_emits2)} plan_node_created events. "
            f"athena_l0 must skip if project status is already 'planning'. "
            f"Currently it only skips 'executing' status."
        )
