#  Orchestration Engine - Richer Context & Traceability Tests
#
#  Tests for enriched task context and requirement ID storage.
#  Coverage endpoint tests are in tests/integration/test_coverage_api.py.
#
#  Depends on: backend/services/decomposer.py
#  Used by:    pytest

import json
import time


from backend.services.decomposer import decompose_plan


# ---------------------------------------------------------------------------
# Enriched context at decomposition time
# ---------------------------------------------------------------------------

class TestEnrichedContext:
    async def test_context_includes_project_requirements(self, seeded_db):
        """Task context should include the full project requirements."""
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        row = await tmp_db.fetchone("SELECT context_json FROM tasks WHERE id = ?", (task_ids[0],))
        ctx = json.loads(row["context_json"])

        req_entries = [e for e in ctx if e.get("type") == "project_requirements"]
        assert len(req_entries) == 1
        assert "test thing" in req_entries[0]["content"].lower()

    async def test_context_includes_sibling_tasks(self, seeded_db):
        """Task context should list sibling tasks for awareness."""
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        # Task A should see Task B as a sibling
        row = await tmp_db.fetchone("SELECT context_json FROM tasks WHERE id = ?", (task_ids[0],))
        ctx = json.loads(row["context_json"])

        sibling_entries = [e for e in ctx if e.get("type") == "sibling_tasks"]
        assert len(sibling_entries) == 1
        assert "Task B" in sibling_entries[0]["content"]

    async def test_context_includes_verification_criteria(self, tmp_db):
        """Verification criteria from the plan should appear in context."""
        now = time.time()
        project_id = "proj_ctx_rich_001"
        plan_id = "plan_ctx_rich_001"

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'draft', ?, ?)",
            (project_id, "Rich Context", "Build widgets", now, now),
        )

        plan_data = {
            "summary": "Widget plan",
            "tasks": [
                {
                    "title": "Build Widget",
                    "description": "Create the widget component",
                    "task_type": "code",
                    "complexity": "simple",
                    "depends_on": [],
                    "tools_needed": [],
                    "verification_criteria": "Widget renders without errors and passes accessibility checks",
                },
            ],
        }
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'draft', ?)",
            (plan_id, project_id, json.dumps(plan_data), now),
        )

        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        row = await tmp_db.fetchone("SELECT context_json FROM tasks WHERE id = ?", (task_ids[0],))
        ctx = json.loads(row["context_json"])

        vc_entries = [e for e in ctx if e.get("type") == "verification_criteria"]
        assert len(vc_entries) == 1
        assert "accessibility" in vc_entries[0]["content"].lower()

    async def test_context_includes_affected_files(self, tmp_db):
        """Affected files from the plan should appear in context."""
        now = time.time()
        project_id = "proj_ctx_rich_002"
        plan_id = "plan_ctx_rich_002"

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'draft', ?, ?)",
            (project_id, "Files Context", "Update API", now, now),
        )

        plan_data = {
            "summary": "API update",
            "tasks": [
                {
                    "title": "Update API",
                    "description": "Modify the endpoints",
                    "task_type": "code",
                    "complexity": "simple",
                    "depends_on": [],
                    "tools_needed": [],
                    "affected_files": ["src/api.py", "src/models.py"],
                },
            ],
        }
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'draft', ?)",
            (plan_id, project_id, json.dumps(plan_data), now),
        )

        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        row = await tmp_db.fetchone("SELECT context_json FROM tasks WHERE id = ?", (task_ids[0],))
        ctx = json.loads(row["context_json"])

        file_entries = [e for e in ctx if e.get("type") == "affected_files"]
        assert len(file_entries) == 1
        assert "src/api.py" in file_entries[0]["content"]
        assert "src/models.py" in file_entries[0]["content"]

    async def test_no_criteria_or_files_omits_entries(self, seeded_db):
        """When plan has no criteria/files, those context entries are absent."""
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        row = await tmp_db.fetchone("SELECT context_json FROM tasks WHERE id = ?", (task_ids[0],))
        ctx = json.loads(row["context_json"])

        assert not any(e.get("type") == "verification_criteria" for e in ctx)
        assert not any(e.get("type") == "affected_files" for e in ctx)


# ---------------------------------------------------------------------------
# Requirement traceability
# ---------------------------------------------------------------------------

class TestRequirementTraceability:
    async def test_requirement_ids_stored(self, tmp_db):
        """Requirement IDs from plan are stored on task rows."""
        now = time.time()
        project_id = "proj_req_001"
        plan_id = "plan_req_001"

        await tmp_db.execute_write(
            "INSERT INTO projects (id, name, requirements, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'draft', ?, ?)",
            (project_id, "Req Test", "Build auth\nAdd logging\nWrite tests", now, now),
        )

        plan_data = {
            "summary": "Traced plan",
            "tasks": [
                {
                    "title": "Auth Task",
                    "description": "Implement auth",
                    "task_type": "code",
                    "complexity": "simple",
                    "depends_on": [],
                    "tools_needed": [],
                    "requirement_ids": ["R1"],
                },
                {
                    "title": "Logging Task",
                    "description": "Add logging",
                    "task_type": "code",
                    "complexity": "simple",
                    "depends_on": [],
                    "tools_needed": [],
                    "requirement_ids": ["R2"],
                },
            ],
        }
        await tmp_db.execute_write(
            "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
            "VALUES (?, ?, 1, 'test-model', ?, 'draft', ?)",
            (plan_id, project_id, json.dumps(plan_data), now),
        )

        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        row_a = await tmp_db.fetchone("SELECT requirement_ids_json FROM tasks WHERE id = ?", (task_ids[0],))
        row_b = await tmp_db.fetchone("SELECT requirement_ids_json FROM tasks WHERE id = ?", (task_ids[1],))

        assert json.loads(row_a["requirement_ids_json"]) == ["R1"]
        assert json.loads(row_b["requirement_ids_json"]) == ["R2"]

    async def test_empty_requirement_ids_defaults(self, seeded_db):
        """Tasks without requirement_ids get an empty list."""
        tmp_db, project_id, plan_id = seeded_db
        result = await decompose_plan(project_id, plan_id, db=tmp_db)
        task_ids = result["task_ids"]

        row = await tmp_db.fetchone("SELECT requirement_ids_json FROM tasks WHERE id = ?", (task_ids[0],))
        assert json.loads(row["requirement_ids_json"]) == []
