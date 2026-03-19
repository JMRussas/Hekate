"""Tests for gods/handlers/athena.py — planning handler.

RED PHASE: gods/handlers/athena.py does not exist yet.

The athena_plan handler:
  1. Takes project_created event
  2. Calls PlannerService.generate() to create a plan
  3. Reviews the plan via review_plan() (different provider)
  4. If review finds gaps → regenerates with feedback
  5. Emits plan_generated (for gate) then project_planned (for odin)

The handler does NOT own the planning logic — it's a thin wrapper
that wires planner.py + review.py into the pipeline.
"""

import json
import time
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio

from gods.pipeline import Pipeline, Event, Emit, GateResult


# ---------------------------------------------------------------------------
# Import handler — DOES NOT EXIST YET (RED)
# ---------------------------------------------------------------------------

from gods.handlers.athena import athena_plan, athena_reassess


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def plan_db(sqlite_db):
    """SQLite with projects + plans tables for planning tests."""
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
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            version INTEGER DEFAULT 1,
            model_used TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            plan_json TEXT,
            status TEXT DEFAULT 'draft',
            node_mapping_json TEXT,
            created_at REAL
        )
    """)
    # Insert a test project
    await sqlite_db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, config_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("proj-1", "Test Project", "Build a login page with email and password",
         "draft", '{"planning_rigor": "L2"}', time.time()),
    )
    return sqlite_db


# ---------------------------------------------------------------------------
# athena_plan — basic flow
# ---------------------------------------------------------------------------

class TestAthenaPlan:
    @pytest.mark.asyncio
    async def test_generates_plan_and_emits(self, plan_db):
        """project_created → plan generated → plan_generated + project_planned emitted."""
        event = Event("project_created", {"project_id": "proj-1"}, "api")

        # Mock the planner to return a canned plan
        mock_plan = {
            "summary": "Login page implementation",
            "phases": [{"name": "Core", "tasks": [
                {"title": "Create login form", "task_type": "code", "complexity": "simple"},
            ]}],
        }

        with patch("gods.handlers.athena._generate_plan", new_callable=AsyncMock) as mock_gen, \
             patch("gods.handlers.athena._review_plan", new_callable=AsyncMock) as mock_rev:

            mock_gen.return_value = {
                "plan_id": "plan-1",
                "plan": mock_plan,
            }
            # Review passes
            mock_rev.return_value = MagicMock(
                has_gaps=False, confidence=0.9, gaps=[], feedback="",
            )

            emits = await athena_plan(event, plan_db)

        assert emits is not None
        assert len(emits) >= 1

        # Should emit project_planned
        planned = next((e for e in emits if e.event_type == "project_planned"), None)
        assert planned is not None
        assert planned.payload["project_id"] == "proj-1"
        assert planned.payload["plan_id"] == "plan-1"

    @pytest.mark.asyncio
    async def test_review_triggers_regeneration(self, plan_db):
        """Review finds gaps → planner called again with feedback."""
        event = Event("project_created", {"project_id": "proj-1"}, "api")

        call_count = {"gen": 0}

        async def fake_generate(project_id, db, **kwargs):
            call_count["gen"] += 1
            return {
                "plan_id": f"plan-{call_count['gen']}",
                "plan": {"summary": "test", "phases": [{"name": "P", "tasks": [{"title": "t"}]}]},
            }

        async def fake_review(plan, requirements, project_name="", **kwargs):
            # First call: gaps found. Second call: passes.
            if call_count["gen"] <= 1:
                return MagicMock(
                    has_gaps=True, confidence=0.3,
                    gaps=["Missing error handling"],
                    feedback="Add error handling tasks",
                )
            return MagicMock(has_gaps=False, confidence=0.9, gaps=[], feedback="")

        with patch("gods.handlers.athena._generate_plan", side_effect=fake_generate), \
             patch("gods.handlers.athena._review_plan", side_effect=fake_review):

            emits = await athena_plan(event, plan_db)

        # Planner should have been called twice (original + regeneration)
        assert call_count["gen"] == 2

        # Final emit should be project_planned with the second plan
        planned = next((e for e in emits if e.event_type == "project_planned"), None)
        assert planned is not None
        assert planned.payload["plan_id"] == "plan-2"

    @pytest.mark.asyncio
    async def test_max_review_iterations(self, plan_db):
        """Review always finds gaps → capped at max iterations, emits anyway."""
        event = Event("project_created", {"project_id": "proj-1"}, "api")
        call_count = {"gen": 0}

        async def fake_generate(project_id, db, **kwargs):
            call_count["gen"] += 1
            return {
                "plan_id": f"plan-{call_count['gen']}",
                "plan": {"summary": "test", "tasks": [{"title": "t"}]},
            }

        async def fake_review(plan, requirements, **kwargs):
            return MagicMock(has_gaps=True, confidence=0.2, gaps=["Still bad"], feedback="Fix it")

        with patch("gods.handlers.athena._generate_plan", side_effect=fake_generate), \
             patch("gods.handlers.athena._review_plan", side_effect=fake_review):

            emits = await athena_plan(event, plan_db)

        # Should cap at max iterations (default 2: generate + 1 regen)
        assert call_count["gen"] <= 3
        # Should still emit project_planned (best effort)
        planned = next((e for e in emits if e.event_type == "project_planned"), None)
        assert planned is not None

    @pytest.mark.asyncio
    async def test_planner_failure_emits_error(self, plan_db):
        """Planner crashes → handler_error-style emit, no project_planned."""
        event = Event("project_created", {"project_id": "proj-1"}, "api")

        with patch("gods.handlers.athena._generate_plan", side_effect=RuntimeError("LLM down")):
            emits = await athena_plan(event, plan_db)

        # Should emit planning_failed, not project_planned
        assert emits is not None
        failed = next((e for e in emits if e.event_type == "planning_failed"), None)
        assert failed is not None
        assert "LLM down" in failed.payload.get("error", "")

    @pytest.mark.asyncio
    async def test_project_not_found(self, plan_db):
        """project_id doesn't exist → error emit."""
        event = Event("project_created", {"project_id": "nonexistent"}, "api")

        with patch("gods.handlers.athena._generate_plan", side_effect=Exception("Not found")):
            emits = await athena_plan(event, plan_db)

        failed = next((e for e in emits if e.event_type == "planning_failed"), None)
        assert failed is not None

    @pytest.mark.asyncio
    async def test_review_data_included_in_emit(self, plan_db):
        """project_planned event includes review metadata."""
        event = Event("project_created", {"project_id": "proj-1"}, "api")

        with patch("gods.handlers.athena._generate_plan", new_callable=AsyncMock) as mock_gen, \
             patch("gods.handlers.athena._review_plan", new_callable=AsyncMock) as mock_rev:

            mock_gen.return_value = {"plan_id": "plan-1", "plan": {"tasks": [{"title": "t"}]}}
            mock_rev.return_value = MagicMock(
                has_gaps=False, confidence=0.85, gaps=[], feedback="",
            )

            emits = await athena_plan(event, plan_db)

        planned = next((e for e in emits if e.event_type == "project_planned"), None)
        review = planned.payload.get("review")
        assert review is not None
        assert review["confidence"] == 0.85

    @pytest.mark.asyncio
    async def test_gate_feedback_triggers_regeneration(self, plan_db):
        """If handler receives _gate_feedback, uses it as review comment."""
        event = Event("project_created", {
            "project_id": "proj-1",
            "_gate_feedback": "Plan has no tasks, phases, or epics",
            "_gate_attempt": 2,
        }, "api")

        gen_kwargs = {}

        async def fake_generate(project_id, db, **kwargs):
            gen_kwargs.update(kwargs)
            return {"plan_id": "plan-retry", "plan": {"tasks": [{"title": "t"}]}}

        with patch("gods.handlers.athena._generate_plan", side_effect=fake_generate), \
             patch("gods.handlers.athena._review_plan", new_callable=AsyncMock) as mock_rev:
            mock_rev.return_value = MagicMock(has_gaps=False, confidence=0.9, gaps=[], feedback="")
            emits = await athena_plan(event, plan_db)

        # Gate feedback should be passed as comments to the planner
        assert gen_kwargs.get("comments") is not None


# ---------------------------------------------------------------------------
# athena_reassess — wave reassessment
# ---------------------------------------------------------------------------

class TestAthenaReassess:
    @pytest.mark.asyncio
    async def test_continue_as_planned(self, plan_db):
        event = Event("wave_complete", {
            "project_id": "proj-1",
            "wave": 0,
        }, "odin")

        with patch("gods.handlers.athena._reassess_wave", new_callable=AsyncMock) as mock:
            mock.return_value = MagicMock(
                outcome="continue_as_planned",
                rationale="All tasks passed",
            )
            emits = await athena_reassess(event, plan_db)

        # Should emit wave_assessed with continue
        assessed = next((e for e in emits if e.event_type == "wave_assessed"), None)
        assert assessed is not None
        assert assessed.payload["outcome"] == "continue_as_planned"

    @pytest.mark.asyncio
    async def test_replan_remaining(self, plan_db):
        event = Event("wave_complete", {"project_id": "proj-1", "wave": 0}, "odin")

        with patch("gods.handlers.athena._reassess_wave", new_callable=AsyncMock) as mock:
            mock.return_value = MagicMock(
                outcome="replan_remaining",
                rationale="New info invalidates plan",
                suggested_changes=["Add caching layer"],
            )
            emits = await athena_reassess(event, plan_db)

        assessed = next((e for e in emits if e.event_type == "wave_assessed"), None)
        assert assessed.payload["outcome"] == "replan_remaining"

    @pytest.mark.asyncio
    async def test_escalate_to_human(self, plan_db):
        event = Event("wave_complete", {"project_id": "proj-1", "wave": 0}, "odin")

        with patch("gods.handlers.athena._reassess_wave", new_callable=AsyncMock) as mock:
            mock.return_value = MagicMock(
                outcome="escalate_to_human",
                rationale="Critical failures",
            )
            emits = await athena_reassess(event, plan_db)

        assessed = next((e for e in emits if e.event_type == "wave_assessed"), None)
        assert assessed.payload["outcome"] == "escalate_to_human"
