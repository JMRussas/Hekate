#  Orchestration Engine - Integration Tests for Task Lifecycle Status Sync
#
#  Verifies that task completion triggers asynchronous context store attribute
#  updates, and that context store failures (circuit breaker open, exceptions)
#  never block or disrupt task execution.
#
#  Depends on: backend/services/task_lifecycle.py, backend/services/plan_sync.py,
#              backend/services/context_store_client.py, tests/conftest.py
#  Used by:    CI test suite

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from backend.models.enums import TaskStatus
from backend.services.context_store_client import ContextStoreClient

from tests.conftest import create_test_project, create_test_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_plan_with_mapping(db, project_id="proj1", plan_id=None,
                                   task_titles=None, mapping=None):
    """Insert project, plan with node_mapping_json, and tasks by title.

    Returns (plan_id, mapping_dict).
    """
    plan_id = plan_id or f"plan_{project_id}"
    task_titles = task_titles or ["Implement auth", "Write tests"]
    now = time.time()

    await db.execute_write(
        "INSERT OR IGNORE INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, 'Test', 'test', 'draft', ?, ?)",
        (project_id, now, now),
    )

    # Build mapping: title → fake node ID
    if mapping is None:
        mapping = {title: f"csnode-{i}" for i, title in enumerate(task_titles)}

    plan_data = {"summary": "Test plan"}
    await db.execute_write(
        "INSERT OR IGNORE INTO plans (id, project_id, version, model_used, plan_json, "
        "status, node_mapping_json, created_at) "
        "VALUES (?, ?, 1, 'test', ?, 'approved', ?, ?)",
        (plan_id, project_id, json.dumps(plan_data), json.dumps(mapping), now),
    )

    for i, title in enumerate(task_titles):
        tid = f"task_{project_id}_{i:03d}"
        await db.execute_write(
            "INSERT OR IGNORE INTO tasks (id, project_id, plan_id, title, description, "
            "task_type, priority, status, model_tier, wave, phase, retry_count, max_retries, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'test desc', 'code', ?, 'pending', 'haiku', 0, 'build', 0, 5, ?, ?)",
            (tid, project_id, plan_id, title, i, now, now),
        )

    return plan_id, mapping


def _make_circuit_open_client():
    """ContextStoreClient with circuit breaker forced open."""
    cs = ContextStoreClient(base_url="http://localhost:5102")
    cs._circuit_open_until = time.monotonic() + 60
    return cs


# ---------------------------------------------------------------------------
# Task completion triggers context store update
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTaskCompletionTriggersUpdate:
    """Verify that task status changes call update_attributes on the context store."""

    async def test_completed_status_updates_context_store_node(self, tmp_db):
        """COMPLETED status triggers update_attributes with correct node ID and attrs."""
        titles = ["Build API endpoint"]
        plan_id, mapping = await _seed_plan_with_mapping(
            tmp_db, task_titles=titles,
        )

        mock_cs = AsyncMock(spec=ContextStoreClient)
        mock_cs.update_attributes = AsyncMock(return_value=True)
        mock_cs._is_circuit_open = lambda: False

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = mock_cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_title="Build API endpoint", plan_id=plan_id,
                status=TaskStatus.COMPLETED,
            )

        mock_cs.update_attributes.assert_called_once()
        call_args = mock_cs.update_attributes.call_args
        assert call_args[0][0] == mapping["Build API endpoint"]
        attrs = call_args[0][1]
        assert attrs["status"] == TaskStatus.COMPLETED
        assert "updated_at" in attrs
        assert "error" not in attrs

    async def test_failed_status_includes_error_in_attributes(self, tmp_db):
        """FAILED status sends error string in update_attributes."""
        titles = ["Run migrations"]
        plan_id, mapping = await _seed_plan_with_mapping(
            tmp_db, task_titles=titles,
        )

        mock_cs = AsyncMock(spec=ContextStoreClient)
        mock_cs.update_attributes = AsyncMock(return_value=True)
        mock_cs._is_circuit_open = lambda: False

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = mock_cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_title="Run migrations", plan_id=plan_id,
                status=TaskStatus.FAILED, error="Alembic head mismatch",
            )

        attrs = mock_cs.update_attributes.call_args[0][1]
        assert attrs["status"] == TaskStatus.FAILED
        assert attrs["error"] == "Alembic head mismatch"

    async def test_running_status_triggers_update(self, tmp_db):
        """RUNNING status also triggers context store update."""
        titles = ["Scaffold models"]
        plan_id, mapping = await _seed_plan_with_mapping(
            tmp_db, task_titles=titles,
        )

        mock_cs = AsyncMock(spec=ContextStoreClient)
        mock_cs.update_attributes = AsyncMock(return_value=True)
        mock_cs._is_circuit_open = lambda: False

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = mock_cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_title="Scaffold models", plan_id=plan_id,
                status=TaskStatus.RUNNING,
            )

        attrs = mock_cs.update_attributes.call_args[0][1]
        assert attrs["status"] == TaskStatus.RUNNING

    async def test_error_truncated_to_500_chars(self, tmp_db):
        """Long error messages are truncated to 500 characters."""
        titles = ["Parse config"]
        plan_id, mapping = await _seed_plan_with_mapping(
            tmp_db, task_titles=titles,
        )

        mock_cs = AsyncMock(spec=ContextStoreClient)
        mock_cs.update_attributes = AsyncMock(return_value=True)
        mock_cs._is_circuit_open = lambda: False

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = mock_cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            long_error = "E" * 1000
            await _sync_status_to_context_store(
                db=tmp_db, task_title="Parse config", plan_id=plan_id,
                status=TaskStatus.FAILED, error=long_error,
            )

        attrs = mock_cs.update_attributes.call_args[0][1]
        assert len(attrs["error"]) == 500

    async def test_title_not_in_mapping_skips_update(self, tmp_db):
        """When task title has no mapping entry, update_attributes is never called."""
        plan_id, mapping = await _seed_plan_with_mapping(
            tmp_db, task_titles=["Known task"],
        )

        mock_cs = AsyncMock(spec=ContextStoreClient)
        mock_cs._is_circuit_open = lambda: False

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = mock_cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_title="Unknown task", plan_id=plan_id,
                status=TaskStatus.COMPLETED,
            )

        mock_cs.update_attributes.assert_not_called()


# ---------------------------------------------------------------------------
# Circuit breaker open — silently skip, never block
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCircuitBreakerSkipsGracefully:
    """When context store circuit breaker is open, status sync is a silent no-op."""

    async def test_circuit_open_skips_update_attributes(self, tmp_db):
        """Circuit breaker open causes early return — no HTTP call made."""
        titles = ["Deploy service"]
        plan_id, mapping = await _seed_plan_with_mapping(
            tmp_db, task_titles=titles,
        )

        cs = _make_circuit_open_client()

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            # Should complete without error despite circuit being open
            await _sync_status_to_context_store(
                db=tmp_db, task_title="Deploy service", plan_id=plan_id,
                status=TaskStatus.RUNNING,
            )

        # No exception raised — that's the assertion

    async def test_circuit_open_does_not_call_update_attributes(self, tmp_db):
        """When circuit is open, update_attributes returns False (short-circuited)."""
        cs = _make_circuit_open_client()
        result = await cs.update_attributes("node-123", {"status": "running"})
        assert result is False

    async def test_task_execution_unaffected_by_circuit_open(self, tmp_db):
        """Simulates full fire-and-forget path — caller returns immediately."""
        titles = ["Run linter"]
        plan_id, mapping = await _seed_plan_with_mapping(
            tmp_db, task_titles=titles,
        )

        cs = _make_circuit_open_client()

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            start = time.monotonic()
            fut = asyncio.ensure_future(_sync_status_to_context_store(
                db=tmp_db, task_title="Run linter", plan_id=plan_id,
                status=TaskStatus.COMPLETED,
            ))
            schedule_time = time.monotonic() - start
            # ensure_future returns immediately — caller is not blocked
            assert schedule_time < 0.05

            await fut  # clean up


# ---------------------------------------------------------------------------
# Exception resilience — never propagate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestExceptionResilience:
    """Verify _sync_status_to_context_store swallows all exceptions."""

    async def test_get_node_mapping_exception_swallowed(self, tmp_db):
        """Exception in get_node_mapping does not propagate."""
        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(
                side_effect=Exception("DB locked"),
            )
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            # Must not raise
            await _sync_status_to_context_store(
                db=tmp_db, task_title="Any task", plan_id="plan_x",
                status=TaskStatus.RUNNING,
            )

    async def test_update_attributes_exception_swallowed(self, tmp_db):
        """Exception in update_attributes does not propagate."""
        mapping = {"Crashing task": "node-crash"}

        mock_cs = AsyncMock(spec=ContextStoreClient)
        mock_cs.update_attributes = AsyncMock(
            side_effect=Exception("Connection refused"),
        )
        mock_cs._is_circuit_open = lambda: False

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = mock_cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            # Must not raise
            await _sync_status_to_context_store(
                db=tmp_db, task_title="Crashing task", plan_id="plan_y",
                status=TaskStatus.COMPLETED,
            )

    async def test_update_attributes_timeout_swallowed(self, tmp_db):
        """Timeout in update_attributes does not propagate."""
        mapping = {"Slow task": "node-slow"}

        mock_cs = AsyncMock(spec=ContextStoreClient)
        mock_cs.update_attributes = AsyncMock(
            side_effect=asyncio.TimeoutError(),
        )
        mock_cs._is_circuit_open = lambda: False

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = mock_cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_title="Slow task", plan_id="plan_z",
                status=TaskStatus.FAILED, error="timed out",
            )

    async def test_fire_and_forget_ensure_future_resilient(self, tmp_db):
        """ensure_future wrapping swallows internal errors — caller unblocked."""
        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(
                side_effect=RuntimeError("unexpected"),
            )
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            fut = asyncio.ensure_future(_sync_status_to_context_store(
                db=tmp_db, task_title="Whatever", plan_id="plan_nope",
                status=TaskStatus.RUNNING,
            ))
            # Await to confirm it completes without raising
            await fut


# ---------------------------------------------------------------------------
# End-to-end: multiple status transitions on same task
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestMultiStatusTransitions:
    """Full lifecycle: task goes through RUNNING → COMPLETED with both updates."""

    async def test_running_then_completed_both_update(self, tmp_db):
        """Two sequential status updates each call update_attributes."""
        titles = ["Refactor auth"]
        plan_id, mapping = await _seed_plan_with_mapping(
            tmp_db, task_titles=titles,
        )
        node_id = mapping["Refactor auth"]

        mock_cs = AsyncMock(spec=ContextStoreClient)
        mock_cs.update_attributes = AsyncMock(return_value=True)
        mock_cs._is_circuit_open = lambda: False

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = mock_cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            await _sync_status_to_context_store(
                db=tmp_db, task_title="Refactor auth", plan_id=plan_id,
                status=TaskStatus.RUNNING,
            )
            await _sync_status_to_context_store(
                db=tmp_db, task_title="Refactor auth", plan_id=plan_id,
                status=TaskStatus.COMPLETED,
            )

        assert mock_cs.update_attributes.call_count == 2
        first_attrs = mock_cs.update_attributes.call_args_list[0][0][1]
        second_attrs = mock_cs.update_attributes.call_args_list[1][0][1]
        assert first_attrs["status"] == TaskStatus.RUNNING
        assert second_attrs["status"] == TaskStatus.COMPLETED
        # Both targeted the same node
        assert mock_cs.update_attributes.call_args_list[0][0][0] == node_id
        assert mock_cs.update_attributes.call_args_list[1][0][0] == node_id

    async def test_multi_task_plan_independent_nodes(self, tmp_db):
        """Each task in a plan updates its own context store node."""
        titles = ["Task Alpha", "Task Beta", "Task Gamma"]
        plan_id, mapping = await _seed_plan_with_mapping(
            tmp_db, task_titles=titles,
        )

        mock_cs = AsyncMock(spec=ContextStoreClient)
        mock_cs.update_attributes = AsyncMock(return_value=True)
        mock_cs._is_circuit_open = lambda: False

        with patch("backend.services.plan_sync.PlanSyncService") as MockSvc:
            mock_instance = AsyncMock()
            mock_instance.get_node_mapping = AsyncMock(return_value=mapping)
            mock_instance._cs = mock_cs
            MockSvc.return_value = mock_instance

            from backend.services.task_lifecycle import _sync_status_to_context_store

            for title, status in zip(titles, [
                TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.RUNNING,
            ]):
                await _sync_status_to_context_store(
                    db=tmp_db, task_title=title, plan_id=plan_id,
                    status=status,
                )

        assert mock_cs.update_attributes.call_count == 3
        updated_nodes = {
            call[0][0] for call in mock_cs.update_attributes.call_args_list
        }
        expected_nodes = set(mapping.values())
        assert updated_nodes == expected_nodes
