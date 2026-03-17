#  Orchestration Engine - Integration Tests for Plan Sync + Status Lifecycle
#
#  Tests end-to-end flow: sync_plan creates mappings, task status transitions
#  trigger async updates to the context store, and simulated context store
#  downtime does not block task execution.
#
#  Depends on: backend/services/plan_sync.py, backend/services/task_lifecycle.py,
#              backend/services/context_store_client.py, tests/conftest.py
#  Used by:    CI test suite

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.enums import TaskStatus
from backend.services.context_store_client import ContextStoreClient
from backend.services.plan_sync import PlanSyncService

from tests.conftest import create_test_project, create_test_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_cs_client(*, create_ok=True, update_ok=True):
    """Build a mock ContextStoreClient that tracks calls."""
    cs = AsyncMock(spec=ContextStoreClient)
    _node_counter = {"n": 0}

    async def _create_node(parent_id, payload):
        if not create_ok:
            return None
        _node_counter["n"] += 1
        return payload.get("id", f"node-{_node_counter['n']}")

    cs.create_node = AsyncMock(side_effect=_create_node)
    cs.update_attributes = AsyncMock(return_value=update_ok)
    cs.close = AsyncMock()
    return cs


async def _seed_plan_with_tasks(db, project_id="proj1", plan_id=None, task_count=2):
    """Insert a project, plan, and N tasks. Returns (plan_id, task_ids)."""
    plan_id = plan_id or f"plan_{project_id}"
    now = time.time()

    await db.execute_write(
        "INSERT OR IGNORE INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, 'Test', 'test', 'draft', ?, ?)",
        (project_id, now, now),
    )

    plan_data = {"summary": "Test plan for sync"}
    await db.execute_write(
        "INSERT OR IGNORE INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', ?, 'approved', ?)",
        (plan_id, project_id, json.dumps(plan_data), now),
    )

    task_ids = []
    for i in range(task_count):
        tid = f"task_{project_id}_{i:03d}"
        await db.execute_write(
            "INSERT OR IGNORE INTO tasks (id, project_id, plan_id, title, description, "
            "task_type, priority, status, model_tier, wave, phase, retry_count, max_retries, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'test', 'code', ?, 'pending', 'haiku', 0, 'build', 0, 5, ?, ?)",
            (tid, project_id, plan_id, f"Task {i}", i, now, now),
        )
        task_ids.append(tid)

    return plan_id, task_ids


# ---------------------------------------------------------------------------
# sync_plan generates mappings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSyncPlanMapping:
    """Verify sync_plan creates context store nodes and persists the mapping."""

    async def test_sync_plan_returns_mapping(self, tmp_db):
        """sync_plan returns {task_id: node_id} for all tasks."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db, task_count=3)
        cs = _mock_cs_client()

        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        mapping = await svc.sync_plan("proj1", plan_id)

        assert len(mapping) == 3
        for tid in task_ids:
            assert tid in mapping
            assert mapping[tid] == f"task-{tid}"

    async def test_sync_plan_persists_mapping_to_db(self, tmp_db):
        """Mapping is persisted in the plans.node_mapping column."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db)
        cs = _mock_cs_client()

        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        await svc.sync_plan("proj1", plan_id)

        row = await tmp_db.fetchone(
            "SELECT node_mapping_json FROM plans WHERE id = ?", (plan_id,)
        )
        assert row is not None
        stored = json.loads(row["node_mapping_json"])
        assert len(stored) == 2
        for tid in task_ids:
            assert tid in stored

    async def test_get_node_mapping_loads_persisted(self, tmp_db):
        """get_node_mapping retrieves what sync_plan persisted."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db)
        cs = _mock_cs_client()

        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        original = await svc.sync_plan("proj1", plan_id)

        loaded = await svc.get_node_mapping(plan_id)
        assert loaded == original

    async def test_sync_plan_creates_plan_node_then_task_nodes(self, tmp_db):
        """First call creates plan node, subsequent calls create task nodes."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db, task_count=2)
        cs = _mock_cs_client()

        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        await svc.sync_plan("proj1", plan_id)

        # First create_node call is for the plan, rest are for tasks
        assert cs.create_node.call_count == 3  # 1 plan + 2 tasks
        # Plan node created under project_id
        first_call = cs.create_node.call_args_list[0]
        assert first_call[0][0] == "proj1"
        assert first_call[0][1]["type"] == "orchestration_plan"

    async def test_sync_plan_empty_when_no_tasks(self, tmp_db):
        """sync_plan returns empty dict when plan has no tasks."""
        now = time.time()
        await tmp_db.execute_write(
            "INSERT OR IGNORE INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES ('proj_empty', 'E', 'e', 'draft', ?, ?)",
            (now, now),
        )
        await tmp_db.execute_write(
            "INSERT OR IGNORE INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES ('plan_empty', 'proj_empty', 1, 'test', '{\"summary\":\"empty\"}', 'approved', ?)",
            (now,),
        )

        cs = _mock_cs_client()
        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        mapping = await svc.sync_plan("proj_empty", "plan_empty")

        assert mapping == {}
        cs.create_node.assert_not_called()


# ---------------------------------------------------------------------------
# Context store downtime — sync_plan degrades gracefully
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSyncPlanContextStoreDown:
    """When context store is unavailable, sync_plan returns empty and doesn't block."""

    async def test_sync_plan_returns_empty_when_cs_down(self, tmp_db):
        """When create_node returns None, sync_plan returns empty mapping."""
        plan_id, _ = await _seed_plan_with_tasks(tmp_db)
        cs = _mock_cs_client(create_ok=False)

        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        mapping = await svc.sync_plan("proj1", plan_id)

        assert mapping == {}

    async def test_no_mapping_persisted_when_cs_down(self, tmp_db):
        """No node_mapping saved when context store is unreachable."""
        plan_id, _ = await _seed_plan_with_tasks(tmp_db)
        cs = _mock_cs_client(create_ok=False)

        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        await svc.sync_plan("proj1", plan_id)

        row = await tmp_db.fetchone(
            "SELECT node_mapping_json FROM plans WHERE id = ?", (plan_id,)
        )
        # node_mapping should be NULL (never written)
        assert row["node_mapping_json"] is None


# ---------------------------------------------------------------------------
# _sync_status_to_context_store — fire-and-forget status updates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStatusSyncToContextStore:
    """Verify _sync_status_to_context_store updates the right node."""

    async def test_status_update_calls_update_attributes(self, tmp_db):
        """Status change finds the mapping and calls update_attributes."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db)
        cs = _mock_cs_client()

        # First sync to create mappings
        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        mapping = await svc.sync_plan("proj1", plan_id)

        # Now call _sync_status_to_context_store
        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_id=task_ids[0], plan_id=plan_id,
                status=TaskStatus.RUNNING,
            )

        cs.update_attributes.assert_called_once()
        call_args = cs.update_attributes.call_args
        assert call_args[0][0] == mapping[task_ids[0]]
        attrs = call_args[0][1]
        assert attrs["status"] == TaskStatus.RUNNING
        assert "updated_at" in attrs

    async def test_status_update_with_error_truncates(self, tmp_db):
        """Error string is truncated to 500 chars in attributes."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db)
        cs = _mock_cs_client()

        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        mapping = await svc.sync_plan("proj1", plan_id)

        long_error = "x" * 1000

        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_id=task_ids[0], plan_id=plan_id,
                status=TaskStatus.FAILED, error=long_error,
            )

        attrs = cs.update_attributes.call_args[0][1]
        assert attrs["error"] == "x" * 500

    async def test_status_update_noop_when_no_mapping(self, tmp_db):
        """When no mapping exists, update_attributes is never called."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_unmapped")
        cs = _mock_cs_client()

        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value={})
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_id="task_unmapped", plan_id="plan_proj1",
                status=TaskStatus.RUNNING,
            )

        cs.update_attributes.assert_not_called()


# ---------------------------------------------------------------------------
# Fire-and-forget: exceptions never propagate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFireAndForget:
    """Verify _sync_status_to_context_store never raises, even on failures."""

    async def test_exception_in_get_mapping_is_swallowed(self, tmp_db):
        """If get_node_mapping throws, the exception is swallowed."""
        await create_test_project(tmp_db)
        await create_test_task(tmp_db, "task_err_001")

        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(
                side_effect=Exception("DB connection lost")
            )
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            # Should NOT raise
            await _sync_status_to_context_store(
                db=tmp_db, task_id="task_err_001", plan_id="plan_proj1",
                status=TaskStatus.RUNNING,
            )

    async def test_exception_in_update_attributes_is_swallowed(self, tmp_db):
        """If update_attributes throws, the exception is swallowed."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db)
        mapping = {task_ids[0]: "node-abc"}

        cs = _mock_cs_client()
        cs.update_attributes = AsyncMock(side_effect=Exception("Connection refused"))

        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            # Should NOT raise
            await _sync_status_to_context_store(
                db=tmp_db, task_id=task_ids[0], plan_id=plan_id,
                status=TaskStatus.COMPLETED,
            )

    async def test_ensure_future_does_not_block_caller(self, tmp_db):
        """Simulates fire-and-forget via ensure_future — caller returns immediately."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db)
        mapping = {task_ids[0]: "node-xyz"}

        # Make update_attributes slow
        slow_cs = _mock_cs_client()

        async def _slow_update(node_id, attrs):
            await asyncio.sleep(0.5)
            return True

        slow_cs.update_attributes = AsyncMock(side_effect=_slow_update)

        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = slow_cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            start = time.monotonic()
            fut = asyncio.ensure_future(_sync_status_to_context_store(
                db=tmp_db, task_id=task_ids[0], plan_id=plan_id,
                status=TaskStatus.RUNNING,
            ))
            elapsed_before_await = time.monotonic() - start

            # ensure_future returns immediately — caller is not blocked
            assert elapsed_before_await < 0.1

            # But the update eventually completes
            await fut
            slow_cs.update_attributes.assert_called_once()


# ---------------------------------------------------------------------------
# Circuit breaker resilience
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCircuitBreakerResilience:
    """Verify that circuit breaker open state causes silent skip."""

    async def test_update_returns_false_when_circuit_open(self, tmp_db):
        """ContextStoreClient.update_attributes returns False when circuit is open."""
        cs = ContextStoreClient(base_url="http://localhost:5102")
        # Force circuit open
        cs._circuit_open_until = time.monotonic() + 60

        result = await cs.update_attributes("node-123", {"status": "running"})
        assert result is False

    async def test_create_node_returns_none_when_circuit_open(self, tmp_db):
        """ContextStoreClient.create_node returns None when circuit is open."""
        cs = ContextStoreClient(base_url="http://localhost:5102")
        cs._circuit_open_until = time.monotonic() + 60

        result = await cs.create_node("parent-1", {"id": "child-1", "type": "test"})
        assert result is None

    async def test_sync_plan_empty_when_circuit_open(self, tmp_db):
        """Full flow: sync_plan returns empty when circuit breaker is open."""
        plan_id, _ = await _seed_plan_with_tasks(tmp_db)
        cs = ContextStoreClient(base_url="http://localhost:5102")
        cs._circuit_open_until = time.monotonic() + 60

        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        mapping = await svc.sync_plan("proj1", plan_id)

        assert mapping == {}

    async def test_status_sync_silent_when_circuit_open(self, tmp_db):
        """Status sync completes without error when circuit breaker is open."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db)
        mapping = {task_ids[0]: "node-cb-test"}

        cs = ContextStoreClient(base_url="http://localhost:5102")
        cs._circuit_open_until = time.monotonic() + 60

        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            # Should NOT raise despite circuit being open
            await _sync_status_to_context_store(
                db=tmp_db, task_id=task_ids[0], plan_id=plan_id,
                status=TaskStatus.RUNNING,
            )


# ---------------------------------------------------------------------------
# End-to-end: sync → status transitions → context store updates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestEndToEndSyncFlow:
    """Full lifecycle: plan sync + multiple status transitions."""

    async def test_full_lifecycle_running_then_completed(self, tmp_db):
        """sync_plan → RUNNING update → COMPLETED update — both hit context store."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db, task_count=1)
        cs = _mock_cs_client()

        # Step 1: sync plan
        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        mapping = await svc.sync_plan("proj1", plan_id)
        assert len(mapping) == 1
        node_id = mapping[task_ids[0]]

        # Step 2: simulate RUNNING status update
        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_id=task_ids[0], plan_id=plan_id,
                status=TaskStatus.RUNNING,
            )

        # Verify RUNNING update
        running_call = cs.update_attributes.call_args_list[-1]
        assert running_call[0][0] == node_id
        assert running_call[0][1]["status"] == TaskStatus.RUNNING

        # Step 3: simulate COMPLETED status update
        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_id=task_ids[0], plan_id=plan_id,
                status=TaskStatus.COMPLETED,
            )

        completed_call = cs.update_attributes.call_args_list[-1]
        assert completed_call[0][0] == node_id
        assert completed_call[0][1]["status"] == TaskStatus.COMPLETED

    async def test_full_lifecycle_running_then_failed_with_error(self, tmp_db):
        """sync_plan → RUNNING → FAILED with error message in attributes."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db, task_count=1)
        cs = _mock_cs_client()

        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        mapping = await svc.sync_plan("proj1", plan_id)

        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_id=task_ids[0], plan_id=plan_id,
                status=TaskStatus.FAILED, error="Max retries exceeded",
            )

        attrs = cs.update_attributes.call_args[0][1]
        assert attrs["status"] == TaskStatus.FAILED
        assert attrs["error"] == "Max retries exceeded"

    async def test_multi_task_plan_independent_updates(self, tmp_db):
        """Multiple tasks in same plan get independent node updates."""
        plan_id, task_ids = await _seed_plan_with_tasks(tmp_db, task_count=3)
        cs = _mock_cs_client()

        svc = PlanSyncService(db=tmp_db, cs_client=cs)
        mapping = await svc.sync_plan("proj1", plan_id)

        with patch("backend.services.task_lifecycle.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            # Update each task to a different status
            statuses = [TaskStatus.RUNNING, TaskStatus.COMPLETED, TaskStatus.FAILED]
            for tid, status in zip(task_ids, statuses):
                await _sync_status_to_context_store(
                    db=tmp_db, task_id=tid, plan_id=plan_id, status=status,
                )

        # Each task's node got a separate update_attributes call
        update_calls = cs.update_attributes.call_args_list
        assert len(update_calls) == 3
        updated_nodes = {call[0][0] for call in update_calls}
        expected_nodes = {mapping[tid] for tid in task_ids}
        assert updated_nodes == expected_nodes
