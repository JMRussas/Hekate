#  Orchestration Engine - Projects API Integration Tests
#
#  CRUD, plan approval, and execution state transitions.
#
#  Depends on: backend/routes/projects.py, tests/conftest.py
#  Used by:    pytest



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
