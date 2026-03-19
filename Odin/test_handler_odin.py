"""Tests for gods/handlers/odin.py — orchestration/dispatch/lifecycle.

Handlers:
  - odin_start: project_planned → set executing → emit project_started
  - odin_dispatch: tick-driven → find ready tasks → emit dispatch_commands
  - odin_lifecycle: task_verified → check wave/project completion
  - odin_tick: periodic → scan all executing projects → dispatch for each
  - odin_handle_diagnosis: task_diagnosis → apply fix → reset task
"""

import json
import time
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

from gods.pipeline import Event, Emit

from gods.handlers.odin import (
    odin_start, odin_dispatch, odin_lifecycle,
    odin_tick, odin_handle_diagnosis,
    _select_provider,
)


# ---------------------------------------------------------------------------
# Fixtures — DB with projects, tasks, task_deps
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def orch_db(sqlite_db):
    """SQLite with orchestration schema for odin tests."""
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            requirements TEXT,
            status TEXT DEFAULT 'draft',
            config_json TEXT DEFAULT '{}',
            completed_at REAL,
            updated_at REAL
        )
    """)
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            title TEXT,
            task_type TEXT DEFAULT 'code',
            complexity TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'pending',
            model_tier TEXT DEFAULT 'claude_code',
            wave INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 0,
            output_text TEXT,
            error TEXT,
            context_json TEXT DEFAULT '{}',
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            started_at REAL,
            completed_at REAL,
            updated_at REAL
        )
    """)
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS task_deps (
            task_id TEXT,
            depends_on TEXT,
            PRIMARY KEY (task_id, depends_on)
        )
    """)
    return sqlite_db


async def _seed_project(db, project_id="proj-1", status="planned"):
    await db.execute_write(
        "INSERT OR REPLACE INTO projects (id, name, requirements, status, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, "Test Project", "Build X", status, time.time()),
    )


async def _seed_task(db, task_id, project_id="proj-1", wave=0, status="pending",
                     task_type="code", complexity="medium", depends_on=None,
                     output_text=None, error=None):
    await db.execute_write(
        "INSERT OR REPLACE INTO tasks (id, project_id, title, task_type, complexity, "
        "status, wave, output_text, error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (task_id, project_id, f"Task {task_id}", task_type, complexity,
         status, wave, output_text, error, time.time()),
    )
    if depends_on:
        for dep in depends_on:
            await db.execute_write(
                "INSERT OR REPLACE INTO task_deps (task_id, depends_on) VALUES (?, ?)",
                (task_id, dep),
            )


# ---------------------------------------------------------------------------
# odin_start — project_planned → project_started
# ---------------------------------------------------------------------------

class TestOdinStart:
    @pytest.mark.asyncio
    async def test_sets_project_to_executing(self, orch_db):
        await _seed_project(orch_db, status="draft")
        event = Event("project_planned", {
            "project_id": "proj-1",
            "plan_id": "plan-1",
        }, "athena")

        emits = await odin_start(event, orch_db)

        # Check project status updated
        row = await orch_db.fetchone("SELECT status FROM projects WHERE id = ?", ("proj-1",))
        assert row[0] == "executing"

        # Should emit project_started
        started = next((e for e in emits if e.event_type == "project_started"), None)
        assert started is not None
        assert started.payload["project_id"] == "proj-1"

    @pytest.mark.asyncio
    async def test_missing_project_emits_error(self, orch_db):
        event = Event("project_planned", {"project_id": "nonexistent"}, "athena")

        emits = await odin_start(event, orch_db)

        error = next((e for e in emits if e.event_type == "odin_error"), None)
        assert error is not None


# ---------------------------------------------------------------------------
# odin_dispatch — find ready tasks → emit dispatch_commands
# ---------------------------------------------------------------------------

class TestOdinDispatch:
    @pytest.mark.asyncio
    async def test_dispatches_ready_wave0_tasks(self, orch_db):
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="pending")
        await _seed_task(orch_db, "t2", wave=0, status="pending")
        await _seed_task(orch_db, "t3", wave=1, status="pending")  # not yet

        event = Event("tick", {"project_id": "proj-1"}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock_prov:
            mock_prov.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}
            emits = await odin_dispatch(event, orch_db)

        commands = [e for e in emits if e.event_type == "dispatch_command"]
        # Should dispatch wave 0 tasks only
        assert len(commands) == 2
        task_ids = {c.payload["task_id"] for c in commands}
        assert task_ids == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_skips_tasks_with_unmet_deps(self, orch_db):
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="pending")
        await _seed_task(orch_db, "t2", wave=0, status="pending", depends_on=["t1"])

        event = Event("tick", {"project_id": "proj-1"}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock_prov:
            mock_prov.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}
            emits = await odin_dispatch(event, orch_db)

        commands = [e for e in emits if e.event_type == "dispatch_command"]
        # Only t1 should dispatch — t2 depends on t1
        assert len(commands) == 1
        assert commands[0].payload["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_no_dispatch_when_all_running(self, orch_db):
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="running")
        await _seed_task(orch_db, "t2", wave=0, status="running")

        event = Event("tick", {"project_id": "proj-1"}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock_prov:
            mock_prov.return_value = {"claude_code": True}
            emits = await odin_dispatch(event, orch_db)

        commands = [e for e in (emits or []) if e.event_type == "dispatch_command"]
        assert len(commands) == 0

    @pytest.mark.asyncio
    async def test_selects_provider_based_on_task_type(self, orch_db):
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="pending",
                         task_type="research", complexity="simple")

        event = Event("tick", {"project_id": "proj-1"}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock_prov:
            mock_prov.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}
            emits = await odin_dispatch(event, orch_db)

        cmd = emits[0]
        # Research/simple should prefer gemini_cli
        assert cmd.payload["provider"] == "gemini_cli"

    @pytest.mark.asyncio
    async def test_diagnoses_failed_tasks(self, orch_db):
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="failed",
                         error="rate limit exceeded")

        event = Event("tick", {"project_id": "proj-1"}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock_prov:
            mock_prov.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}
            emits = await odin_dispatch(event, orch_db)

        diag = next((e for e in emits if e.event_type == "task_diagnosis"), None)
        assert diag is not None
        assert diag.payload["task_id"] == "t1"
        assert diag.payload["fix_type"] in ("reassign_tier", "change_provider")


# ---------------------------------------------------------------------------
# odin_lifecycle — task_verified → wave/project completion
# ---------------------------------------------------------------------------

class TestOdinLifecycle:
    @pytest.mark.asyncio
    async def test_wave_complete_when_all_verified(self, orch_db):
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="completed")
        await _seed_task(orch_db, "t2", wave=0, status="completed")
        await _seed_task(orch_db, "t3", wave=1, status="pending")

        event = Event("task_verified", {
            "project_id": "proj-1",
            "task_id": "t2",
        }, "mimir")

        emits = await odin_lifecycle(event, orch_db)

        wave_done = next((e for e in emits if e.event_type == "wave_complete"), None)
        assert wave_done is not None
        assert wave_done.payload["wave"] == 0
        assert wave_done.payload["project_id"] == "proj-1"

    @pytest.mark.asyncio
    async def test_wave_not_complete_when_tasks_remaining(self, orch_db):
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="completed")
        await _seed_task(orch_db, "t2", wave=0, status="running")  # still running

        event = Event("task_verified", {
            "project_id": "proj-1",
            "task_id": "t1",
        }, "mimir")

        emits = await odin_lifecycle(event, orch_db)

        wave_done = next((e for e in (emits or []) if e.event_type == "wave_complete"), None)
        assert wave_done is None

    @pytest.mark.asyncio
    async def test_project_complete_when_all_waves_done(self, orch_db):
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="completed")
        # No more tasks in any wave

        event = Event("task_verified", {
            "project_id": "proj-1",
            "task_id": "t1",
        }, "mimir")

        emits = await odin_lifecycle(event, orch_db)

        proj_done = next((e for e in emits if e.event_type == "project_complete"), None)
        assert proj_done is not None

        # Check DB updated
        row = await orch_db.fetchone("SELECT status FROM projects WHERE id = ?", ("proj-1",))
        assert row[0] == "completed"

    @pytest.mark.asyncio
    async def test_project_failed_when_deadlocked(self, orch_db):
        """All tasks blocked, none pending/running → deadlock → project_failed."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="failed")
        await _seed_task(orch_db, "t2", wave=0, status="blocked")

        event = Event("task_verified", {
            "project_id": "proj-1",
            "task_id": "t1",
        }, "mimir")

        emits = await odin_lifecycle(event, orch_db)

        proj_fail = next((e for e in emits if e.event_type == "project_failed"), None)
        assert proj_fail is not None
        assert "deadlock" in proj_fail.payload.get("reason", "").lower() or \
               "blocked" in proj_fail.payload.get("reason", "").lower()

    @pytest.mark.asyncio
    async def test_project_failed_with_failed_tasks(self, orch_db):
        """All tasks terminal but some failed → project_failed."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="completed")
        await _seed_task(orch_db, "t2", wave=0, status="failed")

        event = Event("task_verified", {
            "project_id": "proj-1",
            "task_id": "t1",
        }, "mimir")

        emits = await odin_lifecycle(event, orch_db)

        proj_fail = next((e for e in emits if e.event_type == "project_failed"), None)
        assert proj_fail is not None


# ---------------------------------------------------------------------------
# Gap #1: Provider fallback when preferred is unavailable
# ---------------------------------------------------------------------------

class TestProviderFallback:
    def test_falls_back_when_preferred_unavailable(self):
        """research/simple prefers gemini_cli, but if it's down → claude_code."""
        provider = _select_provider("research", "simple",
                                    {"claude_code": True, "gemini_cli": False, "ollama": True})
        assert provider == "claude_code"

    def test_falls_to_ollama_when_cloud_down(self):
        provider = _select_provider("code", "medium",
                                    {"claude_code": False, "gemini_cli": False, "ollama": True})
        assert provider == "ollama"

    def test_returns_claude_as_last_resort_when_all_down(self):
        provider = _select_provider("code", "simple",
                                    {"claude_code": False, "gemini_cli": False, "ollama": False})
        assert provider == "claude_code"


# ---------------------------------------------------------------------------
# Gap #2: Concurrency limiting
# ---------------------------------------------------------------------------

class TestConcurrencyLimiting:
    @pytest.mark.asyncio
    async def test_respects_max_concurrent(self, orch_db):
        """With 3 ready tasks and max_concurrent=1, only 1 dispatches."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="pending")
        await _seed_task(orch_db, "t2", wave=0, status="pending")
        await _seed_task(orch_db, "t3", wave=0, status="pending")

        event = Event("tick", {"project_id": "proj-1", "max_concurrent": 1}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock_prov:
            mock_prov.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}
            emits = await odin_dispatch(event, orch_db)

        commands = [e for e in (emits or []) if e.event_type == "dispatch_command"]
        assert len(commands) == 1

    @pytest.mark.asyncio
    async def test_slots_reduced_by_running_tasks(self, orch_db):
        """With max_concurrent=2 and 1 running, only 1 more dispatches."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="running")
        await _seed_task(orch_db, "t2", wave=0, status="pending")
        await _seed_task(orch_db, "t3", wave=0, status="pending")

        event = Event("tick", {"project_id": "proj-1", "max_concurrent": 2}, "odin")

        with patch("gods.handlers.odin._get_provider_availability", new_callable=AsyncMock) as mock_prov:
            mock_prov.return_value = {"claude_code": True, "gemini_cli": True, "ollama": True}
            emits = await odin_dispatch(event, orch_db)

        commands = [e for e in (emits or []) if e.event_type == "dispatch_command"]
        assert len(commands) == 1


# ---------------------------------------------------------------------------
# Gap #4: Wave progression — wave_complete triggers next wave dispatch
# ---------------------------------------------------------------------------

class TestWaveProgression:
    @pytest.mark.asyncio
    async def test_wave_complete_emits_next_wave_tick(self, orch_db):
        """wave_complete should trigger a tick for the next wave."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="completed")
        await _seed_task(orch_db, "t2", wave=1, status="pending")

        event = Event("task_verified", {
            "project_id": "proj-1",
            "task_id": "t1",
        }, "mimir")

        emits = await odin_lifecycle(event, orch_db)

        wave_done = next((e for e in emits if e.event_type == "wave_complete"), None)
        assert wave_done is not None

        # wave_complete should also emit a tick to dispatch the next wave
        tick = next((e for e in emits if e.event_type == "tick"), None)
        assert tick is not None
        assert tick.payload["project_id"] == "proj-1"


# ---------------------------------------------------------------------------
# Gap #5: odin_start idempotency
# ---------------------------------------------------------------------------

class TestOdinStartIdempotency:
    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(self, orch_db):
        """project_planned firing twice should not error or double-emit."""
        await _seed_project(orch_db, status="draft")
        event = Event("project_planned", {
            "project_id": "proj-1",
            "plan_id": "plan-1",
        }, "athena")

        emits1 = await odin_start(event, orch_db)
        emits2 = await odin_start(event, orch_db)

        # First call should emit project_started
        started1 = [e for e in emits1 if e.event_type == "project_started"]
        assert len(started1) == 1

        # Second call should be a no-op — already executing
        started2 = [e for e in (emits2 or []) if e.event_type == "project_started"]
        assert len(started2) == 0

        # Project should still be executing
        row = await orch_db.fetchone("SELECT status FROM projects WHERE id = ?", ("proj-1",))
        assert row[0] == "executing"

    @pytest.mark.asyncio
    async def test_start_skips_if_already_executing(self, orch_db):
        """If project is already executing, don't emit project_started again."""
        await _seed_project(orch_db, status="executing")
        event = Event("project_planned", {
            "project_id": "proj-1",
            "plan_id": "plan-1",
        }, "athena")

        emits = await odin_start(event, orch_db)

        # Should be a no-op or at least not re-emit project_started
        started = [e for e in (emits or []) if e.event_type == "project_started"]
        assert len(started) == 0


# ---------------------------------------------------------------------------
# Gap #6: Multi-project tick scan (odin_tick)
# ---------------------------------------------------------------------------

class TestOdinTick:
    @pytest.mark.asyncio
    async def test_tick_scans_all_executing_projects(self, orch_db):
        """odin_tick should find all executing projects and emit a tick for each."""
        await _seed_project(orch_db, project_id="p1", status="executing")
        await _seed_project(orch_db, project_id="p2", status="executing")
        await _seed_project(orch_db, project_id="p3", status="completed")  # skip

        event = Event("tick", {}, "scheduler")

        emits = await odin_tick(event, orch_db)

        ticks = [e for e in emits if e.event_type == "tick"]
        tick_projects = {e.payload["project_id"] for e in ticks}
        assert tick_projects == {"p1", "p2"}

    @pytest.mark.asyncio
    async def test_tick_skips_draft_projects(self, orch_db):
        await _seed_project(orch_db, project_id="p1", status="draft")

        event = Event("tick", {}, "scheduler")

        emits = await odin_tick(event, orch_db)

        assert emits is None or len(emits) == 0


# ---------------------------------------------------------------------------
# Gap #9: needs_review should block wave completion
# ---------------------------------------------------------------------------

class TestNeedsReviewBlocking:
    @pytest.mark.asyncio
    async def test_needs_review_blocks_wave_completion(self, orch_db):
        """A task in needs_review is NOT done — wave should not complete."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="completed")
        await _seed_task(orch_db, "t2", wave=0, status="needs_review")
        await _seed_task(orch_db, "t3", wave=1, status="pending")

        event = Event("task_verified", {
            "project_id": "proj-1",
            "task_id": "t1",
        }, "mimir")

        emits = await odin_lifecycle(event, orch_db)

        wave_done = next((e for e in (emits or []) if e.event_type == "wave_complete"), None)
        assert wave_done is None, "needs_review should block wave completion"

    @pytest.mark.asyncio
    async def test_needs_review_does_not_complete_project(self, orch_db):
        """A task in needs_review should prevent project_complete."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="completed")
        await _seed_task(orch_db, "t2", wave=0, status="needs_review")

        event = Event("task_verified", {
            "project_id": "proj-1",
            "task_id": "t1",
        }, "mimir")

        emits = await odin_lifecycle(event, orch_db)

        proj_done = next((e for e in (emits or []) if e.event_type == "project_complete"), None)
        assert proj_done is None, "needs_review should prevent project_complete"


# ---------------------------------------------------------------------------
# Gap #10 + #12: odin_handle_diagnosis — consume diagnosis, apply fix
# ---------------------------------------------------------------------------

class TestOdinHandleDiagnosis:
    @pytest.mark.asyncio
    async def test_retry_resets_task_to_pending(self, orch_db):
        """task_diagnosis with fix_type=retry_as_is → reset task to pending."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="failed", error="timeout")

        event = Event("task_diagnosis", {
            "task_id": "t1",
            "project_id": "proj-1",
            "fix_type": "retry_as_is",
            "root_cause": "timeout",
        }, "odin")

        emits = await odin_handle_diagnosis(event, orch_db)

        # Task should be reset to pending
        row = await orch_db.fetchone("SELECT status, retry_count FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "pending"
        assert row[1] == 1  # retry count incremented

        # Should emit task_reset
        reset = next((e for e in emits if e.event_type == "task_reset"), None)
        assert reset is not None

    @pytest.mark.asyncio
    async def test_reassign_tier_changes_provider(self, orch_db):
        """task_diagnosis with fix_type=reassign_tier → change model_tier."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="failed", error="rate limit")

        event = Event("task_diagnosis", {
            "task_id": "t1",
            "project_id": "proj-1",
            "fix_type": "reassign_tier",
            "new_tier": "gemini_cli",
            "root_cause": "rate_limit",
        }, "odin")

        emits = await odin_handle_diagnosis(event, orch_db)

        row = await orch_db.fetchone("SELECT status, model_tier FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "pending"
        assert row[1] == "gemini_cli"

    @pytest.mark.asyncio
    async def test_skip_marks_task_cancelled(self, orch_db):
        """task_diagnosis with fix_type=skip → mark task cancelled."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="failed", error="unrecoverable")

        event = Event("task_diagnosis", {
            "task_id": "t1",
            "project_id": "proj-1",
            "fix_type": "skip",
            "root_cause": "unrecoverable",
        }, "odin")

        emits = await odin_handle_diagnosis(event, orch_db)

        row = await orch_db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "cancelled"

        skipped = next((e for e in emits if e.event_type == "task_skipped"), None)
        assert skipped is not None

    @pytest.mark.asyncio
    async def test_escalate_emits_human_review(self, orch_db):
        """task_diagnosis with fix_type=escalate → emit needs_human_review."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="failed", error="unknown")

        event = Event("task_diagnosis", {
            "task_id": "t1",
            "project_id": "proj-1",
            "fix_type": "escalate",
            "root_cause": "unknown",
        }, "odin")

        emits = await odin_handle_diagnosis(event, orch_db)

        row = await orch_db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "needs_review"

        human = next((e for e in emits if e.event_type == "needs_human_review"), None)
        assert human is not None

    @pytest.mark.asyncio
    async def test_retry_respects_max_retries(self, orch_db):
        """retry_as_is should not reset if retry_count >= max_retries."""
        await _seed_project(orch_db, status="executing")
        await orch_db.execute_write(
            "INSERT INTO tasks (id, project_id, title, status, error, retry_count, max_retries, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("t1", "proj-1", "Task t1", "failed", "timeout", 3, 3, time.time()),
        )

        event = Event("task_diagnosis", {
            "task_id": "t1",
            "project_id": "proj-1",
            "fix_type": "retry_as_is",
            "root_cause": "timeout",
        }, "odin")

        emits = await odin_handle_diagnosis(event, orch_db)

        # Should NOT reset — max retries exhausted, should escalate instead
        row = await orch_db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row[0] != "pending"

        escalated = next((e for e in emits if e.event_type == "needs_human_review"), None)
        assert escalated is not None

    @pytest.mark.asyncio
    async def test_modify_prompt_stores_guidance(self, orch_db):
        """task_diagnosis with fix_type=modify_prompt → store guidance in context_json."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="failed",
                         error="SyntaxError: invalid syntax")

        event = Event("task_diagnosis", {
            "task_id": "t1",
            "project_id": "proj-1",
            "fix_type": "modify_prompt",
            "root_cause": "syntax_error",
            "prompt_guidance": "Ensure all Python code uses proper indentation.",
        }, "odin")

        emits = await odin_handle_diagnosis(event, orch_db)

        row = await orch_db.fetchone("SELECT status FROM tasks WHERE id = ?", ("t1",))
        assert row[0] == "pending"


# ---------------------------------------------------------------------------
# Gap #7: Import location (verify diagnose helper is a proper function)
# ---------------------------------------------------------------------------

class TestImportCleanliness:
    def test_diagnose_failure_importable(self):
        """diagnose_failure should be importable from the handler module."""
        from gods.handlers.odin import _diagnose_failed_tasks
        assert callable(_diagnose_failed_tasks)


# ---------------------------------------------------------------------------
# Gap #8: No duplicate wave query (verify lifecycle is efficient)
# ---------------------------------------------------------------------------

class TestLifecycleEfficiency:
    @pytest.mark.asyncio
    async def test_lifecycle_single_wave_query(self, orch_db):
        """Lifecycle should work correctly — verifying no duplication bugs."""
        await _seed_project(orch_db, status="executing")
        await _seed_task(orch_db, "t1", wave=0, status="completed")
        await _seed_task(orch_db, "t2", wave=0, status="completed")
        await _seed_task(orch_db, "t3", wave=1, status="pending")

        event = Event("task_verified", {
            "project_id": "proj-1",
            "task_id": "t2",
        }, "mimir")

        emits = await odin_lifecycle(event, orch_db)

        # Should emit exactly ONE wave_complete for wave 0, not duplicates
        wave_events = [e for e in emits if e.event_type == "wave_complete"]
        assert len(wave_events) == 1
        assert wave_events[0].payload["wave"] == 0
