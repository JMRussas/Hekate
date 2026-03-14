#  Orchestration Engine - E2E Workflow Test
#
#  Full workflow: register → create project → plan (mocked) → approve → execute.
#  Verifies the entire pipeline works end-to-end.
#
#  Depends on: all backend modules, tests/conftest.py, backend/services/llm_router.py
#  Used by:    pytest

import json
from unittest.mock import AsyncMock, patch

from backend.services.llm_router import LLMResponse


class TestFullWorkflow:
    async def test_register_create_plan_approve_execute(self, authed_client, tmp_db):
        """E2E: register user → create project → generate plan → approve → execute."""

        # Step 1: Create a project
        resp = await authed_client.post("/api/projects", json={
            "name": "E2E Test Project",
            "requirements": "Build a simple web scraper that extracts headlines from news sites.",
        })
        assert resp.status_code == 201
        project = resp.json()
        project_id = project["id"]
        assert project["status"] == "draft"

        # Step 2: Generate a plan (mock call_llm)
        plan_json = json.dumps({
            "summary": "Build web scraper in 2 steps",
            "tasks": [
                {
                    "title": "Research scraping libraries",
                    "description": "Evaluate BeautifulSoup vs Scrapy",
                    "task_type": "research",
                    "complexity": "simple",
                    "depends_on": [],
                    "tools_needed": ["search_knowledge"],
                },
                {
                    "title": "Implement scraper",
                    "description": "Write the scraper code",
                    "task_type": "code",
                    "complexity": "medium",
                    "depends_on": [0],
                    "tools_needed": ["write_file"],
                },
            ],
        })

        with patch("backend.services.planner.call_llm", new_callable=AsyncMock) as mock_call_llm:
            mock_call_llm.return_value = LLMResponse(
                text=plan_json, provider="test", model="test-model",
            )
            resp = await authed_client.post(f"/api/projects/{project_id}/plan")
            assert resp.status_code == 200
            plan_result = resp.json()
            plan_id = plan_result["plan_id"]

        # Verify project is back to "draft" (plan generated, awaiting approval)
        resp = await authed_client.get(f"/api/projects/{project_id}")
        assert resp.json()["status"] == "draft"

        # Step 3: Verify plan was created
        resp = await authed_client.get(f"/api/projects/{project_id}/plans")
        assert resp.status_code == 200
        plans = resp.json()
        assert len(plans) == 1
        assert plans[0]["status"] == "draft"

        # Step 4: Approve the plan (decomposes into tasks)
        resp = await authed_client.post(
            f"/api/projects/{project_id}/plans/{plan_id}/approve"
        )
        assert resp.status_code == 200
        approve_result = resp.json()
        assert approve_result["tasks_created"] == 2

        # Verify tasks were created
        resp = await authed_client.get(f"/api/tasks/project/{project_id}")
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 2

        # First task (no deps) should be pending
        research_task = next(t for t in tasks if t["title"] == "Research scraping libraries")
        assert research_task["status"] == "pending"

        # Second task (depends on first) should be blocked
        code_task = next(t for t in tasks if t["title"] == "Implement scraper")
        assert code_task["status"] == "blocked"
        assert len(code_task["depends_on"]) == 1

        # Verify project is now "ready"
        resp = await authed_client.get(f"/api/projects/{project_id}")
        assert resp.json()["status"] == "ready"

        # Step 5: Start execution
        resp = await authed_client.post(f"/api/projects/{project_id}/execute")
        assert resp.status_code == 200
        assert resp.json()["status"] == "executing"

        # Verify project is now "executing"
        resp = await authed_client.get(f"/api/projects/{project_id}")
        assert resp.json()["status"] == "executing"

        # Step 6: Pause execution
        resp = await authed_client.post(f"/api/projects/{project_id}/pause")
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused"

        # Step 7: Cancel project
        resp = await authed_client.post(f"/api/projects/{project_id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        # Verify pending/blocked tasks were cancelled
        resp = await authed_client.get(f"/api/tasks/project/{project_id}")
        tasks = resp.json()
        for t in tasks:
            assert t["status"] == "cancelled"

    async def test_budget_records_plan_cost(self, authed_client, tmp_db):
        """Verify that plan generation records an audit entry in the budget system.

        CLI providers report $0 cost and 0 tokens (subscription billing).
        The test verifies the usage_log entry exists with purpose=plan_generation.
        """
        # Create project
        resp = await authed_client.post("/api/projects", json={
            "name": "Budget Test", "requirements": "Test budget tracking",
        })
        project_id = resp.json()["id"]

        # Mock call_llm
        plan_json = json.dumps({
            "summary": "Simple plan",
            "tasks": [{
                "title": "T1", "description": "D1",
                "task_type": "code", "complexity": "simple",
                "depends_on": [], "tools_needed": [],
            }],
        })

        with patch("backend.services.planner.call_llm", new_callable=AsyncMock) as mock_call_llm:
            mock_call_llm.return_value = LLMResponse(
                text=plan_json, provider="test", model="test-model",
            )
            await authed_client.post(f"/api/projects/{project_id}/plan")

        # Check usage summary reflects the plan generation audit entry
        resp = await authed_client.get("/api/usage/summary")
        data = resp.json()
        assert data["api_call_count"] >= 1

        # Verify the actual usage_log row has correct purpose and provider
        log_row = await tmp_db.fetchone(
            "SELECT purpose, provider, cost_usd FROM usage_log WHERE project_id = ?",
            (project_id,),
        )
        assert log_row is not None
        assert log_row["purpose"] == "plan_generation"
        assert log_row["provider"] == "test"
        assert log_row["cost_usd"] == 0.0
