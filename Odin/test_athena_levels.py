"""Tests for athena level progression pipeline.

RED PHASE: The leveled planning flow doesn't exist yet.

Flow:
  project_created
    → athena_plan_leveled
      → L1 generate (AI)
      → rule check L1
      → L2 deepen (AI)
      → rule check L2
      → L3 deepen (AI)
      → rule check L3
      → AI thorough review (different provider)
      → rule check
      → TDD phase (if enabled): generate tests → review tests → rule check
      → project_planned
"""

import pytest
import pytest_asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock

from gods.pipeline import Event, Emit
from gods.plan_levels import PlanLevel, TaskSpec, PlanConfig, RuleResult
from gods.tooling import ToolingInfo


# RED: this doesn't exist yet
from gods.handlers.athena_leveled import (
    athena_plan_leveled,
    _deepen_plan,
    _thorough_review,
    _generate_tdd_tests,
)


@pytest.fixture(autouse=True)
def mock_tooling():
    """Patch tooling availability for all tests — avoid HTTP calls."""
    with patch(
        "gods.tooling.check_tooling_availability",
        new_callable=AsyncMock,
        return_value=ToolingInfo(has_roslyn=True, has_jedi=True, has_ts_compiler=True),
    ):
        yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def plan_db(sqlite_db):
    """SQLite with project + tasks for athena level tests."""
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT,
            requirements TEXT,
            status TEXT DEFAULT 'draft',
            config_json TEXT DEFAULT '{}',
            updated_at REAL
        )
    """)
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            title TEXT,
            description TEXT,
            task_type TEXT DEFAULT 'code',
            complexity TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'pending',
            wave INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 0,
            implementation_notes TEXT,
            test_strategy TEXT,
            context_json TEXT DEFAULT '{}',
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
    await sqlite_db.execute_write("""
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            plan_json TEXT,
            level TEXT DEFAULT 'L1',
            created_at REAL
        )
    """)
    return sqlite_db


async def _seed_project(db, project_id="proj-1", status="draft",
                        requirements="Add a health endpoint that returns version and uptime"):
    await db.execute_write(
        "INSERT OR REPLACE INTO projects (id, name, requirements, status, config_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, "Test Project", requirements, status, '{"tdd": true, "narration": true}', time.time()),
    )


# ---------------------------------------------------------------------------
# Mock plan generators — return plan data at each level
# ---------------------------------------------------------------------------

def _mock_l1_plan():
    return {
        "plan_id": "plan-1",
        "tasks": [
            {"id": "t1", "title": "Add health endpoint", "task_type": "code", "wave": 0},
            {"id": "t2", "title": "Add health tests", "task_type": "code", "wave": 0},
        ],
    }


def _mock_l2_plan():
    return {
        "plan_id": "plan-1",
        "tasks": [
            {
                "id": "t1", "title": "Add health endpoint", "task_type": "code", "wave": 0,
                "description": "Add GET /api/health/detailed returning version and uptime",
                "affected_files": ["orchestration/backend/routes/health.py"],
                "depends_on": [], "complexity": "simple",
            },
            {
                "id": "t2", "title": "Add health tests", "task_type": "code", "wave": 0,
                "description": "Test health endpoint returns 200 with expected keys",
                "affected_files": ["orchestration/tests/test_health.py"],
                "depends_on": ["t1"], "complexity": "simple",
            },
        ],
    }


def _mock_l3_plan():
    return {
        "plan_id": "plan-1",
        "tasks": [
            {
                "id": "t1", "title": "Add health endpoint", "task_type": "code", "wave": 0,
                "description": "Add GET /api/health/detailed returning version and uptime",
                "affected_files": ["orchestration/backend/routes/health.py"],
                "depends_on": [], "complexity": "simple",
                "implementation_notes": "Use FastAPI router, importlib.metadata for version",
                "test_strategy": "Test returns 200 with keys: version, uptime, db_status",
                "edge_cases": ["DB down returns degraded status"],
            },
            {
                "id": "t2", "title": "Add health tests", "task_type": "code", "wave": 0,
                "description": "Test health endpoint",
                "affected_files": ["orchestration/tests/test_health.py"],
                "depends_on": ["t1"], "complexity": "simple",
                "implementation_notes": "Use httpx AsyncClient, test happy path + DB down",
                "test_strategy": "Test 200 response, test degraded response",
                "edge_cases": ["Timeout on DB check"],
            },
        ],
    }


# ---------------------------------------------------------------------------
# Full pipeline: project_created → L1 → L2 → L3 → review → planned
# ---------------------------------------------------------------------------

class TestAthenaLeveledPipeline:
    @pytest.mark.asyncio
    async def test_full_pipeline_l1_through_l3(self, plan_db):
        """project_created → generates at L1, deepens to L2, deepens to L3, reviews, emits planned."""
        await _seed_project(plan_db)

        event = Event("project_created", {"project_id": "proj-1"}, "system")

        with patch("gods.handlers.athena_leveled._generate_l1", new_callable=AsyncMock) as mock_l1, \
             patch("gods.handlers.athena_leveled._deepen_plan", new_callable=AsyncMock) as mock_deepen, \
             patch("gods.handlers.athena_leveled._thorough_review", new_callable=AsyncMock) as mock_review, \
             patch("gods.handlers.athena_leveled._generate_tdd_tests", new_callable=AsyncMock) as mock_tdd:

            mock_l1.return_value = _mock_l1_plan()
            # With all tooling available, target is L5: L1→L2→L3→L4→L5 = 4 deepen calls
            mock_deepen.return_value = _mock_l3_plan()  # return valid plan each time
            mock_review.return_value = {"approved": True, "confidence": 0.9, "feedback": ""}
            mock_tdd.return_value = {"test_specs": []}

            emits = await athena_plan_leveled(event, plan_db)

        planned = next((e for e in emits if e.event_type == "project_planned"), None)
        assert planned is not None
        assert planned.payload["project_id"] == "proj-1"
        # With all tooling → L5, but rule checks may cap lower
        assert planned.payload["level"] in ("L3", "L4", "L5")

        mock_l1.assert_called_once()
        assert mock_deepen.call_count >= 2  # at least L2 + L3

    @pytest.mark.asyncio
    async def test_stops_at_configured_level(self, plan_db):
        """If config says L2, stop after L2 + review."""
        await _seed_project(plan_db)
        # Override config to target L2
        await plan_db.execute_write(
            "UPDATE projects SET config_json = ? WHERE id = ?",
            ('{"tdd": true, "target_level": "L2"}', "proj-1"),
        )

        event = Event("project_created", {"project_id": "proj-1"}, "system")

        with patch("gods.handlers.athena_leveled._generate_l1", new_callable=AsyncMock) as mock_l1, \
             patch("gods.handlers.athena_leveled._deepen_plan", new_callable=AsyncMock) as mock_deepen, \
             patch("gods.handlers.athena_leveled._thorough_review", new_callable=AsyncMock) as mock_review, \
             patch("gods.handlers.athena_leveled._generate_tdd_tests", new_callable=AsyncMock) as mock_tdd:

            mock_l1.return_value = _mock_l1_plan()
            mock_deepen.return_value = _mock_l2_plan()
            mock_review.return_value = {"approved": True, "confidence": 0.9, "feedback": ""}
            mock_tdd.return_value = {"test_specs": []}

            emits = await athena_plan_leveled(event, plan_db)

        planned = next((e for e in emits if e.event_type == "project_planned"), None)
        assert planned is not None
        assert planned.payload["level"] == "L2"

        # Deepen called only once (L1 → L2), not twice
        assert mock_deepen.call_count == 1


# ---------------------------------------------------------------------------
# Rule check between levels
# ---------------------------------------------------------------------------

class TestRuleCheckBetweenLevels:
    @pytest.mark.asyncio
    async def test_l1_rule_failure_regenerates(self, plan_db):
        """If L1 plan fails rule check, regenerate with feedback."""
        await _seed_project(plan_db)

        event = Event("project_created", {"project_id": "proj-1"}, "system")

        bad_l1 = {"plan_id": "plan-1", "tasks": [{"id": "t1", "title": "", "task_type": "code", "wave": 0}]}
        good_l1 = _mock_l1_plan()

        with patch("gods.handlers.athena_leveled._generate_l1", new_callable=AsyncMock) as mock_l1, \
             patch("gods.handlers.athena_leveled._deepen_plan", new_callable=AsyncMock) as mock_deepen, \
             patch("gods.handlers.athena_leveled._thorough_review", new_callable=AsyncMock) as mock_review, \
             patch("gods.handlers.athena_leveled._generate_tdd_tests", new_callable=AsyncMock) as mock_tdd:

            mock_l1.side_effect = [bad_l1, good_l1]
            mock_deepen.return_value = _mock_l3_plan()  # valid at all levels
            mock_review.return_value = {"approved": True, "confidence": 0.9, "feedback": ""}
            mock_tdd.return_value = {"test_specs": []}

            emits = await athena_plan_leveled(event, plan_db)

        # L1 was called twice (first failed rule check, second passed)
        assert mock_l1.call_count == 2
        planned = next((e for e in emits if e.event_type == "project_planned"), None)
        assert planned is not None

    @pytest.mark.asyncio
    async def test_max_rule_retries_then_fail(self, plan_db):
        """If rule check keeps failing, emit planning_failed."""
        await _seed_project(plan_db)

        event = Event("project_created", {"project_id": "proj-1"}, "system")

        bad_l1 = {"plan_id": "plan-1", "tasks": [{"id": "t1", "title": "", "task_type": "code", "wave": 0}]}

        with patch("gods.handlers.athena_leveled._generate_l1", new_callable=AsyncMock) as mock_l1:
            # Always return bad plan
            mock_l1.return_value = bad_l1

            emits = await athena_plan_leveled(event, plan_db)

        failed = next((e for e in emits if e.event_type == "planning_failed"), None)
        assert failed is not None
        assert "rule" in failed.payload.get("error", "").lower() or \
               "validation" in failed.payload.get("error", "").lower()


# ---------------------------------------------------------------------------
# AI thorough review
# ---------------------------------------------------------------------------

class TestThoroughReview:
    @pytest.mark.asyncio
    async def test_review_rejection_triggers_regenerate(self, plan_db):
        """AI review says plan is wrong → regenerate at current level."""
        await _seed_project(plan_db)

        event = Event("project_created", {"project_id": "proj-1"}, "system")

        with patch("gods.handlers.athena_leveled._generate_l1", new_callable=AsyncMock) as mock_l1, \
             patch("gods.handlers.athena_leveled._deepen_plan", new_callable=AsyncMock) as mock_deepen, \
             patch("gods.handlers.athena_leveled._thorough_review", new_callable=AsyncMock) as mock_review, \
             patch("gods.handlers.athena_leveled._generate_tdd_tests", new_callable=AsyncMock) as mock_tdd:

            mock_l1.return_value = _mock_l1_plan()
            mock_deepen.return_value = _mock_l3_plan()  # valid at all levels
            # First review rejects, second approves
            mock_review.side_effect = [
                {"approved": False, "confidence": 0.4, "feedback": "Missing error handling task"},
                {"approved": True, "confidence": 0.85, "feedback": ""},
            ]
            mock_tdd.return_value = {"test_specs": []}

            emits = await athena_plan_leveled(event, plan_db)

        # Review called twice
        assert mock_review.call_count == 2
        planned = next((e for e in emits if e.event_type == "project_planned"), None)
        assert planned is not None


# ---------------------------------------------------------------------------
# TDD phase
# ---------------------------------------------------------------------------

class TestTDDPhase:
    @pytest.mark.asyncio
    async def test_tdd_generates_test_specs(self, plan_db):
        """When tdd=true, generate test specs after review passes."""
        await _seed_project(plan_db)

        event = Event("project_created", {"project_id": "proj-1"}, "system")

        with patch("gods.handlers.athena_leveled._generate_l1", new_callable=AsyncMock) as mock_l1, \
             patch("gods.handlers.athena_leveled._deepen_plan", new_callable=AsyncMock) as mock_deepen, \
             patch("gods.handlers.athena_leveled._thorough_review", new_callable=AsyncMock) as mock_review, \
             patch("gods.handlers.athena_leveled._generate_tdd_tests", new_callable=AsyncMock) as mock_tdd:

            mock_l1.return_value = _mock_l1_plan()
            mock_deepen.return_value = _mock_l3_plan()
            mock_review.return_value = {"approved": True, "confidence": 0.9, "feedback": ""}
            mock_tdd.return_value = {
                "test_specs": [
                    {"task_id": "t1", "test_file": "tests/test_health.py",
                     "test_cases": ["test_returns_200", "test_returns_version_key", "test_db_down_degraded"]},
                ],
            }

            emits = await athena_plan_leveled(event, plan_db)

        mock_tdd.assert_called_once()
        planned = next((e for e in emits if e.event_type == "project_planned"), None)
        assert planned is not None
        assert "test_specs" in planned.payload

    @pytest.mark.asyncio
    async def test_tdd_disabled_skips_test_gen(self, plan_db):
        """When tdd=false, skip TDD phase entirely."""
        await _seed_project(plan_db)
        await plan_db.execute_write(
            "UPDATE projects SET config_json = ? WHERE id = ?",
            ('{"tdd": false}', "proj-1"),
        )

        event = Event("project_created", {"project_id": "proj-1"}, "system")

        with patch("gods.handlers.athena_leveled._generate_l1", new_callable=AsyncMock) as mock_l1, \
             patch("gods.handlers.athena_leveled._deepen_plan", new_callable=AsyncMock) as mock_deepen, \
             patch("gods.handlers.athena_leveled._thorough_review", new_callable=AsyncMock) as mock_review, \
             patch("gods.handlers.athena_leveled._generate_tdd_tests", new_callable=AsyncMock) as mock_tdd:

            mock_l1.return_value = _mock_l1_plan()
            mock_deepen.return_value = _mock_l3_plan()
            mock_review.return_value = {"approved": True, "confidence": 0.9, "feedback": ""}

            emits = await athena_plan_leveled(event, plan_db)

        mock_tdd.assert_not_called()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestAthenaLeveledEdgeCases:
    @pytest.mark.asyncio
    async def test_project_not_found(self, plan_db):
        event = Event("project_created", {"project_id": "nonexistent"}, "system")
        emits = await athena_plan_leveled(event, plan_db)
        failed = next((e for e in emits if e.event_type == "planning_failed"), None)
        assert failed is not None

    @pytest.mark.asyncio
    async def test_already_planned_skips(self, plan_db):
        await _seed_project(plan_db, status="planned")
        event = Event("project_created", {"project_id": "proj-1"}, "system")
        emits = await athena_plan_leveled(event, plan_db)
        assert emits == [] or emits is None

    @pytest.mark.asyncio
    async def test_no_project_id(self, plan_db):
        event = Event("project_created", {}, "system")
        emits = await athena_plan_leveled(event, plan_db)
        failed = next((e for e in emits if e.event_type == "planning_failed"), None)
        assert failed is not None

    @pytest.mark.asyncio
    async def test_narration_events_emitted(self, plan_db):
        """When narration=true, should emit narration events during planning."""
        await _seed_project(plan_db)

        event = Event("project_created", {"project_id": "proj-1"}, "system")

        with patch("gods.handlers.athena_leveled._generate_l1", new_callable=AsyncMock) as mock_l1, \
             patch("gods.handlers.athena_leveled._deepen_plan", new_callable=AsyncMock) as mock_deepen, \
             patch("gods.handlers.athena_leveled._thorough_review", new_callable=AsyncMock) as mock_review, \
             patch("gods.handlers.athena_leveled._generate_tdd_tests", new_callable=AsyncMock) as mock_tdd:

            mock_l1.return_value = _mock_l1_plan()
            mock_deepen.return_value = _mock_l3_plan()
            mock_review.return_value = {"approved": True, "confidence": 0.9, "feedback": ""}
            mock_tdd.return_value = {"test_specs": []}

            emits = await athena_plan_leveled(event, plan_db)

        narration = [e for e in emits if e.event_type == "narration"]
        assert len(narration) > 0, "Should emit narration events during planning"
