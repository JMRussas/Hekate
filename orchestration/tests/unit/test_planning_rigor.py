#  Orchestration Engine - Planning Rigor Tests
#
#  Tests for planning rigor levels (L1/L2/L3): prompt selection,
#  max_tokens, decomposer phase flattening, and phase-aware task creation.
#
#  Depends on: backend/services/planner.py, backend/services/decomposer.py,
#              backend/db/connection.py
#  Used by:    pytest

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.enums import PlanningRigor
from backend.services.planner import (
    PlannerService,
    _build_system_prompt,
    _MAX_TOKENS_BY_RIGOR,
    _RIGOR_SUFFIXES,
)
from backend.services.decomposer import _flatten_plan_tasks, decompose_plan


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

class TestBuildSystemPrompt:

    def test_l1_prompt_contains_flat_tasks(self):
        prompt = _build_system_prompt(PlanningRigor.L1)
        assert '"tasks"' in prompt
        assert '"phases"' not in prompt

    def test_l2_prompt_contains_phases_and_questions(self):
        prompt = _build_system_prompt(PlanningRigor.L2)
        assert '"phases"' in prompt
        assert '"open_questions"' in prompt
        assert '"risk_assessment"' not in prompt

    def test_l3_prompt_contains_risk_and_test_strategy(self):
        prompt = _build_system_prompt(PlanningRigor.L3)
        assert '"phases"' in prompt
        assert '"risk_assessment"' in prompt
        assert '"test_strategy"' in prompt

    def test_all_rigor_levels_have_suffix(self):
        for rigor in PlanningRigor:
            assert rigor in _RIGOR_SUFFIXES
            prompt = _build_system_prompt(rigor)
            assert len(prompt) > 100

    def test_max_tokens_increase_with_rigor(self):
        assert _MAX_TOKENS_BY_RIGOR[PlanningRigor.L1] < _MAX_TOKENS_BY_RIGOR[PlanningRigor.L2]
        assert _MAX_TOKENS_BY_RIGOR[PlanningRigor.L2] < _MAX_TOKENS_BY_RIGOR[PlanningRigor.L3]


# ---------------------------------------------------------------------------
# _flatten_plan_tasks
# ---------------------------------------------------------------------------

class TestFlattenPlanTasks:

    def test_flat_plan_returns_tasks_with_none_phases(self):
        plan = {
            "summary": "Flat plan",
            "tasks": [
                {"title": "A", "description": "Do A"},
                {"title": "B", "description": "Do B"},
            ],
        }
        tasks, phases = _flatten_plan_tasks(plan)
        assert len(tasks) == 2
        assert all(p is None for p in phases)

    def test_phased_plan_concatenates_tasks(self):
        plan = {
            "summary": "Phased plan",
            "phases": [
                {"name": "Foundation", "description": "Setup", "tasks": [
                    {"title": "A", "description": "Do A"},
                ]},
                {"name": "Implementation", "description": "Build", "tasks": [
                    {"title": "B", "description": "Do B"},
                    {"title": "C", "description": "Do C"},
                ]},
            ],
        }
        tasks, phases = _flatten_plan_tasks(plan)
        assert len(tasks) == 3
        assert tasks[0]["title"] == "A"
        assert tasks[1]["title"] == "B"
        assert tasks[2]["title"] == "C"

    def test_phased_plan_returns_phase_names(self):
        plan = {
            "summary": "Phased",
            "phases": [
                {"name": "Phase 1", "description": "First", "tasks": [
                    {"title": "A", "description": "Do A"},
                ]},
                {"name": "Phase 2", "description": "Second", "tasks": [
                    {"title": "B", "description": "Do B"},
                ]},
            ],
        }
        tasks, phases = _flatten_plan_tasks(plan)
        assert phases == ["Phase 1", "Phase 2"]

    def test_empty_phases_falls_back_to_flat(self):
        plan = {
            "summary": "Empty phases",
            "phases": [],
            "tasks": [{"title": "A", "description": "Do A"}],
        }
        tasks, phases = _flatten_plan_tasks(plan)
        assert len(tasks) == 1
        assert phases == [None]

    def test_no_tasks_or_phases_returns_empty(self):
        plan = {"summary": "Nothing"}
        tasks, phases = _flatten_plan_tasks(plan)
        assert len(tasks) == 0
        assert len(phases) == 0

    def test_unnamed_phase_gets_default_name(self):
        plan = {
            "summary": "Unnamed",
            "phases": [
                {"description": "No name", "tasks": [
                    {"title": "A", "description": "Do A"},
                ]},
            ],
        }
        tasks, phases = _flatten_plan_tasks(plan)
        assert phases == ["Unnamed Phase"]

    def test_global_dependency_indexing_preserved(self):
        """Phased plan: depends_on indices are global across phases."""
        plan = {
            "summary": "Cross-phase deps",
            "phases": [
                {"name": "P1", "description": "First", "tasks": [
                    {"title": "A", "depends_on": [], "description": ""},
                    {"title": "B", "depends_on": [0], "description": ""},
                ]},
                {"name": "P2", "description": "Second", "tasks": [
                    {"title": "C", "depends_on": [1], "description": ""},
                ]},
            ],
        }
        tasks, phases = _flatten_plan_tasks(plan)
        assert len(tasks) == 3
        # Task C (index 2) depends on B (index 1) — global index preserved
        assert tasks[2]["depends_on"] == [1]


# ---------------------------------------------------------------------------
# PlannerService rigor from project config
# ---------------------------------------------------------------------------

def _make_plan_response(plan_text=None, pt=100, ct=200):
    if plan_text is None:
        plan_text = json.dumps({
            "summary": "Test plan",
            "tasks": [{"title": "T1", "description": "Do it", "task_type": "code",
                        "complexity": "simple", "depends_on": [], "tools_needed": []}],
        })
    response = MagicMock()
    response.content = [MagicMock(text=plan_text, type="text")]
    response.usage = MagicMock(input_tokens=pt, output_tokens=ct)
    return response


@pytest.fixture
async def rigor_db(tmp_db):
    """Database with projects at different rigor levels."""
    now = time.time()
    for pid, rigor in [("proj_l1", "L1"), ("proj_l2", "L2"), ("proj_l3", "L3"), ("proj_none", None)]:
        config = {"planning_rigor": rigor} if rigor else {}
        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, config_json, created_at, updated_at) "
            "VALUES (?, ?, ?, 'draft', ?, ?, ?)",
            (pid, f"Project {pid}", "Build something", json.dumps(config), now, now),
        )
    return tmp_db


class TestPlannerRigorConfig:

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_l1_uses_flat_prompt(self, _mock_cost, rigor_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_plan_response())

        svc = PlannerService(db=rigor_db, budget=mock_budget)
        await svc.generate("proj_l1", client=mock_client)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        system = call_kwargs["system"]
        assert '"tasks"' in system
        assert '"phases"' not in system
        assert call_kwargs["max_tokens"] == _MAX_TOKENS_BY_RIGOR[PlanningRigor.L1]

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_l3_uses_thorough_prompt(self, _mock_cost, rigor_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_plan_response())

        svc = PlannerService(db=rigor_db, budget=mock_budget)
        await svc.generate("proj_l3", client=mock_client)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        system = call_kwargs["system"]
        assert '"risk_assessment"' in system
        assert '"test_strategy"' in system
        assert call_kwargs["max_tokens"] == _MAX_TOKENS_BY_RIGOR[PlanningRigor.L3]

    @patch("backend.services.planner.calculate_cost", return_value=0.01)
    @patch("backend.services.planner.PLANNING_MODEL", "test-model")
    async def test_missing_rigor_defaults_to_l2(self, _mock_cost, rigor_db):
        mock_budget = AsyncMock()
        mock_budget.reserve_spend = AsyncMock(return_value=True)
        mock_budget.record_spend = AsyncMock()
        mock_budget.release_reservation = AsyncMock()

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=_make_plan_response())

        svc = PlannerService(db=rigor_db, budget=mock_budget)
        await svc.generate("proj_none", client=mock_client)

        call_kwargs = mock_client.messages.create.call_args.kwargs
        system = call_kwargs["system"]
        assert '"phases"' in system
        assert '"open_questions"' in system
        assert call_kwargs["max_tokens"] == _MAX_TOKENS_BY_RIGOR[PlanningRigor.L2]


# ---------------------------------------------------------------------------
# Decomposer with phased plan
# ---------------------------------------------------------------------------

@pytest.fixture
async def phased_db(tmp_db):
    """Database with a phased plan (L2/L3 style)."""
    now = time.time()
    project_id = "proj_phased"
    plan_id = "plan_phased"

    config = {"planning_rigor": "L2"}
    await tmp_db.execute_write(
        "INSERT INTO projects (id, name, requirements, status, config_json, created_at, updated_at) "
        "VALUES (?, ?, ?, 'draft', ?, ?, ?)",
        (project_id, "Phased Project", "Build X\nDo Y", json.dumps(config), now, now),
    )

    plan_data = {
        "summary": "Phased test plan",
        "phases": [
            {
                "name": "Setup",
                "description": "Foundation work",
                "tasks": [
                    {"title": "Task A", "description": "Setup env", "task_type": "code",
                     "complexity": "simple", "depends_on": [], "tools_needed": []},
                ],
            },
            {
                "name": "Build",
                "description": "Core implementation",
                "tasks": [
                    {"title": "Task B", "description": "Build core", "task_type": "code",
                     "complexity": "medium", "depends_on": [0], "tools_needed": []},
                    {"title": "Task C", "description": "Build extras", "task_type": "code",
                     "complexity": "simple", "depends_on": [0], "tools_needed": []},
                ],
            },
        ],
        "open_questions": [
            {"question": "Which DB?", "proposed_answer": "SQLite", "impact": "Migration needed"},
        ],
    }
    await tmp_db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test-model', ?, 'draft', ?)",
        (plan_id, project_id, json.dumps(plan_data), now),
    )

    return tmp_db, project_id, plan_id


class TestDecomposerWithPhases:

    async def test_creates_correct_task_count(self, phased_db):
        db, project_id, plan_id = phased_db
        result = await decompose_plan(project_id, plan_id, db=db)
        assert result["tasks_created"] == 3

    async def test_tasks_have_phase_set(self, phased_db):
        db, project_id, plan_id = phased_db
        result = await decompose_plan(project_id, plan_id, db=db)
        task_ids = result["task_ids"]

        task_a = await db.fetchone("SELECT phase FROM tasks WHERE id = ?", (task_ids[0],))
        assert task_a["phase"] == "Setup"

        task_b = await db.fetchone("SELECT phase FROM tasks WHERE id = ?", (task_ids[1],))
        assert task_b["phase"] == "Build"

        task_c = await db.fetchone("SELECT phase FROM tasks WHERE id = ?", (task_ids[2],))
        assert task_c["phase"] == "Build"

    async def test_cross_phase_dependencies(self, phased_db):
        """Task B (phase 2) depends on Task A (phase 1) via global index."""
        db, project_id, plan_id = phased_db
        result = await decompose_plan(project_id, plan_id, db=db)
        task_ids = result["task_ids"]

        # Task B (index 1) depends on Task A (index 0)
        deps_b = await db.fetchall(
            "SELECT depends_on FROM task_deps WHERE task_id = ?", (task_ids[1],)
        )
        assert len(deps_b) == 1
        assert deps_b[0]["depends_on"] == task_ids[0]

        # Task C (index 2) also depends on Task A (index 0)
        deps_c = await db.fetchall(
            "SELECT depends_on FROM task_deps WHERE task_id = ?", (task_ids[2],)
        )
        assert len(deps_c) == 1
        assert deps_c[0]["depends_on"] == task_ids[0]

    async def test_phase_context_injected(self, phased_db):
        """Tasks should have their phase name in context_json."""
        db, project_id, plan_id = phased_db
        result = await decompose_plan(project_id, plan_id, db=db)
        task_ids = result["task_ids"]

        task_b = await db.fetchone("SELECT context_json FROM tasks WHERE id = ?", (task_ids[1],))
        context = json.loads(task_b["context_json"])
        phase_entries = [c for c in context if c["type"] == "phase"]
        assert len(phase_entries) == 1
        assert phase_entries[0]["content"] == "Build"

    async def test_flat_plan_no_phase_on_tasks(self, seeded_db):
        """Flat plan (L1 style): tasks should have NULL phase."""
        db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=db)
        task_ids = result["task_ids"]

        for tid in task_ids:
            task = await db.fetchone("SELECT phase FROM tasks WHERE id = ?", (tid,))
            assert task["phase"] is None

    async def test_wave_computation_with_phases(self, phased_db):
        """Cross-phase dependencies should produce correct wave numbers."""
        db, project_id, plan_id = phased_db
        result = await decompose_plan(project_id, plan_id, db=db)
        task_ids = result["task_ids"]

        task_a = await db.fetchone("SELECT wave FROM tasks WHERE id = ?", (task_ids[0],))
        assert task_a["wave"] == 0  # No deps

        task_b = await db.fetchone("SELECT wave FROM tasks WHERE id = ?", (task_ids[1],))
        assert task_b["wave"] == 1  # Depends on A

        task_c = await db.fetchone("SELECT wave FROM tasks WHERE id = ?", (task_ids[2],))
        assert task_c["wave"] == 1  # Depends on A


# ---------------------------------------------------------------------------
# Edge cases for _flatten_plan_tasks
# ---------------------------------------------------------------------------

class TestFlattenPlanTasksEdgeCases:

    def test_non_dict_phase_entries_are_skipped(self):
        """If Claude returns a non-dict in phases list, skip it gracefully."""
        plan = {
            "summary": "Bad phases",
            "phases": [
                "not a dict",
                {"name": "Real Phase", "description": "OK", "tasks": [
                    {"title": "A", "description": "Do A"},
                ]},
            ],
        }
        tasks, phases = _flatten_plan_tasks(plan)
        assert len(tasks) == 1
        assert phases == ["Real Phase"]

    def test_phase_with_no_tasks_key(self):
        """Phase dict without 'tasks' key produces no tasks from that phase."""
        plan = {
            "summary": "Missing tasks key",
            "phases": [
                {"name": "Empty", "description": "No tasks key"},
                {"name": "Full", "description": "Has tasks", "tasks": [
                    {"title": "A", "description": "Do A"},
                ]},
            ],
        }
        tasks, phases = _flatten_plan_tasks(plan)
        assert len(tasks) == 1
        assert phases == ["Full"]
