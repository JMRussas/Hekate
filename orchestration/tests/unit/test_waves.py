#  Orchestration Engine - Wave Computation Tests
#
#  Tests for wave-based execution: wave assignment during decomposition
#  and wave-filtered dispatch in the executor.
#
#  Depends on: backend/services/decomposer.py, backend/services/executor.py
#  Used by:    pytest

import json
import time


from backend.models.enums import TaskStatus
from backend.services.decomposer import _compute_waves, decompose_plan


# ---------------------------------------------------------------------------
# _compute_waves unit tests (pure function, no DB needed)
# ---------------------------------------------------------------------------

class TestComputeWaves:
    def test_empty_task_list(self):
        assert _compute_waves([]) == []

    def test_single_task(self):
        tasks = [{"title": "A", "depends_on": []}]
        assert _compute_waves(tasks) == [0]

    def test_all_independent(self):
        tasks = [
            {"title": "A", "depends_on": []},
            {"title": "B", "depends_on": []},
            {"title": "C", "depends_on": []},
        ]
        assert _compute_waves(tasks) == [0, 0, 0]

    def test_linear_chain(self):
        """A → B → C produces waves [0, 1, 2]."""
        tasks = [
            {"title": "A", "depends_on": []},
            {"title": "B", "depends_on": [0]},
            {"title": "C", "depends_on": [1]},
        ]
        assert _compute_waves(tasks) == [0, 1, 2]

    def test_diamond_pattern(self):
        """A → B, A → C, B+C → D produces waves [0, 1, 1, 2]."""
        tasks = [
            {"title": "A", "depends_on": []},
            {"title": "B", "depends_on": [0]},
            {"title": "C", "depends_on": [0]},
            {"title": "D", "depends_on": [1, 2]},
        ]
        assert _compute_waves(tasks) == [0, 1, 1, 2]

    def test_wide_then_merge(self):
        """Three independent tasks merged by a fourth."""
        tasks = [
            {"title": "A", "depends_on": []},
            {"title": "B", "depends_on": []},
            {"title": "C", "depends_on": []},
            {"title": "D", "depends_on": [0, 1, 2]},
        ]
        assert _compute_waves(tasks) == [0, 0, 0, 1]

    def test_string_dep_indices(self):
        """Claude may return dep indices as strings."""
        tasks = [
            {"title": "A", "depends_on": []},
            {"title": "B", "depends_on": ["0"]},
        ]
        assert _compute_waves(tasks) == [0, 1]

    def test_ignores_invalid_deps(self):
        """Out-of-range, self-referencing, and non-integer deps are ignored."""
        tasks = [
            {"title": "A", "depends_on": [99, -1, "invalid", 0]},
            {"title": "B", "depends_on": []},
        ]
        # Self-dep (0→0) is ignored, so A stays wave 0
        assert _compute_waves(tasks) == [0, 0]

    def test_complex_dag(self):
        """
        A(0)  B(1)
        |  \\ /
        C(2) D(3)
            |
            E(4)
        """
        tasks = [
            {"title": "A", "depends_on": []},      # wave 0
            {"title": "B", "depends_on": []},      # wave 0
            {"title": "C", "depends_on": [0]},     # wave 1
            {"title": "D", "depends_on": [0, 1]},  # wave 1
            {"title": "E", "depends_on": [3]},     # wave 2
        ]
        assert _compute_waves(tasks) == [0, 0, 1, 1, 2]


# ---------------------------------------------------------------------------
# Wave assignment during decomposition (integration with DB)
# ---------------------------------------------------------------------------

class TestDecomposeWaves:
    async def test_wave_assigned_on_decomposition(self, seeded_db):
        """The seeded plan has A→B, so A=wave 0, B=wave 1."""
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        task_a = await tmp_db.fetchone("SELECT wave FROM tasks WHERE id = ?", (task_ids[0],))
        task_b = await tmp_db.fetchone("SELECT wave FROM tasks WHERE id = ?", (task_ids[1],))

        assert task_a["wave"] == 0
        assert task_b["wave"] == 1

    async def test_total_waves_in_result(self, seeded_db):
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        assert result["total_waves"] == 2

    async def test_independent_tasks_share_wave(self, tmp_db):
        """All-independent tasks get wave 0."""
        now = time.time()
        project_id = "proj_wave_001"
        plan_id = "plan_wave_001"

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'draft', ?, ?)",
            (project_id, "Wave Test", "Test waves", now, now),
        )

        plan_data = {
            "summary": "Independent tasks",
            "tasks": [
                {"title": "A", "description": "Do A", "task_type": "code",
                 "complexity": "simple", "depends_on": [], "tools_needed": []},
                {"title": "B", "description": "Do B", "task_type": "code",
                 "complexity": "simple", "depends_on": [], "tools_needed": []},
                {"title": "C", "description": "Do C", "task_type": "code",
                 "complexity": "simple", "depends_on": [], "tools_needed": []},
            ],
        }
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'draft', ?)",
            (plan_id, project_id, json.dumps(plan_data), now),
        )

        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        for tid in task_ids:
            row = await tmp_db.fetchone("SELECT wave FROM tasks WHERE id = ?", (tid,))
            assert row["wave"] == 0

        assert result["total_waves"] == 1


# ---------------------------------------------------------------------------
# Wave-filtered dispatch in executor
# ---------------------------------------------------------------------------

class TestWaveDispatch:
    async def test_wave_filters_ready_tasks(self, seeded_db):
        """The ready-task query with wave filter should only return wave 0 tasks."""
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        # Task B is blocked (depends on A). Manually unblock it to test wave filtering.
        # Both tasks will be PENDING, but only wave 0 should be returned.
        await tmp_db.execute_write(
            "UPDATE tasks SET status = ? WHERE id = ?",
            (TaskStatus.PENDING, task_ids[1]),
        )

        # Determine current wave
        wave_row = await tmp_db.fetchone(
            "SELECT MIN(wave) as w FROM tasks "
            "WHERE project_id = ? AND status NOT IN (?, ?, ?)",
            (project_id, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED),
        )
        current_wave = wave_row["w"]
        assert current_wave == 0

        # Query ready tasks filtered by wave (same query the executor uses)
        ready = await tmp_db.fetchall(
            "SELECT t.* FROM tasks t "
            "LEFT JOIN task_deps d ON d.task_id = t.id "
            "LEFT JOIN tasks dep ON dep.id = d.depends_on AND dep.status != ? "
            "WHERE t.project_id = ? AND t.status = ? AND t.wave = ? "
            "GROUP BY t.id HAVING COUNT(dep.id) = 0 "
            "ORDER BY t.priority ASC",
            (TaskStatus.COMPLETED, project_id, TaskStatus.PENDING, current_wave),
        )

        # Only Task A (wave 0) should be ready (B is wave 1)
        assert len(ready) == 1
        assert ready[0]["id"] == task_ids[0]
        assert ready[0]["wave"] == 0

    async def test_wave_advances_after_completion(self, seeded_db):
        """After wave 0 tasks complete, wave 1 becomes the current wave."""
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        # Complete Task A (wave 0)
        await tmp_db.execute_write(
            "UPDATE tasks SET status = ? WHERE id = ?",
            (TaskStatus.COMPLETED, task_ids[0]),
        )

        # Current wave should now be 1
        wave_row = await tmp_db.fetchone(
            "SELECT MIN(wave) as w FROM tasks "
            "WHERE project_id = ? AND status NOT IN (?, ?, ?)",
            (project_id, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED),
        )
        assert wave_row["w"] == 1

    async def test_all_waves_complete_returns_none(self, seeded_db):
        """When all tasks are terminal, MIN(wave) returns None."""
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        # Complete both tasks
        for tid in task_ids:
            await tmp_db.execute_write(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (TaskStatus.COMPLETED, tid),
            )

        wave_row = await tmp_db.fetchone(
            "SELECT MIN(wave) as w FROM tasks "
            "WHERE project_id = ? AND status NOT IN (?, ?, ?)",
            (project_id, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED),
        )
        assert wave_row["w"] is None
