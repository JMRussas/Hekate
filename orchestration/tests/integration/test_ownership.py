#  Orchestration Engine - Ownership Enforcement Tests
#
#  Tests that users can only access their own projects/tasks.
#  Admin can access all.
#
#  Depends on: backend/routes/projects.py, backend/routes/tasks.py, tests/conftest.py
#  Used by:    pytest



async def _register_and_login(client, email, password="password123"):
    """Register a user and return auth headers."""
    await client.post("/api/auth/register", json={
        "email": email,
        "password": password,
    })
    resp = await client.post("/api/auth/login", json={
        "email": email,
        "password": password,
    })
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestProjectOwnership:
    async def test_user_sees_only_own_projects(self, app_client):
        # User A (admin — first registered)
        headers_a = await _register_and_login(app_client, "a@test.com")
        # User B
        headers_b = await _register_and_login(app_client, "b@test.com")

        # A creates a project
        resp = await app_client.post("/api/projects", json={
            "name": "A's Project", "requirements": "r",
        }, headers=headers_a)
        assert resp.status_code == 201

        # B creates a project
        resp = await app_client.post("/api/projects", json={
            "name": "B's Project", "requirements": "r",
        }, headers=headers_b)
        assert resp.status_code == 201
        pid_b = resp.json()["id"]

        # B only sees their own project
        resp = await app_client.get("/api/projects", headers=headers_b)
        projects = resp.json()
        assert len(projects) == 1
        assert projects[0]["id"] == pid_b

        # A (admin) sees both
        resp = await app_client.get("/api/projects", headers=headers_a)
        assert len(resp.json()) == 2

    async def test_user_cannot_get_other_users_project(self, app_client):
        headers_a = await _register_and_login(app_client, "a@test.com")
        headers_b = await _register_and_login(app_client, "b@test.com")

        resp = await app_client.post("/api/projects", json={
            "name": "A's", "requirements": "r",
        }, headers=headers_a)

        # B cannot access A's project (A is admin, but B is not)
        # Actually A is admin so let's test B's project from a third user
        headers_c = await _register_and_login(app_client, "c@test.com")

        resp = await app_client.post("/api/projects", json={
            "name": "B's", "requirements": "r",
        }, headers=headers_b)
        pid_b = resp.json()["id"]

        # C cannot access B's project
        resp = await app_client.get(f"/api/projects/{pid_b}", headers=headers_c)
        assert resp.status_code == 403

    async def test_user_cannot_delete_other_users_project(self, app_client):
        await _register_and_login(app_client, "a@test.com")
        headers_b = await _register_and_login(app_client, "b@test.com")

        resp = await app_client.post("/api/projects", json={
            "name": "B's", "requirements": "r",
        }, headers=headers_b)
        pid_b = resp.json()["id"]

        # Register a third user (non-admin)
        headers_c = await _register_and_login(app_client, "c@test.com")

        resp = await app_client.delete(f"/api/projects/{pid_b}", headers=headers_c)
        assert resp.status_code == 403

    async def test_admin_can_access_any_project(self, app_client):
        headers_admin = await _register_and_login(app_client, "admin@test.com")
        headers_user = await _register_and_login(app_client, "user@test.com")

        resp = await app_client.post("/api/projects", json={
            "name": "User's", "requirements": "r",
        }, headers=headers_user)
        pid = resp.json()["id"]

        # Admin can access the user's project
        resp = await app_client.get(f"/api/projects/{pid}", headers=headers_admin)
        assert resp.status_code == 200

    async def test_unauthenticated_returns_401(self, app_client):
        resp = await app_client.get("/api/projects")
        assert resp.status_code == 401

        resp = await app_client.post("/api/projects", json={
            "name": "X", "requirements": "r",
        })
        assert resp.status_code == 401


class TestUsageOwnership:
    async def test_budget_requires_admin(self, app_client):
        headers_admin = await _register_and_login(app_client, "admin@test.com")
        headers_user = await _register_and_login(app_client, "user@test.com")

        # Admin can access budget
        resp = await app_client.get("/api/usage/budget", headers=headers_admin)
        assert resp.status_code == 200

        # Regular user cannot
        resp = await app_client.get("/api/usage/budget", headers=headers_user)
        assert resp.status_code == 403
