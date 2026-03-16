#  Orchestration Engine - Projects API Integration Tests
#
#  CRUD, plan approval, execution state transitions, and git branching.
#
#  Depends on: backend/routes/projects.py, backend/services/git_service.py, tests/conftest.py
#  Used by:    pytest

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dependency_injector import providers

from backend.routes.projects import _bootstrap_hekate_config
from backend.services.git_service import GitService



class TestCreateProject:
    async def test_create_returns_201(self, authed_client):
        resp = await authed_client.post("/api/projects", json={
            "name": "Test Project",
            "requirements": "Build something",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Project"
        assert data["status"] == "draft"
        assert "id" in data

    async def test_create_with_config(self, authed_client):
        resp = await authed_client.post("/api/projects", json={
            "name": "Configured",
            "requirements": "Build with config",
            "config": {"key": "value"},
        })
        assert resp.status_code == 201
        assert resp.json()["config"]["key"] == "value"

    async def test_create_missing_name_returns_422(self, authed_client):
        resp = await authed_client.post("/api/projects", json={
            "requirements": "no name",
        })
        assert resp.status_code == 422


class TestBootstrapHekateConfig:
    """Tests for _bootstrap_hekate_config auto-generation."""

    def test_noz_project_gets_noz_template(self, tmp_path):
        # Simulate a NoZ project (has noz/ subdir and a .sln)
        (tmp_path / "noz").mkdir()
        (tmp_path / "MyGame.sln").write_text("")
        _bootstrap_hekate_config(str(tmp_path))
        target = tmp_path / ".hekate.json"
        assert target.exists()
        config = json.loads(target.read_text())
        assert config["projectType"] == "noz-game"
        assert "game" in config["roles"]

    def test_generic_cs_project_gets_generic_template(self, tmp_path):
        # No noz/ dir, just a plain directory
        _bootstrap_hekate_config(str(tmp_path))
        target = tmp_path / ".hekate.json"
        assert target.exists()
        config = json.loads(target.read_text())
        assert config["projectType"] == "csharp"
        assert "app" in config["roles"]

    def test_existing_file_not_overwritten(self, tmp_path):
        existing = tmp_path / ".hekate.json"
        existing.write_text('{"custom": true}')
        _bootstrap_hekate_config(str(tmp_path))
        assert json.loads(existing.read_text()) == {"custom": True}

    def test_nonexistent_path_skipped(self):
        _bootstrap_hekate_config("/nonexistent/path/that/does/not/exist")
        # Should not raise


class TestListProjects:
    async def test_list_empty(self, authed_client):
        resp = await authed_client.get("/api/projects")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_returns_created(self, authed_client):
        await authed_client.post("/api/projects", json={
            "name": "P1", "requirements": "r1",
        })
        await authed_client.post("/api/projects", json={
            "name": "P2", "requirements": "r2",
        })
        resp = await authed_client.get("/api/projects")
        assert len(resp.json()) == 2

    async def test_list_filter_by_status(self, authed_client):
        await authed_client.post("/api/projects", json={
            "name": "Draft", "requirements": "r",
        })
        resp = await authed_client.get("/api/projects?status=draft")
        assert len(resp.json()) == 1
        resp = await authed_client.get("/api/projects?status=completed")
        assert len(resp.json()) == 0


class TestGetProject:
    async def test_get_existing(self, authed_client):
        create = await authed_client.post("/api/projects", json={
            "name": "P", "requirements": "r",
        })
        pid = create.json()["id"]
        resp = await authed_client.get(f"/api/projects/{pid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "P"

    async def test_get_nonexistent_returns_404(self, authed_client):
        resp = await authed_client.get("/api/projects/nope")
        assert resp.status_code == 404


class TestUpdateProject:
    async def test_update_name(self, authed_client):
        create = await authed_client.post("/api/projects", json={
            "name": "Old", "requirements": "r",
        })
        pid = create.json()["id"]
        resp = await authed_client.patch(f"/api/projects/{pid}", json={
            "name": "New",
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"

    async def test_update_no_fields_returns_400(self, authed_client):
        create = await authed_client.post("/api/projects", json={
            "name": "P", "requirements": "r",
        })
        pid = create.json()["id"]
        resp = await authed_client.patch(f"/api/projects/{pid}", json={})
        assert resp.status_code == 400


class TestUpdateProjectRigor:
    async def test_patch_planning_rigor(self, authed_client):
        create = await authed_client.post("/api/projects", json={
            "name": "Rigor Test", "requirements": "r",
        })
        pid = create.json()["id"]
        assert create.json()["planning_rigor"] == "L2"  # default

        resp = await authed_client.patch(f"/api/projects/{pid}", json={
            "planning_rigor": "L3",
        })
        assert resp.status_code == 200
        assert resp.json()["planning_rigor"] == "L3"

    async def test_patch_rigor_preserves_existing_config(self, authed_client):
        create = await authed_client.post("/api/projects", json={
            "name": "Config Test", "requirements": "r",
            "config": {"custom_key": "custom_value"},
        })
        pid = create.json()["id"]

        resp = await authed_client.patch(f"/api/projects/{pid}", json={
            "planning_rigor": "L1",
        })
        assert resp.status_code == 200
        assert resp.json()["planning_rigor"] == "L1"
        assert resp.json()["config"]["custom_key"] == "custom_value"

    async def test_patch_config_and_rigor_together(self, authed_client):
        create = await authed_client.post("/api/projects", json={
            "name": "Both Test", "requirements": "r",
        })
        pid = create.json()["id"]

        resp = await authed_client.patch(f"/api/projects/{pid}", json={
            "config": {"new_key": "new_value"},
            "planning_rigor": "L3",
        })
        assert resp.status_code == 200
        assert resp.json()["config"]["new_key"] == "new_value"
        assert resp.json()["planning_rigor"] == "L3"


class TestDeleteProject:
    async def test_delete_existing(self, authed_client):
        create = await authed_client.post("/api/projects", json={
            "name": "P", "requirements": "r",
        })
        pid = create.json()["id"]
        resp = await authed_client.delete(f"/api/projects/{pid}")
        assert resp.status_code == 204

        resp = await authed_client.get(f"/api/projects/{pid}")
        assert resp.status_code == 404

    async def test_delete_nonexistent_returns_404(self, authed_client):
        resp = await authed_client.delete("/api/projects/nope")
        assert resp.status_code == 404


class TestCancelProject:
    async def test_cancel_draft_project(self, authed_client):
        create = await authed_client.post("/api/projects", json={
            "name": "P", "requirements": "r",
        })
        pid = create.json()["id"]
        resp = await authed_client.post(f"/api/projects/{pid}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Helpers for execution/branching tests
# ---------------------------------------------------------------------------

async def _create_ready_project(authed_client, db, name="Branch Test", repo_path=None,
                                git_base_branch=None):
    """Create a project and set it to 'ready' state via direct DB update."""
    payload = {"name": name, "requirements": "Build something"}
    if repo_path:
        payload["repo_path"] = repo_path
    if git_base_branch:
        payload["git_base_branch"] = git_base_branch

    resp = await authed_client.post("/api/projects", json=payload)
    assert resp.status_code == 201
    pid = resp.json()["id"]

    # Advance to ready (normally done by plan approval, shortcut via DB)
    await db.execute_write(
        "UPDATE projects SET status = 'ready', updated_at = ? WHERE id = ?",
        (time.time(), pid),
    )
    return pid


# ---------------------------------------------------------------------------
# Execute endpoint — git branching
# ---------------------------------------------------------------------------

class TestExecuteBranching:
    """Verify orch/ branch creation when project execution starts."""

    async def test_execute_creates_branch(self, authed_client, tmp_db):
        """Execute with repo_path creates orch/ branch and returns branch name."""
        mock_git = AsyncMock(spec=GitService)
        mock_git.ensure_feature_branch = AsyncMock(return_value=True)

        from backend.app import container
        container.git_service.override(providers.Object(mock_git))
        try:
            pid = await _create_ready_project(
                authed_client, tmp_db,
                name="My Cool Project",
                repo_path="C:/repos/my-cool-project",
            )
            resp = await authed_client.post(f"/api/projects/{pid}/execute")
            assert resp.status_code == 200

            data = resp.json()
            assert data["status"] == "executing"
            assert data["branch"] == "orch/my-cool-project"

            # Verify ensure_feature_branch was called with correct args
            call_args = mock_git.ensure_feature_branch.call_args
            assert call_args[0][1] == "orch/my-cool-project"
            assert call_args[0][2] == "main"

            # Verify DB has the branch stored
            row = await tmp_db.fetchone(
                "SELECT status, git_project_branch FROM projects WHERE id = ?", (pid,),
            )
            assert row["status"] == "executing"
            assert row["git_project_branch"] == "orch/my-cool-project"
        finally:
            container.git_service.reset_override()

    async def test_execute_custom_base_branch(self, authed_client, tmp_db):
        """Execute uses the project's git_base_branch instead of default main."""
        mock_git = AsyncMock(spec=GitService)
        mock_git.ensure_feature_branch = AsyncMock(return_value=True)

        from backend.app import container
        container.git_service.override(providers.Object(mock_git))
        try:
            pid = await _create_ready_project(
                authed_client, tmp_db,
                name="Develop Branch",
                repo_path="C:/repos/dev-project",
                git_base_branch="develop",
            )
            resp = await authed_client.post(f"/api/projects/{pid}/execute")
            assert resp.status_code == 200

            call_args = mock_git.ensure_feature_branch.call_args
            assert call_args[0][1] == "orch/develop-branch"
            assert call_args[0][2] == "develop"
        finally:
            container.git_service.reset_override()

    async def test_execute_no_repo_path_skips_branch(self, authed_client, tmp_db):
        """Execute without repo_path skips branching silently."""
        mock_git = AsyncMock(spec=GitService)
        mock_git.ensure_feature_branch = AsyncMock(return_value=False)

        from backend.app import container
        container.git_service.override(providers.Object(mock_git))
        try:
            pid = await _create_ready_project(authed_client, tmp_db, name="No Repo")
            resp = await authed_client.post(f"/api/projects/{pid}/execute")
            assert resp.status_code == 200

            data = resp.json()
            assert data["status"] == "executing"
            assert data["branch"] is None

            # ensure_feature_branch still called (with None repo_path), returns False
            mock_git.ensure_feature_branch.assert_awaited_once()

            # DB should NOT have git_project_branch set
            row = await tmp_db.fetchone(
                "SELECT git_project_branch FROM projects WHERE id = ?", (pid,),
            )
            assert row["git_project_branch"] is None
        finally:
            container.git_service.reset_override()

    async def test_execute_idempotent_reexecution(self, authed_client, tmp_db):
        """Re-executing a paused project reuses the existing branch."""
        mock_git = AsyncMock(spec=GitService)
        mock_git.ensure_feature_branch = AsyncMock(return_value=True)

        from backend.app import container
        container.git_service.override(providers.Object(mock_git))
        try:
            pid = await _create_ready_project(
                authed_client, tmp_db,
                name="Pause Resume",
                repo_path="C:/repos/pause-test",
            )

            # First execution
            resp = await authed_client.post(f"/api/projects/{pid}/execute")
            assert resp.status_code == 200
            assert resp.json()["branch"] == "orch/pause-resume"

            # Pause
            resp = await authed_client.post(f"/api/projects/{pid}/pause")
            assert resp.status_code == 200

            # Re-execute — should call ensure_feature_branch again (idempotent)
            mock_git.ensure_feature_branch.reset_mock()
            resp = await authed_client.post(f"/api/projects/{pid}/execute")
            assert resp.status_code == 200
            assert resp.json()["branch"] == "orch/pause-resume"

            call_args = mock_git.ensure_feature_branch.call_args
            assert call_args[0][1] == "orch/pause-resume"
            assert call_args[0][2] == "main"
        finally:
            container.git_service.reset_override()

    async def test_execute_git_error_returns_500(self, authed_client, tmp_db):
        """GitError during branch setup returns 500."""
        from backend.exceptions import GitError

        mock_git = AsyncMock(spec=GitService)
        mock_git.ensure_feature_branch = AsyncMock(
            side_effect=GitError("branch creation failed"),
        )

        from backend.app import container
        container.git_service.override(providers.Object(mock_git))
        try:
            pid = await _create_ready_project(
                authed_client, tmp_db,
                name="Git Fail",
                repo_path="C:/repos/bad-repo",
            )
            resp = await authed_client.post(f"/api/projects/{pid}/execute")
            assert resp.status_code == 500
            assert "Git branch setup failed" in resp.json()["detail"]
        finally:
            container.git_service.reset_override()

    async def test_execute_draft_project_returns_400(self, authed_client):
        """Cannot execute a project still in draft state."""
        resp = await authed_client.post("/api/projects", json={
            "name": "Draft", "requirements": "r",
        })
        pid = resp.json()["id"]
        resp = await authed_client.post(f"/api/projects/{pid}/execute")
        assert resp.status_code == 400

    async def test_branch_name_slugified(self, authed_client, tmp_db):
        """Special characters in project name are slugified in branch name."""
        mock_git = AsyncMock(spec=GitService)
        mock_git.ensure_feature_branch = AsyncMock(return_value=True)

        from backend.app import container
        container.git_service.override(providers.Object(mock_git))
        try:
            pid = await _create_ready_project(
                authed_client, tmp_db,
                name="My Project!!! (v2.0) @#$",
                repo_path="C:/repos/slugtest",
            )
            resp = await authed_client.post(f"/api/projects/{pid}/execute")
            assert resp.status_code == 200

            branch = resp.json()["branch"]
            # Should be lowercase, hyphens only, no special chars
            assert branch.startswith("orch/")
            assert "!" not in branch
            assert "@" not in branch
            assert "#" not in branch
            assert "$" not in branch
        finally:
            container.git_service.reset_override()
