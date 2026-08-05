"""Tests for gate wiring — verify gates block bad output and pass good output.

Tests the Phase 0 gate fixes and Phase 1 wiring from the gap resolution plan.
"""

import json
import time

import pytest
import pytest_asyncio
import aiosqlite

from conftest import SqliteDB
from gods.pipeline import Pipeline, Event, Emit, GateResult
from gods.gates import (
    check_plan_created,
    check_plan_reviewed,
    check_files_staged,
    check_pr_created,
    compose_gates,
)


@pytest_asyncio.fixture
async def full_db():
    """SQLite DB with god tables + projects/plans/tasks for gate tests."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE god_relay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            source TEXT NOT NULL,
            payload TEXT,
            severity TEXT DEFAULT 'info',
            idempotency_key TEXT UNIQUE,
            created_at REAL NOT NULL
        );
        CREATE INDEX idx_relay_type_created
            ON god_relay_events (event_type, created_at);

        CREATE TABLE god_registry (
            name TEXT PRIMARY KEY,
            port INTEGER,
            status TEXT DEFAULT 'unknown',
            last_heartbeat REAL,
            config TEXT,
            updated_at REAL
        );

        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            requirements TEXT,
            status TEXT DEFAULT 'draft',
            repo_path TEXT,
            config_json TEXT DEFAULT '{}',
            created_at REAL,
            updated_at REAL,
            completed_at REAL
        );

        CREATE TABLE plans (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            version INTEGER DEFAULT 1,
            model_used TEXT,
            plan_json TEXT,
            status TEXT DEFAULT 'draft',
            created_at REAL
        );

        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            plan_id TEXT,
            title TEXT,
            description TEXT,
            task_type TEXT DEFAULT 'code',
            status TEXT DEFAULT 'pending',
            wave INTEGER DEFAULT 0,
            model_tier TEXT DEFAULT 'claude_code',
            context_json TEXT DEFAULT '{}',
            output_text TEXT,
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 3,
            error TEXT,
            started_at REAL,
            completed_at REAL,
            updated_at REAL
        );
    """)
    await conn.commit()
    db = SqliteDB(conn)
    yield db
    await conn.close()


class TestCheckPlanCreated:
    @pytest.mark.asyncio
    async def test_blocks_when_no_plan_generated_emit(self, full_db):
        """Gate should fail if handler doesn't emit plan_generated."""
        event = Event("project_created", {"project_id": "p1"}, "api")
        emits = [Emit("narration", {"msg": "thinking..."}, "athena")]

        result = await check_plan_created(event, emits, full_db)
        assert not result.passed
        assert "No plan_generated" in result.reason

    @pytest.mark.asyncio
    async def test_blocks_when_plan_not_in_db(self, full_db):
        """Gate should fail if plan_generated references a plan not in DB."""
        event = Event("project_created", {"project_id": "p1"}, "api")
        emits = [Emit("plan_generated", {"plan_id": "nonexistent"}, "athena")]

        result = await check_plan_created(event, emits, full_db)
        assert not result.passed
        assert "not found" in result.reason

    @pytest.mark.asyncio
    async def test_passes_valid_plan(self, full_db):
        """Gate should pass when plan exists in DB with tasks."""
        plan_json = json.dumps({"tasks": [{"id": "t1", "title": "Do thing"}]})
        await full_db.execute_write(
            "INSERT INTO plans (id, project_id, plan_json, created_at) VALUES (?, ?, ?, ?)",
            ("plan-1", "p1", plan_json, time.time()),
        )

        event = Event("project_created", {"project_id": "p1"}, "api")
        emits = [Emit("plan_generated", {"plan_id": "plan-1"}, "athena")]

        result = await check_plan_created(event, emits, full_db)
        assert result.passed


class TestCheckPlanReviewed:
    @pytest.mark.asyncio
    async def test_passes_when_review_skipped(self, full_db):
        """Gate should pass when review was explicitly skipped."""
        event = Event("project_planned", {"project_id": "p1"}, "athena")
        emits = [Emit("project_planned", {
            "plan_id": "plan-1",
            "review_skipped": True,
        }, "athena")]

        result = await check_plan_reviewed(event, emits, full_db)
        assert result.passed
        assert "skipped" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_blocks_low_confidence(self, full_db):
        """Gate should block plans with gaps and low confidence."""
        event = Event("project_planned", {"project_id": "p1"}, "athena")
        emits = [Emit("project_planned", {
            "plan_id": "plan-1",
            "review": {"has_gaps": True, "confidence": 0.3, "gaps": ["missing tests"]},
        }, "athena")]

        result = await check_plan_reviewed(event, emits, full_db)
        assert not result.passed
        assert "confidence" in result.reason.lower()

    @pytest.mark.asyncio
    async def test_passes_high_confidence(self, full_db):
        """Gate should pass plans with high confidence."""
        event = Event("project_planned", {"project_id": "p1"}, "athena")
        emits = [Emit("project_planned", {
            "plan_id": "plan-1",
            "review": {"has_gaps": False, "confidence": 0.9},
        }, "athena")]

        result = await check_plan_reviewed(event, emits, full_db)
        assert result.passed


class TestCheckFilesStaged:
    @pytest.mark.asyncio
    async def test_passes_files_staged_emit(self, full_db):
        """Gate should pass with files_staged emit (not old files_committed)."""
        event = Event("task_verified", {"task_id": "t1"}, "odin")
        emits = [Emit("files_staged", {
            "task_id": "t1",
            "files": ["app.py", "test_app.py"],
        }, "hephaestus")]

        result = await check_files_staged(event, emits, full_db)
        assert result.passed
        assert "2 file(s)" in result.reason

    @pytest.mark.asyncio
    async def test_passes_when_nothing_to_stage(self, full_db):
        """Gate should pass when handler returns None (no affected_files)."""
        event = Event("task_verified", {"task_id": "t1"}, "odin")
        emits = []  # handler returned None → no emits

        result = await check_files_staged(event, emits, full_db)
        assert result.passed

    @pytest.mark.asyncio
    async def test_blocks_on_stage_failed(self, full_db):
        """Gate should block when staging fails."""
        event = Event("task_verified", {"task_id": "t1"}, "odin")
        emits = [Emit("stage_failed", {
            "task_id": "t1",
            "error": "git add failed",
        }, "hephaestus")]

        result = await check_files_staged(event, emits, full_db)
        assert not result.passed
        assert "failed" in result.reason.lower()


class TestCheckPrCreated:
    @pytest.mark.asyncio
    async def test_passes_on_skipped(self, full_db):
        """Gate should pass when no changes to commit."""
        event = Event("project_complete", {"project_id": "p1"}, "odin")
        emits = [Emit("project_committed", {
            "project_id": "p1",
            "skipped": True,
            "reason": "no changes",
        }, "hephaestus")]

        result = await check_pr_created(event, emits, full_db)
        assert result.passed
        assert "No changes" in result.reason

    @pytest.mark.asyncio
    async def test_passes_with_pr(self, full_db):
        """Gate should pass when PR is created."""
        event = Event("project_complete", {"project_id": "p1"}, "odin")
        emits = [Emit("project_committed", {
            "project_id": "p1",
            "commit_sha": "abc123",
            "pushed": True,
            "pr_url": "https://github.com/org/repo/pull/42",
        }, "hephaestus")]

        result = await check_pr_created(event, emits, full_db)
        assert result.passed
        assert "PR created" in result.reason

    @pytest.mark.asyncio
    async def test_passes_pushed_without_pr(self, full_db):
        """Gate should pass when pushed but no PR (already on feature branch)."""
        event = Event("project_complete", {"project_id": "p1"}, "odin")
        emits = [Emit("project_committed", {
            "project_id": "p1",
            "commit_sha": "abc123",
            "pushed": True,
            "pr_url": None,
        }, "hephaestus")]

        result = await check_pr_created(event, emits, full_db)
        assert result.passed

    @pytest.mark.asyncio
    async def test_blocks_on_commit_failed(self, full_db):
        """Gate should block when commit fails."""
        event = Event("project_complete", {"project_id": "p1"}, "odin")
        emits = [Emit("commit_failed", {
            "project_id": "p1",
            "error": "nothing to commit",
        }, "hephaestus")]

        result = await check_pr_created(event, emits, full_db)
        assert not result.passed


class TestGateRetryFeedback:
    @pytest.mark.asyncio
    async def test_retry_injects_gate_feedback(self, full_db):
        """Pipeline should retry handler with _gate_feedback when gate fails."""
        attempts = []

        async def flaky_handler(event, db):
            attempt = event.payload.get("_gate_attempt", 1)
            attempts.append(attempt)
            plan_json = json.dumps({"tasks": [{"id": "t1"}]}) if attempt >= 2 else "{}"
            await db.execute_write(
                "INSERT OR REPLACE INTO plans (id, project_id, plan_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("plan-1", "p1", plan_json, time.time()),
            )
            return [Emit("plan_generated", {"plan_id": "plan-1"}, "athena")]

        pipeline = Pipeline(full_db)
        pipeline.register("project_created", flaky_handler,
                          gate=check_plan_created, max_retries=2)

        await pipeline._emit(Emit("project_created", {"project_id": "p1"}, "test"))
        pipeline._last_seen_id = 0

        # Multiple ticks to process event + retries
        for _ in range(5):
            await pipeline.tick()

        assert len(attempts) >= 2
        # First attempt should be 1, second should be 2
        assert attempts[0] == 1
        assert attempts[1] == 2
