#  Orchestration Engine - E2E Workflow Test
#
#  Full workflow: register → create project → plan (mocked) → approve → execute.
#  Verifies the entire pipeline works end-to-end.
#
#  Depends on: all backend modules, tests/conftest.py
#  Used by:    pytest

import json
from unittest.mock import AsyncMock, MagicMock, patch



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

        # Step 2: Generate a plan (mock Claude)
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps({
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
                }),
                type="text",
            )
        ]
        mock_response.usage = MagicMock(input_tokens=500, output_tokens=300)

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
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
        """Verify that plan generation records cost in the budget system."""
        # Create project
        resp = await authed_client.post("/api/projects", json={
            "name": "Budget Test", "requirements": "Test budget tracking",
        })
        project_id = resp.json()["id"]

        # Mock Claude response
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps({
                    "summary": "Simple plan",
                    "tasks": [{
                        "title": "T1", "description": "D1",
                        "task_type": "code", "complexity": "simple",
                        "depends_on": [], "tools_needed": [],
                    }],
                }),
                type="text",
            )
        ]
        mock_response.usage = MagicMock(input_tokens=1000, output_tokens=500)
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            await authed_client.post(f"/api/projects/{project_id}/plan")

        # Check usage summary reflects the plan generation cost
        resp = await authed_client.get("/api/usage/summary")
        data = resp.json()
        assert data["api_call_count"] >= 1
        assert data["total_prompt_tokens"] >= 1000
        assert data["total_completion_tokens"] >= 500
