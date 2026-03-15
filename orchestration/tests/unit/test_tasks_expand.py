#  Orchestration Engine - Task Expand Endpoint Tests
#
#  Tests for POST /tasks/{task_id}/expand — L0 epic expansion to L2 projects.
#
#  Depends on: conftest.py (authed_client, create_test_project, create_test_task)
#  Used by:    CI

import json
import time

import pytest

from tests.conftest import create_test_project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setup_expandable_task(
    client,
    *,
    config: dict | None = None,
    repo_path: str = "C:\\Users\\dev\\project",
    git_base_branch: str = "main",
    task_title: str = "Epic: Build Auth System",
    task_description: str = "Implement full auth with OIDC and JWT",
):
    """Create a project with config and a task suitable for expansion."""
    resp = await client.post("/api/projects", json={
        "name": "L0 Roadmap",
        "requirements": "Build the platform",
        "config": config or {},
        "repo_path": repo_path,
        "git_base_branch": git_base_branch,
    })
    assert resp.status_code == 201
    project_id = resp.json()["id"]

    # Insert task directly via DB (avoids needing full plan flow)
    from backend.app import container
    db = container.db()
    now = time.time()
    plan_id = f"plan_{project_id}"

    await db.execute_write(
        "INSERT INTO plans (id, project_id, version, model_used, plan_json, status, created_at) "
        "VALUES (?, ?, 1, 'test', '{}', 'approved', ?)",
        (plan_id, project_id, now),
    )

    task_id = "task_expand_001"
    await db.execute_write(
        "INSERT INTO tasks (id, project_id, plan_id, title, description, "
        "task_type, priority, status, model_tier, wave, retry_count, max_retries, "
        "created_at, updated_at, context_json, tools_json, system_prompt, "
        "requirement_ids_json) "
        "VALUES (?, ?, ?, ?, ?, 'research', 50, 'pending', 'haiku', 0, 0, 2, ?, ?, "
        "'[]', '[]', '', '[]')",
        (task_id, project_id, plan_id, task_title, task_description, now, now),
    )

    return project_id, task_id


class TestExpandEndpoint:
    """Tests for POST /api/tasks/{task_id}/expand."""

    async def test_expand_returns_201_with_project_id(self, authed_client):
        """Expanding a task returns 201 with the new project's ID and name."""
        _, task_id = await _setup_expandable_task(authed_client)
        resp = await authed_client.post(f"/api/tasks/{task_id}/expand")
        assert resp.status_code == 201
        data = resp.json()
        assert "project_id" in data
        assert data["name"] == "Epic: Build Auth System"

    async def test_expand_creates_project_with_task_metadata(self, authed_client):
        """New project uses task title as name and description as requirements."""
        _, task_id = await _setup_expandable_task(
            authed_client,
            task_title="Epic: Payment Integration",
            task_description="Integrate Stripe for subscriptions",
        )
        resp = await authed_client.post(f"/api/tasks/{task_id}/expand")
        assert resp.status_code == 201
        new_project_id = resp.json()["project_id"]

        # Fetch the created project
        resp = await authed_client.get(f"/api/projects/{new_project_id}")
        assert resp.status_code == 200
        project = resp.json()
        assert project["name"] == "Epic: Payment Integration"
        assert project["requirements"] == "Integrate Stripe for subscriptions"
        assert project["status"] == "draft"

    async def test_expand_inherits_repo_path(self, authed_client):
        """New project inherits repo_path from parent."""
        _, task_id = await _setup_expandable_task(
            authed_client,
            repo_path="C:\\Users\\dev\\my-repo",
        )
        resp = await authed_client.post(f"/api/tasks/{task_id}/expand")
        new_project_id = resp.json()["project_id"]

        resp = await authed_client.get(f"/api/projects/{new_project_id}")
        assert resp.json()["repo_path"] == "C:\\Users\\dev\\my-repo"

    async def test_expand_inherits_git_base_branch(self, authed_client):
        """New project inherits git_base_branch from parent."""
        _, task_id = await _setup_expandable_task(
            authed_client,
            git_base_branch="develop",
        )
        resp = await authed_client.post(f"/api/tasks/{task_id}/expand")
        new_project_id = resp.json()["project_id"]

        resp = await authed_client.get(f"/api/projects/{new_project_id}")
        assert resp.json()["git_base_branch"] == "develop"

    async def test_expand_inherits_parent_config(self, authed_client):
        """New project config inherits parent's config values (e.g., review_cycle)."""
        parent_config = {
            "review_cycle": {"enabled": True, "max_iterations": 2},
            "execution_mode": "hybrid",
        }
        _, task_id = await _setup_expandable_task(
            authed_client,
            config=parent_config,
        )
        resp = await authed_client.post(f"/api/tasks/{task_id}/expand")
        new_project_id = resp.json()["project_id"]

        resp = await authed_client.get(f"/api/projects/{new_project_id}")
        new_config = resp.json()["config"]
        assert new_config["review_cycle"] == {"enabled": True, "max_iterations": 2}
        assert new_config["execution_mode"] == "hybrid"

    async def test_expand_sets_planning_rigor_l2(self, authed_client):
        """Expanded project config always has planning_rigor = L2."""
        _, task_id = await _setup_expandable_task(
            authed_client,
            config={"planning_rigor": "L0"},
        )
        resp = await authed_client.post(f"/api/tasks/{task_id}/expand")
        new_project_id = resp.json()["project_id"]

        resp = await authed_client.get(f"/api/projects/{new_project_id}")
        assert resp.json()["config"]["planning_rigor"] == "L2"

    async def test_expand_stores_lineage_in_config(self, authed_client):
        """Expanded project config contains expanded_from with parent project/task IDs."""
        project_id, task_id = await _setup_expandable_task(authed_client)
        resp = await authed_client.post(f"/api/tasks/{task_id}/expand")
        new_project_id = resp.json()["project_id"]

        resp = await authed_client.get(f"/api/projects/{new_project_id}")
        expanded_from = resp.json()["config"]["expanded_from"]
        assert expanded_from["project_id"] == project_id
        assert expanded_from["task_id"] == task_id
        assert expanded_from["epic_title"] == "Epic: Build Auth System"

    async def test_expand_nonexistent_task_returns_404(self, authed_client):
        """Expanding a non-existent task returns 404."""
        resp = await authed_client.post("/api/tasks/nonexistent_999/expand")
        assert resp.status_code == 404

    async def test_expand_unowned_task_returns_403(self, authed_client):
        """Expanding a task owned by another user returns 403."""
        # authed_client is admin (first registered user) — register a second
        # non-admin user and create a project owned by them, then try to
        # expand as that non-admin user.
        _, task_id = await _setup_expandable_task(authed_client)

        # Register a non-admin user
        resp = await authed_client.post("/api/auth/register", json={
            "email": "nonadmin@example.com",
            "password": "nonadminpass123",
            "display_name": "Non Admin",
        })
        assert resp.status_code == 201

        # Login as the non-admin user
        resp = await authed_client.post("/api/auth/login", json={
            "email": "nonadmin@example.com",
            "password": "nonadminpass123",
        })
        assert resp.status_code == 200
        nonadmin_token = resp.json()["access_token"]

        # Try to expand the task as the non-admin (who doesn't own the project)
        resp = await authed_client.post(
            f"/api/tasks/{task_id}/expand",
            headers={"Authorization": f"Bearer {nonadmin_token}"},
        )
        assert resp.status_code == 403
