"""Reliability tests — production failure modes and recovery mechanisms.

Six fixes, one test class each. All tests written BEFORE implementation.
Run with:
  cd Odin && pytest tests/test_reliability.py -v

Fix 1: TestStuckTaskRecovery     — odin_tick resets stuck running/planning
Fix 2: TestMimirDeferredReplay   — deferred queue survives service restart
Fix 3: TestMimirFailClosed       — already-done and LLM-fail → needs_review
Fix 4: TestHermesConcurrencyLock — concurrent dispatch can't exceed max
Fix 5: TestRelayWriteFailure     — strict relay fail → task marked failed
Fix 6: TestStartupRecovery       — startup pass re-triggers stuck projects
"""

from __future__ import annotations

import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from gods.pipeline import Event, Emit


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class FakeDB:
    def __init__(self):
        self._rows: dict = {}
        self.writes: list = []

    async def fetchone(self, sql, params=()):
        key = (sql.strip(), params)
        return self._rows.get(key)

    async def fetchall(self, sql, params=()):
        key = (sql.strip(), params)
        return self._rows.get(key) or []

    async def execute_write(self, sql, params=()):
        self.writes.append((sql.strip()[:120], params))

    def get_writes_for(self, fragment: str) -> list:
        return [(s, p) for s, p in self.writes if fragment in s]

    def set(self, sql: str, params, value):
        self._rows[(sql.strip(), params)] = value


def make_event(event_type: str, payload: dict) -> Event:
    return Event(event_type, payload, source="test")


# ===========================================================================
# Fix 1 — Stuck task recovery in odin_tick
# ===========================================================================

class TestStuckTaskRecovery:
    """
    odin_tick should detect and reset tasks/projects stuck in intermediate states.

    Currently odin_tick only emits project_tick for executing projects.
    It needs to also:
      - Find tasks in 'running' with updated_at older than STUCK_TASK_THRESHOLD (600s)
        → reset to 'pending', emit task_stuck_reset
      - Find projects in 'planning' with updated_at older than STUCK_PLANNING_THRESHOLD (300s)
        → reset to 'draft', emit project_stuck_reset

    Without this: a crashed hermes background task leaves the task in 'running'
    forever. No human intervention, no automatic recovery.
    """

    @pytest.mark.asyncio
    async def test_tick_resets_stuck_running_task(self):
        """Task stuck in 'running' for > 600s should be reset to 'pending'."""
        from gods.handlers.odin import odin_tick

        db = FakeDB()
        # No executing projects (so no project_tick noise)
        db.set("SELECT id FROM projects WHERE status = $1", ("executing",), [])
        # One task stuck in running for 700s
        stuck_task = {
            "id": "task1",
            "project_id": "proj1",
            "status": "running",
            "updated_at": time.time() - 700,
        }
        # Override fetchall to return stuck task for the right query
        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "tasks" in sql and "running" in str(params):
                return [stuck_task]
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        event = make_event("odin_tick", {})
        result = await odin_tick(event, db)

        # Should have reset the task
        update_writes = db.get_writes_for("UPDATE tasks")
        assert any("pending" in str(p) and "task1" in str(p) for _, p in update_writes), \
            "Stuck 'running' task should be reset to 'pending'"

        # Should emit task_stuck_reset
        emits = result or []
        reset_emits = [e for e in emits if e.event_type == "task_stuck_reset"]
        assert len(reset_emits) >= 1, "Should emit task_stuck_reset for each stuck task"
        assert reset_emits[0].payload["task_id"] == "task1"

    @pytest.mark.asyncio
    async def test_tick_resets_stuck_planning_project(self):
        """Project stuck in 'planning' for > 300s should be reset to 'draft'."""
        from gods.handlers.odin import odin_tick

        db = FakeDB()
        db.set("SELECT id FROM projects WHERE status = $1", ("executing",), [])

        stuck_project = {
            "id": "proj1",
            "status": "planning",
            "updated_at": time.time() - 400,
        }

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "projects" in sql and "planning" in str(params):
                return [stuck_project]
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        event = make_event("odin_tick", {})
        result = await odin_tick(event, db)

        update_writes = db.get_writes_for("UPDATE projects")
        assert any("draft" in str(p) and "proj1" in str(p) for _, p in update_writes), \
            "Stuck 'planning' project should be reset to 'draft'"

        emits = result or []
        reset_emits = [e for e in emits if e.event_type == "project_stuck_reset"]
        assert len(reset_emits) >= 1
        assert reset_emits[0].payload["project_id"] == "proj1"

    @pytest.mark.asyncio
    async def test_tick_does_not_reset_recently_updated_running_task(self):
        """Task running for only 60s should NOT be reset — it's still active."""
        from gods.handlers.odin import odin_tick

        db = FakeDB()
        db.set("SELECT id FROM projects WHERE status = $1", ("executing",), [])

        recent_task = {
            "id": "task1",
            "project_id": "proj1",
            "status": "running",
            "updated_at": time.time() - 60,  # only 60s, not stuck
        }

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "tasks" in sql and "running" in str(params):
                # Return empty — recent task not in the stuck query (threshold filters it out)
                return []
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        event = make_event("odin_tick", {})
        await odin_tick(event, db)

        update_writes = db.get_writes_for("UPDATE tasks")
        assert not any("pending" in str(p) and "task1" in str(p) for _, p in update_writes), \
            "Recently running task should NOT be reset"

    @pytest.mark.asyncio
    async def test_tick_handles_both_project_tick_and_stuck_recovery(self):
        """odin_tick should emit project_ticks AND task_stuck_resets in same call."""
        from gods.handlers.odin import odin_tick

        db = FakeDB()
        db.set("SELECT id FROM projects WHERE status = $1", ("executing",), [{"id": "proj1"}])

        stuck_task = {
            "id": "taskX",
            "project_id": "proj2",
            "status": "running",
            "updated_at": time.time() - 800,
        }

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "projects" in sql and "executing" in str(params):
                return [{"id": "proj1"}]
            if "tasks" in sql and "running" in str(params):
                return [stuck_task]
            if "projects" in sql and "planning" in str(params):
                return []
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        event = make_event("odin_tick", {})
        result = await odin_tick(event, db)

        emits = result or []
        project_ticks = [e for e in emits if e.event_type == "project_tick"]
        stuck_resets = [e for e in emits if e.event_type == "task_stuck_reset"]

        assert len(project_ticks) >= 1, "Should still emit project_tick for executing projects"
        assert len(stuck_resets) >= 1, "Should also emit task_stuck_reset for stuck tasks"

    @pytest.mark.asyncio
    async def test_tick_resets_multiple_stuck_tasks(self):
        """All stuck tasks should be reset in a single tick — not just the first."""
        from gods.handlers.odin import odin_tick

        db = FakeDB()
        db.set("SELECT id FROM projects WHERE status = $1", ("executing",), [])

        stuck_tasks = [
            {"id": "t1", "project_id": "p1", "status": "running", "updated_at": time.time() - 700},
            {"id": "t2", "project_id": "p1", "status": "running", "updated_at": time.time() - 900},
            {"id": "t3", "project_id": "p2", "status": "running", "updated_at": time.time() - 1200},
        ]

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "tasks" in sql and "running" in str(params):
                return stuck_tasks
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        event = make_event("odin_tick", {})
        result = await odin_tick(event, db)

        emits = result or []
        reset_emits = [e for e in emits if e.event_type == "task_stuck_reset"]
        assert len(reset_emits) == 3, f"Expected 3 resets, got {len(reset_emits)}"

        reset_ids = {e.payload["task_id"] for e in reset_emits}
        assert reset_ids == {"t1", "t2", "t3"}


# ===========================================================================
# Fix 2 — Mimir deferred queue survives restart
# ===========================================================================

class TestMimirDeferredReplay:
    """
    When Mimir's verification slots are full, tasks go into _deferred (in-memory).
    On service restart, that queue is gone — those tasks are never verified.

    Fix: on startup, Mimir queries relay for unprocessed verification_deferred events
    (those with no corresponding verification_dequeued event) and replays them.

    The verification_deferred event is already written to the relay (line 414 in mimir.py).
    We just need to read it back on startup.
    """

    @pytest.mark.asyncio
    async def test_startup_replays_unprocessed_deferred_events(self):
        """On startup, Mimir should find deferred events in relay and queue them."""
        from gods.handlers.mimir import MimirRunner

        db = FakeDB()

        # Simulate relay has a verification_deferred event that was never dequeued
        deferred_events = [
            {
                "id": 42,
                "event_type": "verification_deferred",
                "payload": json.dumps({
                    "task_id": "task1",
                    "project_id": "proj1",
                    "in_flight": 4,
                    "queued": 0,
                    "max_concurrent": 4,
                }),
                "created_at": time.time() - 120,
            }
        ]

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "god_relay_events" in sql and "verification_deferred" in str(params):
                return deferred_events
            if "god_relay_events" in sql and "verification_dequeued" in str(params):
                return []
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        mimir = MimirRunner(db=db, max_concurrent=4)
        await mimir.replay_deferred_from_relay()

        # Mimir's internal deferred queue should now contain task1
        deferred_ids = [e.payload.get("task_id") for e in mimir._deferred]
        assert "task1" in deferred_ids, \
            "Mimir should have replayed task1 from the relay into _deferred"

    @pytest.mark.asyncio
    async def test_startup_skips_already_processed_deferred_events(self):
        """Deferred events that were already dequeued should not be replayed."""
        from gods.handlers.mimir import MimirRunner

        db = FakeDB()

        # task1 was deferred then verified — has a verification_dequeued marker
        deferred_events = [
            {
                "id": 10,
                "event_type": "verification_deferred",
                "payload": json.dumps({"task_id": "task1", "project_id": "proj1"}),
                "created_at": time.time() - 200,
            }
        ]
        # task1 has already been processed
        processed_ids = {"task1"}

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "god_relay_events" in sql and "verification_deferred" in str(params):
                return deferred_events
            if "god_relay_events" in sql and "verification_dequeued" in str(params):
                return [{"payload": json.dumps({"task_id": "task1"})}]
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        mimir = MimirRunner(db=db, max_concurrent=4)
        await mimir.replay_deferred_from_relay()

        deferred_ids = [e.payload.get("task_id") for e in mimir._deferred]
        assert "task1" not in deferred_ids, \
            "Already-processed deferred tasks should not be re-queued"

    @pytest.mark.asyncio
    async def test_dequeuing_writes_verification_dequeued_event(self):
        """When a deferred task is picked up, write verification_dequeued to relay."""
        from gods.handlers.mimir import MimirRunner

        db = FakeDB()
        # Task row needed for verification
        db.set(
            "SELECT title, description, output_text, retry_count, max_retries, context_json, "
            "verification_status FROM tasks WHERE id = $1",
            ("task1",),
            {
                "title": "Test task",
                "description": "desc",
                "output_text": "Here is the implementation with tests",
                "retry_count": 0,
                "max_retries": 3,
                "context_json": "{}",
                "verification_status": None,
            },
        )

        mimir = MimirRunner(db=db, max_concurrent=4)

        # Manually add a deferred event
        deferred_event = make_event("worker_event", {
            "task_id": "task1",
            "project_id": "proj1",
            "status": "completed",
        })
        mimir._deferred.append(deferred_event)

        # Drain — should pick up the deferred task and write dequeued marker
        with patch.object(mimir, "_verify_task", new=AsyncMock()):
            await mimir._drain_deferred()

        relay_writes = db.get_writes_for("INSERT INTO god_relay_events")
        assert any("verification_dequeued" in str(p) for _, p in relay_writes), \
            "Should write verification_dequeued to relay when picking up a deferred task"

    @pytest.mark.asyncio
    async def test_startup_replay_is_idempotent(self):
        """Calling replay_deferred_from_relay twice should not double-queue tasks."""
        from gods.handlers.mimir import MimirRunner

        db = FakeDB()

        deferred_events = [
            {
                "id": 5,
                "event_type": "verification_deferred",
                "payload": json.dumps({"task_id": "taskA", "project_id": "proj1"}),
                "created_at": time.time() - 60,
            }
        ]

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "god_relay_events" in sql and "verification_deferred" in str(params):
                return deferred_events
            if "god_relay_events" in sql and "verification_dequeued" in str(params):
                return []
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        mimir = MimirRunner(db=db, max_concurrent=4)
        await mimir.replay_deferred_from_relay()
        await mimir.replay_deferred_from_relay()  # second call

        deferred_ids = [e.payload.get("task_id") for e in mimir._deferred]
        assert deferred_ids.count("taskA") == 1, \
            "Idempotent: taskA should appear exactly once in deferred queue"


# ===========================================================================
# Fix 3 — Mimir fail-closed
# ===========================================================================

class TestMimirFailClosed:
    """
    Two auto-pass paths that should route to needs_review instead:

    1. "Already done" heuristic: output contains "already exists" etc.
       Current behavior: pass with confidence=0.8
       Correct behavior: needs_review — the heuristic can't tell if this is
         legitimate ("file already migrated") or a bug ("migration failed: already exists")

    2. LLM verification failure with output > 20 chars:
       Current behavior: pass with confidence=0.5
       Correct behavior: needs_review with confidence=0.3
         The verifier being unavailable is not evidence the task succeeded.
    """

    @pytest.mark.asyncio
    async def test_already_done_phrase_routes_to_needs_review(self):
        """Output containing 'already exists' should go to needs_review, not auto-pass."""
        from gods.handlers.mimir import MimirRunner

        db = FakeDB()
        db.set(
            "SELECT title, description, output_text, retry_count, max_retries, context_json, "
            "verification_status FROM tasks WHERE id = $1",
            ("task1",),
            {
                "title": "Add migration",
                "description": "Create migration for users table",
                "output_text": "Migration file already exists in versions/",
                "retry_count": 0,
                "max_retries": 3,
                "context_json": "{}",
                "verification_status": None,
            },
        )

        mimir = MimirRunner(db=db, max_concurrent=4)
        event = make_event("worker_event", {
            "task_id": "task1",
            "project_id": "proj1",
            "status": "completed",
        })
        result = await mimir.handle_verify(event, db)

        # Should NOT emit task_verified
        emits = result or []
        verified_emits = [e for e in emits if e.event_type == "task_verified"]
        assert len(verified_emits) == 0, \
            "'Already done' output should NOT auto-pass — it could be a task failure"

        # Should set needs_review in DB
        update_writes = db.get_writes_for("UPDATE tasks")
        assert any("needs_review" in str(p) for _, p in update_writes), \
            "'Already done' output should route task to needs_review status"

    @pytest.mark.asyncio
    async def test_already_done_emits_needs_human_review(self):
        """'Already done' path should emit needs_human_review relay event."""
        from gods.handlers.mimir import MimirRunner

        db = FakeDB()
        db.set(
            "SELECT title, description, output_text, retry_count, max_retries, context_json, "
            "verification_status FROM tasks WHERE id = $1",
            ("task1",),
            {
                "title": "Install dependency",
                "description": "pip install httpx",
                "output_text": "httpx is already installed and in requirements",
                "retry_count": 0,
                "max_retries": 3,
                "context_json": "{}",
                "verification_status": None,
            },
        )

        mimir = MimirRunner(db=db, max_concurrent=4)
        event = make_event("worker_event", {
            "task_id": "task1",
            "project_id": "proj1",
            "status": "completed",
        })
        await mimir.handle_verify(event, db)

        relay_writes = db.get_writes_for("INSERT INTO god_relay_events")
        assert any("needs_human_review" in str(p) for _, p in relay_writes), \
            "Should emit needs_human_review event for 'already done' tasks"

    @pytest.mark.asyncio
    async def test_verifier_failure_with_output_routes_to_needs_review(self):
        """LLM verifier failure → needs_review in DB, NOT auto-pass.

        Tests _verify_task directly (the background coroutine) to avoid
        waiting on asyncio background tasks in the test.
        """
        from gods.handlers.mimir import MimirRunner

        db = FakeDB()
        mimir = MimirRunner(db=db, max_concurrent=4)

        with patch("gods.handlers.mimir._call_verifier", side_effect=ConnectionError("Gateway down")):
            await mimir._verify_task(
                task_id="task1",
                project_id="proj1",
                title="Implement auth",
                description="Add JWT",
                output_text="Here is the JWT implementation with full test coverage and error handling",
                retry_count=0,
                max_retries=3,
            )

        update_writes = db.get_writes_for("UPDATE tasks")
        assert any("needs_review" in str(p) for _, p in update_writes), \
            "Verifier failure should set task to needs_review"
        # Must NOT have auto-passed
        passed_writes = [p for _, p in update_writes if "completed" in str(p) or "passed" in str(p)]
        assert len(passed_writes) == 0, \
            "Verifier failure should NOT auto-pass the task"

    @pytest.mark.asyncio
    async def test_verifier_failure_confidence_is_low(self):
        """When verifier fails, relay event must have confidence <= 0.3, NOT 0.5."""
        from gods.handlers.mimir import MimirRunner

        db = FakeDB()
        mimir = MimirRunner(db=db, max_concurrent=4)

        with patch("gods.handlers.mimir._call_verifier", side_effect=ConnectionError("Gateway down")):
            await mimir._verify_task(
                task_id="task1",
                project_id="proj1",
                title="Some task",
                description="do something",
                output_text="Done! All tests pass and the implementation is complete.",
                retry_count=0,
                max_retries=3,
            )

        relay_writes = db.get_writes_for("INSERT INTO god_relay_events")
        # Must NOT have task_verified with confidence=0.5
        bad_writes = [p for _, p in relay_writes
                     if "task_verified" in str(p) and "0.5" in str(p)]
        assert len(bad_writes) == 0, \
            "Verifier failure must not emit task_verified with confidence=0.5 (fail-open)"
        # Must have needs_human_review
        assert any("needs_human_review" in str(p) for _, p in relay_writes), \
            "Verifier failure must emit needs_human_review to relay"


# ===========================================================================
# Fix 4 — Hermes concurrency lock
# ===========================================================================

class TestHermesConcurrencyLock:
    """
    Hermes checks `len(self._tasks) >= max_concurrent` before launching.
    If two dispatch_command events are processed concurrently (both see len==3,
    max==4), both launch — exceeding the limit.

    Fix: asyncio.Lock around the check-and-add operation so only one dispatch
    proceeds at a time.
    """

    @pytest.mark.asyncio
    async def test_concurrent_dispatch_does_not_exceed_max(self):
        """Two simultaneous dispatch_command events must not both launch when at limit."""
        from gods.handlers.hermes_async import HermesRunner

        db = FakeDB()
        db.set(
            "SELECT id, title, description, task_type, status, model_tier, context_json "
            "FROM tasks WHERE id = $1",
            ("task1",),
            {"id": "task1", "title": "T1", "description": "", "task_type": "code",
             "status": "pending", "model_tier": "claude_code", "context_json": "{}"},
        )
        db.set(
            "SELECT id, title, description, task_type, status, model_tier, context_json "
            "FROM tasks WHERE id = $1",
            ("task2",),
            {"id": "task2", "title": "T2", "description": "", "task_type": "code",
             "status": "pending", "model_tier": "claude_code", "context_json": "{}"},
        )
        db.set("SELECT repo_path FROM projects WHERE id = $1", ("proj1",), {"repo_path": "."})

        hermes = HermesRunner(db=db, max_concurrent=1)

        # Fill the slot with a fake task
        fake_task = asyncio.create_task(asyncio.sleep(10), name="fake-occupant")
        hermes._tasks["occupant"] = fake_task

        event1 = make_event("dispatch_command", {"task_id": "task1", "project_id": "proj1", "provider": "claude_code"})
        event2 = make_event("dispatch_command", {"task_id": "task2", "project_id": "proj1", "provider": "claude_code"})

        # Run both concurrently
        results = await asyncio.gather(
            hermes.handle_dispatch(event1, db),
            hermes.handle_dispatch(event2, db),
        )

        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass

        # Both should have gotten slots_full — max_concurrent is 1 and it's occupied
        all_emits = [e for r in results if r for e in r]
        slots_full = [e for e in all_emits if e.event_type == "slots_full"]
        launched = [e for e in all_emits if e.event_type == "task_running"]

        # Neither should have launched (slot was full)
        assert len(launched) == 0, \
            f"No tasks should launch when all slots full, but got {len(launched)} task_running emits"

    @pytest.mark.asyncio
    async def test_concurrent_dispatch_with_one_slot_only_one_launches(self):
        """With one free slot and two concurrent dispatches, exactly one should launch."""
        from gods.handlers.hermes_async import HermesRunner

        db = FakeDB()
        for task_id in ("taskA", "taskB"):
            db.set(
                "SELECT id, title, description, task_type, status, model_tier, context_json "
                "FROM tasks WHERE id = $1",
                (task_id,),
                {"id": task_id, "title": task_id, "description": "", "task_type": "code",
                 "status": "pending", "model_tier": "claude_code", "context_json": "{}"},
            )
        db.set("SELECT repo_path FROM projects WHERE id = $1", ("proj1",), {"repo_path": "."})

        hermes = HermesRunner(db=db, max_concurrent=1)
        # Slots are completely empty

        launched_count = 0
        original_monitor = hermes._monitor_task

        async def mock_monitor(**kwargs):
            nonlocal launched_count
            launched_count += 1
            await asyncio.sleep(0)  # yield but don't actually run

        hermes._monitor_task = mock_monitor

        event_a = make_event("dispatch_command", {"task_id": "taskA", "project_id": "proj1", "provider": "claude_code"})
        event_b = make_event("dispatch_command", {"task_id": "taskB", "project_id": "proj1", "provider": "claude_code"})

        results = await asyncio.gather(
            hermes.handle_dispatch(event_a, db),
            hermes.handle_dispatch(event_b, db),
        )

        all_emits = [e for r in results if r for e in r]
        launched = [e for e in all_emits if e.event_type == "task_running"]
        slots_full = [e for e in all_emits if e.event_type == "slots_full"]

        assert len(launched) == 1, \
            f"Exactly one task should launch with one free slot, got {len(launched)}"
        assert len(slots_full) == 1, \
            f"The other task should get slots_full, got {len(slots_full)}"

    @pytest.mark.asyncio
    async def test_dispatch_lock_is_an_asyncio_lock(self):
        """HermesRunner must have a _dispatch_lock attribute that is an asyncio.Lock."""
        from gods.handlers.hermes_async import HermesRunner

        db = FakeDB()
        hermes = HermesRunner(db=db, max_concurrent=4)

        assert hasattr(hermes, "_dispatch_lock"), \
            "HermesRunner must have a _dispatch_lock for concurrency safety"
        assert isinstance(hermes._dispatch_lock, asyncio.Lock), \
            "_dispatch_lock must be an asyncio.Lock instance"


# ===========================================================================
# Fix 5 — Relay write failure → task marked failed
# ===========================================================================

class TestRelayWriteFailure:
    """
    _write_relay_event currently swallows all exceptions.
    This means a task can complete execution but its completion event is never
    written — Mimir never sees it, project stalls silently.

    Note: _write_relay_event_strict already raises (used for worker_event completion).
    The issue is _write_relay_event (best-effort) used for narration/heartbeat events
    hiding errors, and narration failures masking infrastructure problems.

    Broader fix: _write_relay_event should log AND increment a failure counter.
    After N consecutive relay failures, switch to degraded mode and log a critical alert.
    This surfaces infrastructure problems before they cause silent data loss.
    """

    @pytest.mark.asyncio
    async def test_relay_write_strict_failure_leaves_task_running(self):
        """If _write_relay_event_strict raises, task stays 'running' (not 'completed').

        This is the CORRECT behavior — relay write must succeed before DB update.
        We're testing that the existing split-brain protection still works.
        """
        from gods.handlers.hermes_async import HermesRunner

        db = FakeDB()
        db.set("SELECT repo_path FROM projects WHERE id = $1", ("proj1",), {"repo_path": "."})

        write_calls = []
        async def fail_on_relay(sql, params=()):
            write_calls.append((sql, params))
            if "god_relay_events" in sql:
                raise RuntimeError("DB write failed")
            db.writes.append((sql.strip()[:120], params))

        db.execute_write = fail_on_relay

        hermes = HermesRunner(db=db, max_concurrent=4)

        # Simulate completed execution result
        async def mock_run_cli(**kwargs):
            return {
                "output": "Implementation complete with tests",
                "cost_usd": 0.01,
                "prompt_tokens": 100,
                "completion_tokens": 200,
                "model_used": "claude-sonnet-4-6",
                "narration": [],
            }

        hermes._run_cli_background = mock_run_cli

        # Run monitor directly (bypass handle_dispatch)
        try:
            await hermes._monitor_task(
                task_id="task1",
                project_id="proj1",
                provider="claude_code",
                prompt="test",
                cwd=".",
                task_type="code",
            )
        except Exception:
            pass  # Expected — relay write failed

        # Task DB update should NOT have happened (relay write failed first)
        update_writes = [s for s, p in write_calls
                        if "UPDATE tasks SET status" in s and "completed" in str(p)]
        assert len(update_writes) == 0, \
            "Task should NOT be marked 'completed' in DB if relay write failed"

    @pytest.mark.asyncio
    async def test_consecutive_relay_failures_trigger_alert(self):
        """After N consecutive _write_relay_event failures, a critical alert should be logged."""
        from gods.handlers.hermes_async import HermesRunner

        db = FakeDB()

        async def always_fail(sql, params=()):
            if "god_relay_events" in sql:
                raise RuntimeError("Relay unavailable")

        db.execute_write = always_fail

        hermes = HermesRunner(db=db, max_concurrent=4)

        import logging
        with patch.object(logging.getLogger("gods.handlers.hermes"), "critical") as mock_critical:
            # Write 5 consecutive failures
            for _ in range(5):
                await hermes._write_relay_event("narration", {"task_id": "t1"})

            # After threshold, critical should have been logged at least once
            # (threshold is implementation-defined, but must be <= 5)
            assert mock_critical.called or hermes._relay_failure_count >= 3, \
                "Consecutive relay write failures should be tracked and escalated"


# ===========================================================================
# Fix 6 — Startup recovery pass
# ===========================================================================

class TestStartupRecovery:
    """
    On service restart, the cursor is restored to max(persisted, max_id - 50).
    Any project whose project_created event is > 50 events back is silently skipped.
    Those projects stay in 'draft' forever.

    Fix: after cursor restore, run a startup recovery pass that:
    1. Finds projects in 'draft' with no recent activity → re-emits project_created
    2. Finds projects in 'planning' stuck > threshold → resets to 'draft'

    The recovery pass should be callable from odin_tick with a special flag,
    OR as a dedicated startup function called from the pipeline on first tick.
    """

    @pytest.mark.asyncio
    async def test_recovery_writes_project_created_to_relay(self):
        """Draft projects with no relay activity get project_created written to relay."""
        from gods.handlers.odin import run_startup_recovery

        db = FakeDB()

        draft_projects = [
            {"id": "proj1", "status": "draft", "updated_at": time.time() - 400},
            {"id": "proj2", "status": "draft", "updated_at": time.time() - 600},
        ]

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "draft" in str(params):
                return draft_projects
            if "planning" in str(params):
                return []
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        original_fetchone = db.fetchone
        async def smart_fetchone(sql, params=()):
            if "god_relay_events" in sql:
                return None  # no recent events
            return await original_fetchone(sql, params)
        db.fetchone = smart_fetchone

        await run_startup_recovery(db)

        relay_writes = db.get_writes_for("INSERT INTO god_relay_events")
        created = [p for _, p in relay_writes if "project_created" in str(p)]
        assert len(created) >= 2, "Should write project_created to relay for each stuck draft"
        written_ids = {json.loads(p[2])["project_id"] for p in created}
        assert {"proj1", "proj2"} == written_ids

    @pytest.mark.asyncio
    async def test_recovery_resets_stuck_planning_projects(self):
        """Projects stuck in 'planning' for > threshold should be reset to 'draft'."""
        from gods.handlers.odin import run_startup_recovery

        db = FakeDB()

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "draft" in str(params):
                return []
            if "planning" in str(params):
                return [{"id": "projX", "status": "planning", "updated_at": time.time() - 500}]
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        await run_startup_recovery(db)

        update_writes = db.get_writes_for("UPDATE projects")
        assert any("draft" in str(p) and "projX" in str(p) for _, p in update_writes), \
            "Stuck 'planning' project should be reset to 'draft' on startup"

        relay_writes = db.get_writes_for("INSERT INTO god_relay_events")
        assert any("project_stuck_reset" in str(p) for _, p in relay_writes), \
            "Should write project_stuck_reset to relay"

    @pytest.mark.asyncio
    async def test_recovery_skips_active_draft_projects(self):
        """Draft projects with recent relay activity should NOT be re-triggered."""
        from gods.handlers.odin import run_startup_recovery

        db = FakeDB()

        draft_projects = [
            {"id": "proj1", "status": "draft", "updated_at": time.time() - 30},
        ]

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "draft" in str(params):
                return draft_projects
            if "planning" in str(params):
                return []
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        original_fetchone = db.fetchone
        async def smart_fetchone(sql, params=()):
            if "god_relay_events" in sql and "proj1" in str(params):
                return {"id": 99}  # recent activity exists
            return await original_fetchone(sql, params)
        db.fetchone = smart_fetchone

        await run_startup_recovery(db)

        relay_writes = db.get_writes_for("INSERT INTO god_relay_events")
        created = [p for _, p in relay_writes if "project_created" in str(p)
                   and "proj1" in str(p)]
        assert len(created) == 0, \
            "Active draft projects (recent relay events) should not be re-triggered"

    @pytest.mark.asyncio
    async def test_recovery_is_safe_to_call_multiple_times(self):
        """run_startup_recovery called twice should not double-write for same project."""
        from gods.handlers.odin import run_startup_recovery

        db = FakeDB()
        call_count = [0]

        draft_projects = [
            {"id": "proj1", "status": "draft", "updated_at": time.time() - 500},
        ]

        original_fetchall = db.fetchall
        async def smart_fetchall(sql, params=()):
            if "draft" in str(params):
                return draft_projects if call_count[0] == 0 else []
            if "planning" in str(params):
                return []
            return await original_fetchall(sql, params)
        db.fetchall = smart_fetchall

        original_fetchone = db.fetchone
        async def smart_fetchone(sql, params=()):
            if "god_relay_events" in sql:
                # Second call: relay already has a project_created for proj1
                if call_count[0] > 0 and "proj1" in str(params):
                    return {"id": 1}  # simulate existing relay event
                return None
            return await original_fetchone(sql, params)
        db.fetchone = smart_fetchone

        call_count[0] = 0
        await run_startup_recovery(db)
        call_count[0] = 1
        await run_startup_recovery(db)

        relay_writes = db.get_writes_for("INSERT INTO god_relay_events")
        created = [p for _, p in relay_writes if "project_created" in str(p)]
        assert len(created) == 1, \
            "Second recovery call should not re-write project_created (relay already has it)"
