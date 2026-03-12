#  Orchestration Engine - External Execution Route Tests
#
#  Tests for external task claiming, result submission, and release.
#
#  Depends on: conftest.py (authed_client)
#  Used by:    CI

import json
import time



async def _setup_executing_project(client, execution_mode="external"):
    """Create a project with tasks in executing state."""
    # Create project
    resp = await client.post("/api/projects", json={
        "name": "Test Project",
        "requirements": "Build something",
        "config": {"execution_mode": execution_mode},
    })
    assert resp.status_code == 201
    project_id = resp.json()["id"]

    # We need to manually set up the project as executing with tasks
    # since the full plan+execute flow requires Anthropic API
    from backend.app import container
    db = container.db()

    now = time.time()
    plan_id = "plan_ext_test"

    await db.execute_write(
        "UPDATE projects SET status = 'executing', config_json = ? WHERE id = ?",
        (json.dumps({"execution_mode": execution_mode}), project_id),
    )
    await db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
        (plan_id, project_id, now),
    )

    # Create 3 tasks: haiku (wave 0), sonnet (wave 0), ollama (wave 0)
    for tid, tier in [("t1", "haiku"), ("t2", "sonnet"), ("t3", "ollama")]:
        await db.execute_write(
            "INSERT INTO tasks (id, project_id, plan_id, title, description, "
            "task_type, priority, status, model_tier, wave, retry_count, max_retries, "
            "created_at, updated_at, context_json, tools_json, system_prompt, "
            "requirement_ids_json) "
            "VALUES (?, ?, ?, ?, 'Do stuff', 'code', 50, 'pending', ?, 0, 0, 2, ?, ?, "
            "'[]', '[]', '', '[]')",
            (tid, project_id, plan_id, f"Task {tid}", tier, now, now),
        )

    return project_id


class TestClaimableEndpoint:
    async def test_list_claimable_external(self, authed_client):
        project_id = await _setup_executing_project(authed_client)
        resp = await authed_client.get(f"/api/external/{project_id}/claimable")
        assert resp.status_code == 200
        tasks = resp.json()
        # All 3 tasks should be claimable in external mode
        assert len(tasks) == 3

    async def test_list_claimable_hybrid_excludes_ollama(self, authed_client):
        project_id = await _setup_executing_project(authed_client, "hybrid")
        resp = await authed_client.get(f"/api/external/{project_id}/claimable")
        assert resp.status_code == 200
        tasks = resp.json()
        # Ollama task should be excluded
        assert len(tasks) == 2
        tiers = [t["model_tier"] for t in tasks]
        assert "ollama" not in tiers

    async def test_list_claimable_auto_returns_empty(self, authed_client):
        project_id = await _setup_executing_project(authed_client, "auto")
        resp = await authed_client.get(f"/api/external/{project_id}/claimable")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_claimable_not_executing_returns_empty(self, authed_client):
        resp = await authed_client.post("/api/projects", json={
            "name": "Draft Project",
            "requirements": "Build something",
        })
        project_id = resp.json()["id"]
        resp = await authed_client.get(f"/api/external/{project_id}/claimable")
        assert resp.status_code == 200
        assert resp.json() == []


class TestClaimEndpoint:
    async def test_claim_task(self, authed_client):
        project_id = await _setup_executing_project(authed_client)
        resp = await authed_client.post("/api/external/tasks/t1/claim")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "t1"
        assert data["project_id"] == project_id
        assert data["title"] == "Task t1"

    async def test_claim_already_claimed_returns_409(self, authed_client):
        await _setup_executing_project(authed_client)
        resp = await authed_client.post("/api/external/tasks/t1/claim")
        assert resp.status_code == 200

        # Try to claim again
        resp = await authed_client.post("/api/external/tasks/t1/claim")
        assert resp.status_code == 409

    async def test_claim_auto_mode_returns_409(self, authed_client):
        await _setup_executing_project(authed_client, "auto")
        resp = await authed_client.post("/api/external/tasks/t1/claim")
        assert resp.status_code == 409

    async def test_claim_nonexistent_returns_404(self, authed_client):
        resp = await authed_client.post("/api/external/tasks/nonexistent/claim")
        assert resp.status_code == 404

    async def test_claim_hybrid_ollama_returns_409(self, authed_client):
        await _setup_executing_project(authed_client, "hybrid")
        resp = await authed_client.post("/api/external/tasks/t3/claim")
        assert resp.status_code == 409


class TestResultEndpoint:
    async def test_submit_result(self, authed_client):
        await _setup_executing_project(authed_client)
        await authed_client.post("/api/external/tasks/t1/claim")

        resp = await authed_client.post("/api/external/tasks/t1/result", json={
            "output_text": "Task completed successfully",
            "model_used": "claude-sonnet-4-6",
            "prompt_tokens": 100,
            "completion_tokens": 200,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_id"] == "t1"
        assert data["status"] == "completed"

    async def test_submit_result_not_running_returns_409(self, authed_client):
        await _setup_executing_project(authed_client)
        # Don't claim first
        resp = await authed_client.post("/api/external/tasks/t1/result", json={
            "output_text": "Output",
            "model_used": "test",
        })
        assert resp.status_code == 409

    async def test_submit_result_suggests_next_task(self, authed_client):
        await _setup_executing_project(authed_client)
        await authed_client.post("/api/external/tasks/t1/claim")

        resp = await authed_client.post("/api/external/tasks/t1/result", json={
            "output_text": "Done",
            "model_used": "claude-sonnet-4-6",
        })
        data = resp.json()
        # Should suggest one of the remaining unclaimed tasks
        if data["next_claimable_task_id"]:
            assert data["next_claimable_task_id"] in ("t2", "t3")


class TestReleaseEndpoint:
    async def test_release_task(self, authed_client):
        await _setup_executing_project(authed_client)
        await authed_client.post("/api/external/tasks/t1/claim")

        resp = await authed_client.post("/api/external/tasks/t1/release")
        assert resp.status_code == 200
        assert resp.json()["status"] == "released"

        # Task should be claimable again
        resp = await authed_client.post("/api/external/tasks/t1/claim")
        assert resp.status_code == 200

    async def test_release_not_running_returns_409(self, authed_client):
        await _setup_executing_project(authed_client)
        resp = await authed_client.post("/api/external/tasks/t1/release")
        assert resp.status_code == 409


class TestApiKeyAuth:
    async def test_api_key_auth_works_on_external_routes(self, authed_client):
        """Test that API key auth works for external routes."""
        # Create an API key via JWT-authenticated endpoint
        resp = await authed_client.post("/api/auth/api-keys", json={"name": "Test Key"})
        assert resp.status_code == 201
        api_key = resp.json()["key"]

        # Use the API key to access a route
        project_id = await _setup_executing_project(authed_client)

        # Switch to API key auth
        authed_client.headers["Authorization"] = f"Bearer {api_key}"
        resp = await authed_client.get(f"/api/external/{project_id}/claimable")
        assert resp.status_code == 200
