#  Orchestration Engine - Decomposer Tests
#
#  Tests for plan decomposition into executable tasks.
#
#  Depends on: backend/services/decomposer.py, backend/db/connection.py
#  Used by:    pytest

import json

import pytest

from backend.exceptions import InvalidStateError, NotFoundError
from backend.models.enums import PlanStatus, ProjectStatus, TaskStatus
from backend.services.decomposer import decompose_plan


class TestDecomposePlan:
    async def test_creates_correct_task_count(self, seeded_db):
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        assert result["tasks_created"] == 2
        assert len(result["task_ids"]) == 2

    async def test_creates_dependency_edges(self, seeded_db):
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        # Task B (index 1) depends on Task A (index 0)
        deps = await tmp_db.fetchall(
            "SELECT * FROM task_deps WHERE task_id = ?", (task_ids[1],)
        )
        assert len(deps) == 1
        assert deps[0]["depends_on"] == task_ids[0]

    async def test_marks_plan_approved(self, seeded_db):
        tmp_db, project_id, plan_id = seeded_db
        await decompose_plan(project_id, plan_id, db=tmp_db)
        plan_row = await tmp_db.fetchone("SELECT status FROM plans WHERE id = ?", (plan_id,))
        assert plan_row["status"] == PlanStatus.APPROVED

    async def test_updates_project_to_ready(self, seeded_db):
        tmp_db, project_id, plan_id = seeded_db
        await decompose_plan(project_id, plan_id, db=tmp_db)
        proj_row = await tmp_db.fetchone("SELECT status FROM projects WHERE id = ?", (project_id,))
        assert proj_row["status"] == ProjectStatus.READY

    async def test_blocked_tasks_have_correct_status(self, seeded_db):
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        # Task A has no deps → should be pending
        task_a = await tmp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_ids[0],))
        assert task_a["status"] == TaskStatus.PENDING

        # Task B depends on Task A → should be blocked
        task_b = await tmp_db.fetchone("SELECT status FROM tasks WHERE id = ?", (task_ids[1],))
        assert task_b["status"] == TaskStatus.BLOCKED

    async def test_handles_string_dep_indices(self, seeded_db):
        """Claude sometimes returns dep indices as strings — decomposer must handle both."""
        tmp_db, project_id, plan_id = seeded_db

        # Overwrite the plan with string indices
        plan_data = {
            "summary": "String index plan",
            "tasks": [
                {"title": "A", "description": "Do A", "task_type": "code",
                 "complexity": "simple", "depends_on": [], "tools_needed": []},
                {"title": "B", "description": "Do B", "task_type": "code",
                 "complexity": "simple", "depends_on": ["0"], "tools_needed": []},
            ],
        }
        await tmp_db.execute_write(
            "UPDATE plans SET plan_json = ?, status = 'draft' WHERE id = ?",
            (json.dumps(plan_data), plan_id),
        )

        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        deps = await tmp_db.fetchall(
            "SELECT * FROM task_deps WHERE task_id = ?", (task_ids[1],)
        )
        assert len(deps) == 1

    async def test_raises_on_missing_plan(self, seeded_db):
        tmp_db, project_id, _ = seeded_db
        with pytest.raises(NotFoundError, match="not found"):
            await decompose_plan(project_id, "nonexistent_plan", db=tmp_db)

    async def test_raises_on_empty_plan(self, seeded_db):
        tmp_db, project_id, plan_id = seeded_db

        # Overwrite plan with empty task list
        await tmp_db.execute_write(
            "UPDATE plans SET plan_json = ? WHERE id = ?",
            (json.dumps({"summary": "Empty", "tasks": []}), plan_id),
        )

        with pytest.raises(InvalidStateError, match="no tasks"):
            await decompose_plan(project_id, plan_id, db=tmp_db)
