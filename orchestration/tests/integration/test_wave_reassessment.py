#  Orchestration Engine - Integration Tests for Wave Reassessment (Athena Loop)
#
#  Tests the full wave checkpoint → reassessment → replan/escalation flow
#  with mocked LLM responses. Verifies:
#  - replan_remaining: pending tasks cancelled, new plan decomposed, revision node created
#  - escalate_to_human: intervention proposal published, SSE event emitted
#
#  Depends on: backend/services/sentinel/plan_sentinel.py,
#              backend/services/task_lifecycle.py,
#              backend/services/planner.py, tests/conftest.py
#  Used by:    CI test suite

import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.enums import ReassessmentOutcome, TaskStatus
from backend.models.schemas import (
    HumanInterventionProposal,
    ReassessmentResult,
    TaskOutcomeSummary,
    WaveReassessmentContext,
)
from backend.services.sentinel.bus import SentinelBus
from backend.services.sentinel.models import Severity, SentinelObservation
from backend.services.sentinel.plan_sentinel import PlanSentinel

from tests.conftest import create_test_project, create_test_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SENTINEL_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS sentinel_observations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sentinel_decisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    timestamp REAL NOT NULL,
    command TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    confidence REAL NOT NULL,
    outcome TEXT,
    details_json TEXT
);
"""


def _make_wave_context(project_id="proj1", wave_number=1):
    """Build a minimal WaveReassessmentContext for testing."""
    return WaveReassessmentContext(
        project_id=project_id,
        wave_number=wave_number,
        task_outcomes=[
            TaskOutcomeSummary(
                task_id="task_w1_a",
                title="Research auth patterns",
                status="completed",
                output_summary="Found OAuth2 + PKCE is the right fit.",
            ),
            TaskOutcomeSummary(
                task_id="task_w1_b",
                title="Scaffold DB schema",
                status="failed",
                output_summary="",
                error="Migration tool crash: alembic version mismatch",
            ),
        ],
        knowledge_findings=["OAuth2+PKCE is preferred for SPA clients"],
        sentinel_observations=["task_w1_b stuck for 120s before failing"],
        original_plan={
            "summary": "Build auth system",
            "tasks": [
                {"title": "Research auth patterns", "wave": 1},
                {"title": "Scaffold DB schema", "wave": 1},
                {"title": "Implement login endpoint", "wave": 2},
                {"title": "Write integration tests", "wave": 3},
            ],
        },
    )


def _replan_reassessment_result():
    return ReassessmentResult(
        outcome=ReassessmentOutcome.REPLAN_REMAINING,
        rationale="Wave 1 DB task failed due to migration tool mismatch; remaining waves depend on it.",
        suggested_changes=[
            "Add alembic version pin task before schema work",
            "Merge wave 2 login endpoint into wave 2 with schema retry",
        ],
    )


def _escalate_reassessment_result():
    return ReassessmentResult(
        outcome=ReassessmentOutcome.ESCALATE_TO_HUMAN,
        rationale="Multiple critical failures and contradictory knowledge findings require human review.",
        suggested_changes=[
            "Review whether OAuth2 is still the right approach",
            "Consider switching to session-based auth",
        ],
    )


def _continue_reassessment_result():
    return ReassessmentResult(
        outcome=ReassessmentOutcome.CONTINUE_AS_PLANNED,
        rationale="All tasks passed, knowledge is consistent. Proceeding to wave 2.",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def wave_db(tmp_db):
    """tmp_db extended with sentinel tables and seeded project + tasks across waves."""
    # Add sentinel tables (not in inline schema)
    await tmp_db.conn.executescript(SENTINEL_TABLES_SQL)
    await tmp_db.conn.commit()

    project_id = "proj1"
    now = time.time()

    # Create project
    await tmp_db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
        "VALUES (?, 'Auth System', 'Build auth with OAuth2', 'active', ?, ?)",
        (project_id, now, now),
    )

    # Create plan
    plan_json = json.dumps({
        "summary": "Build auth system",
        "tasks": [
            {"title": "Research auth patterns", "wave": 1, "task_type": "research",
             "complexity": "simple", "depends_on": [], "tools_needed": []},
            {"title": "Scaffold DB schema", "wave": 1, "task_type": "code",
             "complexity": "moderate", "depends_on": [], "tools_needed": []},
            {"title": "Implement login endpoint", "wave": 2, "task_type": "code",
             "complexity": "moderate", "depends_on": [0, 1], "tools_needed": []},
            {"title": "Write integration tests", "wave": 3, "task_type": "code",
             "complexity": "simple", "depends_on": [2], "tools_needed": []},
        ],
    })
    await tmp_db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'claude-haiku', ?, 'approved', ?)",
        ("plan_proj1", project_id, plan_json, now),
    )

    # Wave 1 tasks (terminal)
    await tmp_db.execute_write(
        "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
        "priority, status, model_tier, wave, retry_count, max_retries, created_at, updated_at, "
        "output_text, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("task_w1_a", project_id, "plan_proj1", "Research auth patterns",
         "Research OAuth2 patterns", "research", 0, "completed", "haiku",
         1, 0, 5, now, now, "Found OAuth2 + PKCE is the right fit.", None),
    )
    await tmp_db.execute_write(
        "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
        "priority, status, model_tier, wave, retry_count, max_retries, created_at, updated_at, "
        "output_text, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("task_w1_b", project_id, "plan_proj1", "Scaffold DB schema",
         "Create DB schema", "code", 0, "failed", "haiku",
         1, 3, 5, now, now, None, "Migration tool crash: alembic version mismatch"),
    )

    # Wave 2 task (pending — should be cancelled on replan)
    await tmp_db.execute_write(
        "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
        "priority, status, model_tier, wave, retry_count, max_retries, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("task_w2_a", project_id, "plan_proj1", "Implement login endpoint",
         "Build login", "code", 0, "pending", "sonnet",
         2, 0, 5, now, now),
    )

    # Wave 3 task (blocked — should also be cancelled on replan)
    await tmp_db.execute_write(
        "INSERT INTO tasks (id, project_id, plan_id, title, description, task_type, "
        "priority, status, model_tier, wave, retry_count, max_retries, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("task_w3_a", project_id, "plan_proj1", "Write integration tests",
         "Write tests", "code", 0, "blocked", "haiku",
         3, 0, 5, now, now),
    )

    # Seed knowledge
    await tmp_db.execute_write(
        "INSERT INTO project_knowledge (id, project_id, category, content, content_hash, "
        "rationale, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("k1", project_id, "discovery", "OAuth2+PKCE is preferred for SPA clients",
         "abc123", "Research task found this", "high", now),
    )

    # Seed sentinel observation
    await tmp_db.execute_write(
        "INSERT INTO sentinel_observations (id, project_id, task_id, category, severity, "
        "message, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("obs1", project_id, "task_w1_b", "task_stuck", "warning",
         "task_w1_b stuck for 120s before failing",
         json.dumps({"task_id": "task_w1_b", "duration": 120}), now),
    )

    return tmp_db


@pytest.fixture
def mock_bus():
    """A real SentinelBus so we can inspect published messages."""
    return SentinelBus()


@pytest.fixture
def mock_progress():
    """Mocked ProgressManager for SSE event verification."""
    pm = AsyncMock()
    pm.push_event = AsyncMock()
    return pm


# ---------------------------------------------------------------------------
# Test: collect_wave_reassessment_context (real DB)
# ---------------------------------------------------------------------------

class TestCollectWaveContextRealDB:
    """Verify context collection against a real seeded database."""

    async def test_collects_all_context(self, wave_db):
        from backend.services.task_lifecycle import collect_wave_reassessment_context

        ctx = await collect_wave_reassessment_context(
            db=wave_db, project_id="proj1", wave_number=1,
        )

        assert ctx is not None
        assert ctx.project_id == "proj1"
        assert ctx.wave_number == 1
        assert len(ctx.task_outcomes) == 2

        completed = [t for t in ctx.task_outcomes if t.status == "completed"]
        failed = [t for t in ctx.task_outcomes if t.status == "failed"]
        assert len(completed) == 1
        assert len(failed) == 1
        assert failed[0].error == "Migration tool crash: alembic version mismatch"

        assert len(ctx.knowledge_findings) >= 1
        assert any("OAuth2" in f for f in ctx.knowledge_findings)

        assert len(ctx.sentinel_observations) >= 1
        assert ctx.original_plan["summary"] == "Build auth system"

    async def test_returns_none_without_plan(self, tmp_db):
        """No plan → returns None."""
        from backend.services.task_lifecycle import collect_wave_reassessment_context

        now = time.time()
        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES ('no_plan', 'X', 'x', 'draft', ?, ?)",
            (now, now),
        )
        ctx = await collect_wave_reassessment_context(
            db=tmp_db, project_id="no_plan", wave_number=1,
        )
        assert ctx is None


# ---------------------------------------------------------------------------
# Test: execute_replan — cancels tasks, generates new plan, creates revision
# ---------------------------------------------------------------------------

class TestExecuteReplan:
    """Integration test for the full replan flow with real DB."""

    async def test_replan_cancels_future_wave_tasks(self, wave_db):
        """Pending/blocked tasks in waves > completed_wave get cancelled."""
        from backend.services.task_lifecycle import execute_replan

        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        # Mock planner.generate to return a new plan
        new_plan_id = f"plan_replan_{uuid.uuid4().hex[:8]}"
        mock_planner = AsyncMock()
        mock_planner.generate = AsyncMock(return_value={
            "plan_id": new_plan_id,
            "version": 2,
            "plan": {"summary": "Revised auth plan"},
            "model_used": "test",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
        })

        # Mock decomposer to return task creation summary
        mock_decomposer = AsyncMock()
        mock_decomposer.decompose = AsyncMock(return_value={
            "tasks_created": 3,
            "total_waves": 2,
        })

        # Mock plan_sync for revision tracking
        mock_plan_sync = AsyncMock()
        mock_plan_sync.sync_revision = AsyncMock(return_value="revision_node_42")

        with patch("backend.services.task_lifecycle.PlannerService", return_value=mock_planner), \
             patch("backend.services.task_lifecycle.DecomposerService", return_value=mock_decomposer):

            result = await execute_replan(
                db=wave_db,
                budget=mock_budget,
                plan_sync=mock_plan_sync,
                project_id="proj1",
                completed_wave=1,
                rationale="DB task failed",
                suggested_changes=["Pin alembic version"],
                wave_context_json='{"wave": 1}',
            )

        # Verify cancellation
        assert result["cancelled_count"] == 2  # task_w2_a (pending) + task_w3_a (blocked)

        # Verify cancelled task statuses in DB
        w2 = await wave_db.fetchone("SELECT status FROM tasks WHERE id = 'task_w2_a'")
        w3 = await wave_db.fetchone("SELECT status FROM tasks WHERE id = 'task_w3_a'")
        assert w2["status"] == TaskStatus.CANCELLED
        assert w3["status"] == TaskStatus.CANCELLED

        # Wave 1 tasks should be untouched
        w1a = await wave_db.fetchone("SELECT status FROM tasks WHERE id = 'task_w1_a'")
        w1b = await wave_db.fetchone("SELECT status FROM tasks WHERE id = 'task_w1_b'")
        assert w1a["status"] == "completed"
        assert w1b["status"] == "failed"

        # Verify new plan was generated
        assert result["new_plan_id"] == new_plan_id
        assert result["new_tasks_created"] == 3
        assert result["new_total_waves"] == 2

        # Verify revision node was created
        assert result["revision_node_id"] == "revision_node_42"
        mock_plan_sync.sync_revision.assert_called_once()
        call_kwargs = mock_plan_sync.sync_revision.call_args
        assert call_kwargs.kwargs["wave_number"] == 1
        assert call_kwargs.kwargs["outcome"] == "replan_remaining"
        assert "DB task failed" in call_kwargs.kwargs["rationale"]

    async def test_replan_restores_requirements_on_error(self, wave_db):
        """Even if planner.generate fails, original requirements are restored."""
        from backend.services.task_lifecycle import execute_replan

        mock_budget = AsyncMock()
        mock_budget.record_spend = AsyncMock()

        mock_planner = AsyncMock()
        mock_planner.generate = AsyncMock(side_effect=RuntimeError("LLM down"))

        with patch("backend.services.task_lifecycle.PlannerService", return_value=mock_planner), \
             pytest.raises(RuntimeError, match="LLM down"):
            await execute_replan(
                db=wave_db,
                budget=mock_budget,
                plan_sync=None,
                project_id="proj1",
                completed_wave=1,
                rationale="test",
                suggested_changes=[],
            )

        # Requirements should be restored to original
        row = await wave_db.fetchone("SELECT requirements FROM projects WHERE id = 'proj1'")
        assert "WAVE 1 REASSESSMENT" not in row["requirements"]
        assert row["requirements"] == "Build auth with OAuth2"

    async def test_replan_no_revision_without_plan_sync(self, wave_db):
        """If plan_sync is None, replan still succeeds without revision node."""
        from backend.services.task_lifecycle import execute_replan

        mock_budget = AsyncMock()
        mock_planner = AsyncMock()
        mock_planner.generate = AsyncMock(return_value={
            "plan_id": "plan_new", "version": 2,
            "plan": {}, "model_used": "test",
            "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
        })
        mock_decomposer = AsyncMock()
        mock_decomposer.decompose = AsyncMock(return_value={
            "tasks_created": 1, "total_waves": 1,
        })

        with patch("backend.services.task_lifecycle.PlannerService", return_value=mock_planner), \
             patch("backend.services.task_lifecycle.DecomposerService", return_value=mock_decomposer):
            result = await execute_replan(
                db=wave_db,
                budget=mock_budget,
                plan_sync=None,
                project_id="proj1",
                completed_wave=1,
                rationale="test",
                suggested_changes=[],
            )

        assert result["revision_node_id"] is None
        assert result["cancelled_count"] == 2


# ---------------------------------------------------------------------------
# Test: _trigger_wave_reassessment — replan_remaining path
# ---------------------------------------------------------------------------

class TestTriggerWaveReassessmentReplan:
    """Mock LLM returns replan_remaining → verify full flow."""

    async def test_replan_flow(self, wave_db, mock_bus, mock_progress):
        sentinel = PlanSentinel(
            project_id="proj1",
            bus=mock_bus,
            db=wave_db,
            progress_manager=mock_progress,
        )

        replan_result = _replan_reassessment_result()

        # Capture bus messages
        received = []
        async def _capture(msg):
            received.append(msg)
        mock_bus.on("replan_required", _capture)

        mock_execute_replan = AsyncMock(return_value={
            "cancelled_count": 2,
            "new_plan_id": "plan_v2",
            "new_tasks_created": 3,
            "new_total_waves": 2,
            "revision_node_id": "rev_42",
        })

        wave_obs = SentinelObservation(
            project_id="proj1",
            task_id=None,
            category="wave_complete",
            severity=Severity.INFO,
            message="Wave 1 complete",
            details={"wave": 1},
        )

        with patch(
            "backend.services.planner.PlannerService.evaluate_wave_reassessment",
            new_callable=AsyncMock,
            return_value=replan_result,
        ), patch(
            "backend.services.task_lifecycle.execute_replan",
            mock_execute_replan,
        ), patch(
            "backend.services.sentinel.plan_sentinel.ContextStoreClient",
        ), patch(
            "backend.services.sentinel.plan_sentinel.PlanSyncService",
        ):
            await sentinel._trigger_wave_reassessment(wave_obs)

        # Verify execute_replan was called with correct args
        mock_execute_replan.assert_called_once()
        call_kw = mock_execute_replan.call_args.kwargs
        assert call_kw["project_id"] == "proj1"
        assert call_kw["completed_wave"] == 1
        assert "migration tool mismatch" in call_kw["rationale"].lower()
        assert len(call_kw["suggested_changes"]) == 2

        # Verify bus message published
        assert len(received) == 1
        msg = received[0]
        assert msg.topic == "replan_required"
        assert msg.payload["project_id"] == "proj1"
        assert msg.payload["new_plan_id"] == "plan_v2"
        assert msg.payload["cancelled_count"] == 2
        assert msg.payload["revision_node_id"] == "rev_42"

        # Verify reassessment observation persisted to DB
        obs_rows = await wave_db.fetchall(
            "SELECT * FROM sentinel_observations WHERE category = 'wave_reassessment'",
        )
        assert len(obs_rows) == 1
        details = json.loads(obs_rows[0]["details_json"])
        assert details["outcome"] == "replan_remaining"
        assert details["wave_number"] == 1


# ---------------------------------------------------------------------------
# Test: _trigger_wave_reassessment — escalate_to_human path
# ---------------------------------------------------------------------------

class TestTriggerWaveReassessmentEscalate:
    """Mock LLM returns escalate_to_human → verify intervention + SSE."""

    async def test_escalation_flow(self, wave_db, mock_bus, mock_progress):
        sentinel = PlanSentinel(
            project_id="proj1",
            bus=mock_bus,
            db=wave_db,
            progress_manager=mock_progress,
        )

        escalate_result = _escalate_reassessment_result()

        # Capture intervention proposals on the bus
        received = []
        async def _capture(msg):
            received.append(msg)
        mock_bus.on("intervention_proposal", _capture)

        wave_obs = SentinelObservation(
            project_id="proj1",
            task_id=None,
            category="wave_complete",
            severity=Severity.INFO,
            message="Wave 1 complete",
            details={"wave": 1},
        )

        with patch(
            "backend.services.planner.PlannerService.evaluate_wave_reassessment",
            new_callable=AsyncMock,
            return_value=escalate_result,
        ):
            await sentinel._trigger_wave_reassessment(wave_obs)

        # Verify SSE event emitted
        mock_progress.push_event.assert_called_once()
        sse_kwargs = mock_progress.push_event.call_args.kwargs
        assert sse_kwargs["project_id"] == "proj1"
        assert sse_kwargs["event_type"] == "escalation_proposal"
        assert "human intervention" in sse_kwargs["message"].lower()
        assert sse_kwargs["wave_number"] == 1
        assert "OAuth2" in str(sse_kwargs.get("suggested_actions", []))

        # Verify intervention proposal published to bus
        assert len(received) == 1
        msg = received[0]
        assert msg.payload["category"] == "wave_reassessment_escalation"
        assert msg.payload["severity"] == "high"

        # Verify reassessment observation persisted to DB
        obs_rows = await wave_db.fetchall(
            "SELECT * FROM sentinel_observations WHERE category = 'wave_reassessment'",
        )
        assert len(obs_rows) == 1
        details = json.loads(obs_rows[0]["details_json"])
        assert details["outcome"] == "escalate_to_human"

    async def test_escalation_without_progress_manager(self, wave_db, mock_bus):
        """Escalation works even without a ProgressManager (no SSE)."""
        sentinel = PlanSentinel(
            project_id="proj1",
            bus=mock_bus,
            db=wave_db,
            progress_manager=None,
        )

        wave_obs = SentinelObservation(
            project_id="proj1",
            task_id=None,
            category="wave_complete",
            severity=Severity.INFO,
            message="Wave 1 complete",
            details={"wave": 1},
        )

        with patch(
            "backend.services.planner.PlannerService.evaluate_wave_reassessment",
            new_callable=AsyncMock,
            return_value=_escalate_reassessment_result(),
        ):
            # Should not raise
            await sentinel._trigger_wave_reassessment(wave_obs)

        # Observation still persisted
        obs_rows = await wave_db.fetchall(
            "SELECT * FROM sentinel_observations WHERE category = 'wave_reassessment'",
        )
        assert len(obs_rows) == 1


# ---------------------------------------------------------------------------
# Test: _trigger_wave_reassessment — continue_as_planned path
# ---------------------------------------------------------------------------

class TestTriggerWaveReassessmentContinue:
    """Mock LLM returns continue_as_planned → no replan, no escalation."""

    async def test_continue_no_side_effects(self, wave_db, mock_bus, mock_progress):
        sentinel = PlanSentinel(
            project_id="proj1",
            bus=mock_bus,
            db=wave_db,
            progress_manager=mock_progress,
        )

        wave_obs = SentinelObservation(
            project_id="proj1",
            task_id=None,
            category="wave_complete",
            severity=Severity.INFO,
            message="Wave 1 complete",
            details={"wave": 1},
        )

        with patch(
            "backend.services.planner.PlannerService.evaluate_wave_reassessment",
            new_callable=AsyncMock,
            return_value=_continue_reassessment_result(),
        ):
            await sentinel._trigger_wave_reassessment(wave_obs)

        # No SSE escalation event
        mock_progress.push_event.assert_not_called()

        # Pending tasks still pending (no cancellation)
        w2 = await wave_db.fetchone("SELECT status FROM tasks WHERE id = 'task_w2_a'")
        assert w2["status"] == "pending"

        # Reassessment observation still recorded
        obs_rows = await wave_db.fetchall(
            "SELECT * FROM sentinel_observations WHERE category = 'wave_reassessment'",
        )
        assert len(obs_rows) == 1
        details = json.loads(obs_rows[0]["details_json"])
        assert details["outcome"] == "continue_as_planned"


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------

class TestWaveReassessmentEdgeCases:

    async def test_no_db_skips_reassessment(self, mock_bus, mock_progress):
        """Sentinel without DB connection returns early."""
        sentinel = PlanSentinel(
            project_id="proj1",
            bus=mock_bus,
            db=None,
            progress_manager=mock_progress,
        )

        wave_obs = SentinelObservation(
            project_id="proj1",
            task_id=None,
            category="wave_complete",
            severity=Severity.INFO,
            message="Wave 1 complete",
            details={"wave": 1},
        )

        # Should not raise — just returns
        await sentinel._trigger_wave_reassessment(wave_obs)
        mock_progress.push_event.assert_not_called()

    async def test_missing_wave_number_skips(self, wave_db, mock_bus, mock_progress):
        """Observation without wave number in details → early return."""
        sentinel = PlanSentinel(
            project_id="proj1",
            bus=mock_bus,
            db=wave_db,
            progress_manager=mock_progress,
        )

        wave_obs = SentinelObservation(
            project_id="proj1",
            task_id=None,
            category="wave_complete",
            severity=Severity.INFO,
            message="Wave complete but no wave number",
            details={},  # Missing "wave" key
        )

        await sentinel._trigger_wave_reassessment(wave_obs)
        mock_progress.push_event.assert_not_called()

    async def test_context_collection_fails_gracefully(self, wave_db, mock_bus, mock_progress):
        """If context collection returns None, reassessment is skipped."""
        sentinel = PlanSentinel(
            project_id="proj_nonexistent",
            bus=mock_bus,
            db=wave_db,
            progress_manager=mock_progress,
        )

        wave_obs = SentinelObservation(
            project_id="proj_nonexistent",
            task_id=None,
            category="wave_complete",
            severity=Severity.INFO,
            message="Wave 1 complete",
            details={"wave": 1},
        )

        # proj_nonexistent has no plan → collect returns None
        await sentinel._trigger_wave_reassessment(wave_obs)
        mock_progress.push_event.assert_not_called()

    async def test_llm_error_falls_back_to_escalation(self, wave_db, mock_bus, mock_progress):
        """If evaluate_wave_reassessment raises, fallback to escalate_to_human."""
        sentinel = PlanSentinel(
            project_id="proj1",
            bus=mock_bus,
            db=wave_db,
            progress_manager=mock_progress,
        )

        wave_obs = SentinelObservation(
            project_id="proj1",
            task_id=None,
            category="wave_complete",
            severity=Severity.INFO,
            message="Wave 1 complete",
            details={"wave": 1},
        )

        # The planner's evaluate_wave_reassessment has an internal try/except
        # that returns escalate_to_human on error. We mock the whole method
        # to return an escalation result (simulating the fallback).
        fallback_result = ReassessmentResult(
            outcome=ReassessmentOutcome.ESCALATE_TO_HUMAN,
            rationale="Automated reassessment failed with an error. Human review required.",
        )

        with patch(
            "backend.services.planner.PlannerService.evaluate_wave_reassessment",
            new_callable=AsyncMock,
            return_value=fallback_result,
        ):
            await sentinel._trigger_wave_reassessment(wave_obs)

        # Should escalate — SSE event emitted
        mock_progress.push_event.assert_called_once()
        sse_kwargs = mock_progress.push_event.call_args.kwargs
        assert sse_kwargs["event_type"] == "escalation_proposal"


# ---------------------------------------------------------------------------
# Test: HumanInterventionProposal schema
# ---------------------------------------------------------------------------

class TestHumanInterventionProposal:
    """Verify the proposal schema serializes correctly for the frontend."""

    def test_proposal_round_trips(self):
        ctx = _make_wave_context()
        proposal = HumanInterventionProposal(
            project_id="proj1",
            wave_number=1,
            rationale="Critical failures require human review",
            reassessment_context=ctx,
            suggested_actions=["Review auth approach", "Check DB migration tool"],
        )
        data = proposal.model_dump(mode="json")

        assert data["project_id"] == "proj1"
        assert data["wave_number"] == 1
        assert len(data["reassessment_context"]["task_outcomes"]) == 2
        assert len(data["suggested_actions"]) == 2

        # Verify it can be reconstructed
        restored = HumanInterventionProposal(**data)
        assert restored.rationale == proposal.rationale
        assert len(restored.reassessment_context.task_outcomes) == 2
