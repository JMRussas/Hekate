#  Orchestration Engine - Executor Hardening Tests
#
#  Tests for graceful shutdown and stale task recovery.
#
#  Depends on: backend/services/executor.py, backend/db/connection.py
#  Used by:    pytest

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.enums import TaskStatus
from backend.services.executor import Executor


@pytest.fixture
async def executor_with_db(tmp_db):
    """Create an Executor wired to tmp_db with mocked external services."""
    mock_budget = AsyncMock()
    mock_budget.can_spend = AsyncMock(return_value=True)
    mock_budget.reserve_spend = AsyncMock(return_value=True)
    mock_budget.reserve_spend_project = AsyncMock(return_value=True)
    mock_budget.can_spend_project = AsyncMock(return_value=True)
    mock_budget.release_reservation = AsyncMock()
    mock_budget.release_reservation_project = AsyncMock()
    mock_budget.record_spend = AsyncMock()

    mock_progress = AsyncMock()
    mock_progress.push_event = AsyncMock()

    mock_rm = MagicMock()
    mock_rm.is_available = MagicMock(return_value=True)

    mock_registry = MagicMock()

    executor = Executor(
        db=tmp_db,
        budget=mock_budget,
        progress=mock_progress,
        resource_monitor=mock_rm,
        tool_registry=mock_registry,
    )

    return executor


# ---------------------------------------------------------------------------
# Stale Task Recovery
# ---------------------------------------------------------------------------

class TestStaleTaskRecovery:
    async def test_stale_running_task_recovered(self, tmp_db, executor_with_db):
        """Tasks stuck in 'running' longer than threshold are reset to pending."""
        now = time.time()
        stale_time = now - 600  # 10 min ago, well past 5 min threshold

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'executing', ?, ?)",
            ("proj_stale_001", "Stale Test", "test", now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'approved', ?)",
            ("plan_stale_001", "proj_stale_001", json.dumps({"summary": "test", "tasks": []}), now),
        )
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, max_retries, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task_stale_001", "proj_stale_001", "plan_stale_001", "Stale Task",
             "Do something", "code", 0, TaskStatus.RUNNING, "haiku", 0,
             1, 5, stale_time, stale_time),
        )

        await executor_with_db._recover_stale_tasks()

        row = await tmp_db.fetchone("SELECT status, retry_count, error FROM tasks WHERE id = ?",
                                     ("task_stale_001",))
        assert row["status"] == TaskStatus.PENDING
        assert row["retry_count"] == 2  # Was 1, incremented to 2
        assert "Recovered from stale state" in row["error"]

    async def test_queued_task_recovered_without_retry_increment(self, tmp_db, executor_with_db):
        """Queued tasks are recovered but retry_count is NOT incremented
        (they hadn't started execution, so no retry was consumed)."""
        now = time.time()

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'executing', ?, ?)",
            ("proj_stale_002", "Stale Queued", "test", now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'approved', ?)",
            ("plan_stale_002", "proj_stale_002", json.dumps({"summary": "test", "tasks": []}), now),
        )
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, max_retries, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task_stale_002", "proj_stale_002", "plan_stale_002", "Queued Task",
             "Do something", "code", 0, TaskStatus.QUEUED, "haiku", 0,
             0, 5, now, now),
        )

        await executor_with_db._recover_stale_tasks()

        row = await tmp_db.fetchone("SELECT status, retry_count, error FROM tasks WHERE id = ?",
                                     ("task_stale_002",))
        assert row["status"] == TaskStatus.PENDING
        assert row["retry_count"] == 0  # NOT incremented for queued tasks
        assert "queued state" in row["error"]

    async def test_no_stale_tasks_is_noop(self, tmp_db, executor_with_db):
        """Recovery with no stale tasks should complete without error."""
        # No tasks exist at all — should just return
        await executor_with_db._recover_stale_tasks()
        # If we got here without exception, test passes

    async def test_stale_task_with_unmet_deps_becomes_blocked(self, tmp_db, executor_with_db):
        """Stale tasks with incomplete dependencies should recover to BLOCKED, not PENDING."""
        now = time.time()
        stale_time = now - 600

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'executing', ?, ?)",
            ("proj_stale_dep", "Stale Dep Test", "test", now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'approved', ?)",
            ("plan_stale_dep", "proj_stale_dep", json.dumps({"summary": "test", "tasks": []}), now),
        )
        # Task A: dependency, still pending (not completed)
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, max_retries, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task_dep_a", "proj_stale_dep", "plan_stale_dep", "Dep Task A",
             "Dependency", "code", 0, TaskStatus.PENDING, "haiku", 0,
             0, 5, now, now),
        )
        # Task B: stale running, depends on Task A
        await tmp_db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
            "priority, status, model_tier, wave, retry_count, max_retries, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("task_dep_b", "proj_stale_dep", "plan_stale_dep", "Stale Task B",
             "Depends on A", "code", 0, TaskStatus.RUNNING, "haiku", 1,
             0, 5, stale_time, stale_time),
        )
        await tmp_db.execute_write(
            "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)",
            ("task_dep_b", "task_dep_a"),
        )

        await executor_with_db._recover_stale_tasks()

        row = await tmp_db.fetchone("SELECT status, retry_count FROM tasks WHERE id = ?",
                                     ("task_dep_b",))
        assert row["status"] == TaskStatus.BLOCKED
        assert row["retry_count"] == 1


# ---------------------------------------------------------------------------
# Graceful Shutdown
# ---------------------------------------------------------------------------

class TestGracefulShutdown:
    async def test_stop_waits_for_in_flight_tasks(self, executor_with_db):
        """Stop should wait for in-flight tasks to complete within grace period."""
        completed = False

        async def slow_task():
            nonlocal completed
            await asyncio.sleep(0.1)
            completed = True

        handle = asyncio.create_task(slow_task())
        executor_with_db._in_flight.add(handle)
        handle.add_done_callback(executor_with_db._in_flight.discard)

        await executor_with_db.stop(grace_seconds=5)

        assert completed is True
        assert len(executor_with_db._in_flight) == 0

    async def test_stop_cancels_after_grace_period(self, executor_with_db):
        """Tasks still running after grace period should be cancelled."""
        cancelled = False

        async def very_slow_task():
            nonlocal cancelled
            try:
                await asyncio.sleep(60)  # Way longer than grace
            except asyncio.CancelledError:
                cancelled = True
                raise

        handle = asyncio.create_task(very_slow_task())
        executor_with_db._in_flight.add(handle)

        await executor_with_db.stop(grace_seconds=0.1)

        assert cancelled is True
        assert len(executor_with_db._in_flight) == 0

    async def test_stop_with_no_in_flight(self, executor_with_db):
        """Stop with no in-flight tasks should complete immediately."""
        await executor_with_db.stop(grace_seconds=1)
        assert len(executor_with_db._in_flight) == 0

    async def test_stop_resets_running_tasks_to_pending(self, tmp_db, executor_with_db):
        """Shutdown should reset running/queued tasks to pending in the database."""
        now = time.time()

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'executing', ?, ?)",
            ("proj_shutdown", "Shutdown Test", "test", now, now),
        )
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'approved', ?)",
            ("plan_shutdown", "proj_shutdown", json.dumps({"summary": "test", "tasks": []}), now),
        )
        # Insert a running and a queued task
        for tid, status in [("task_run", TaskStatus.RUNNING), ("task_queue", TaskStatus.QUEUED)]:
            await tmp_db.execute_write(
                "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
                "priority, status, model_tier, wave, retry_count, max_retries, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tid, "proj_shutdown", "plan_shutdown", f"Task {tid}",
                 "test", "code", 0, status, "haiku", 0, 0, 5, now, now),
            )

        await executor_with_db.stop(grace_seconds=0.1)

        for tid in ["task_run", "task_queue"]:
            row = await tmp_db.fetchone("SELECT status, error FROM tasks WHERE id = ?", (tid,))
            assert row["status"] == TaskStatus.PENDING
            assert "Interrupted by shutdown" in row["error"]

    async def test_stop_clears_dispatched_set(self, executor_with_db):
        """Shutdown should clear the dispatched tracking set."""
        executor_with_db._dispatched.add("task_1")
        executor_with_db._dispatched.add("task_2")

        await executor_with_db.stop(grace_seconds=0.1)

        assert len(executor_with_db._dispatched) == 0

    async def test_stop_clears_resource_skip_cache(self, executor_with_db):
        """Shutdown should clear the resource circuit breaker cache."""
        executor_with_db._resource_skip_until["ollama_local"] = 999999
        executor_with_db._resource_skip_until["anthropic_api"] = 999999

        await executor_with_db.stop(grace_seconds=0.1)

        assert len(executor_with_db._resource_skip_until) == 0


# ---------------------------------------------------------------------------
# Resource Circuit Breaker
# ---------------------------------------------------------------------------

class TestResourceCircuitBreaker:
    def test_offline_resource_skipped_for_30s(self, executor_with_db):
        """After a resource goes offline, skip re-checking for 30 seconds."""
        rm = executor_with_db._resource_monitor
        rm.is_available = MagicMock(return_value=False)

        # First check: resource is offline, should add to skip cache
        task_row = {"model_tier": "ollama", "tools_json": None}
        assert executor_with_db._resources_available(task_row) is False

        # Verify it was cached
        assert "ollama_local" in executor_with_db._resource_skip_until

        # Second check: should return False without calling is_available again
        rm.is_available.reset_mock()
        assert executor_with_db._resources_available(task_row) is False
        rm.is_available.assert_not_called()

    def test_resource_comes_back_online(self, executor_with_db):
        """Resource coming back online clears the skip cache."""
        rm = executor_with_db._resource_monitor

        # First: resource offline → cached
        rm.is_available = MagicMock(return_value=False)
        task_row = {"model_tier": "ollama", "tools_json": None}
        executor_with_db._resources_available(task_row)
        assert "ollama_local" in executor_with_db._resource_skip_until

        # Expire the cache
        executor_with_db._resource_skip_until["ollama_local"] = 0

        # Now resource is online
        rm.is_available = MagicMock(return_value=True)
        assert executor_with_db._resources_available(task_row) is True
        assert "ollama_local" not in executor_with_db._resource_skip_until
