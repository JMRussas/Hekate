#  Orchestration Engine - Admin API Tests
#
#  Tests for admin-only endpoints: users, stats.
#
#  Depends on: backend/routes/admin.py, tests/conftest.py
#  Used by:    pytest



async def _register_user(client, email, password="testpass123", display_name="User"):
    """Register a user and return the user dict."""
    resp = await client.post("/api/auth/register", json={
        "email": email, "password": password, "display_name": display_name,
    })
    return resp


async def _get_admin_client(app_client, tmp_db):
    """Register an admin user (first user) and return the authed client."""
    resp = await app_client.post("/api/auth/register", json={
        "email": "admin@example.com",
        "password": "adminpass123",
        "display_name": "Admin",
    })
    assert resp.status_code == 201

    resp = await app_client.post("/api/auth/login", json={
        "email": "admin@example.com",
        "password": "adminpass123",
    })
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    app_client.headers["Authorization"] = f"Bearer {token}"
    return app_client


async def _get_non_admin_client(app_client):
    """Register a second (non-admin) user and return their token."""

    # Register second user (non-admin)
    resp = await app_client.post("/api/auth/register", json={
        "email": "user@example.com",
        "password": "userpass123",
        "display_name": "Regular User",
    })
    assert resp.status_code == 201

    resp = await app_client.post("/api/auth/login", json={
        "email": "user@example.com",
        "password": "userpass123",
    })
    assert resp.status_code == 200
    return resp.json()["access_token"]


class TestListUsers:
    async def test_admin_can_list_users(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.get("/api/admin/users")
        assert resp.status_code == 200
        users = resp.json()
        assert len(users) >= 1
        assert users[0]["email"] == "admin@example.com"
        assert "project_count" in users[0]

    async def test_non_admin_gets_403(self, app_client, tmp_db):
        # First register admin
        client = await _get_admin_client(app_client, tmp_db)
        # Register non-admin
        token = await _get_non_admin_client(client)

        client.headers["Authorization"] = f"Bearer {token}"
        resp = await client.get("/api/admin/users")
        assert resp.status_code == 403

    async def test_unauthenticated_gets_401(self, app_client, tmp_db):
        resp = await app_client.get("/api/admin/users")
        assert resp.status_code == 401

    async def test_lists_multiple_users(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        # Register more users
        await _register_user(client, "a@example.com")
        await _register_user(client, "b@example.com")

        resp = await client.get("/api/admin/users")
        assert resp.status_code == 200
        assert len(resp.json()) == 3  # admin + 2 users

    async def test_includes_project_count(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        # Create a project
        await client.post("/api/projects", json={
            "name": "Test", "requirements": "Test req",
        })

        resp = await client.get("/api/admin/users")
        users = resp.json()
        admin_user = next(u for u in users if u["email"] == "admin@example.com")
        assert admin_user["project_count"] == 1


class TestUpdateUser:
    async def test_change_role(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        await _register_user(client, "target@example.com")

        # Get user id
        resp = await client.get("/api/admin/users")
        target = next(u for u in resp.json() if u["email"] == "target@example.com")

        resp = await client.patch(f"/api/admin/users/{target['id']}", json={
            "role": "admin",
        })
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    async def test_deactivate_user(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        await _register_user(client, "target@example.com")

        resp = await client.get("/api/admin/users")
        target = next(u for u in resp.json() if u["email"] == "target@example.com")

        resp = await client.patch(f"/api/admin/users/{target['id']}", json={
            "is_active": False,
        })
        assert resp.status_code == 200
        assert resp.json()["is_active"] is False

    async def test_cannot_deactivate_self(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)

        resp = await client.get("/api/admin/users")
        admin = next(u for u in resp.json() if u["email"] == "admin@example.com")

        resp = await client.patch(f"/api/admin/users/{admin['id']}", json={
            "is_active": False,
        })
        assert resp.status_code == 400
        assert "own account" in resp.json()["detail"]

    async def test_cannot_change_own_role(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)

        resp = await client.get("/api/admin/users")
        admin = next(u for u in resp.json() if u["email"] == "admin@example.com")

        resp = await client.patch(f"/api/admin/users/{admin['id']}", json={
            "role": "user",
        })
        assert resp.status_code == 400
        assert "own role" in resp.json()["detail"]

    async def test_nonexistent_user_returns_404(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.patch("/api/admin/users/nonexistent", json={
            "role": "admin",
        })
        assert resp.status_code == 404

    async def test_no_fields_returns_400(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        await _register_user(client, "target@example.com")

        resp = await client.get("/api/admin/users")
        target = next(u for u in resp.json() if u["email"] == "target@example.com")

        resp = await client.patch(f"/api/admin/users/{target['id']}", json={})
        assert resp.status_code == 400

    async def test_invalid_role_returns_422(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        await _register_user(client, "target@example.com")

        resp = await client.get("/api/admin/users")
        target = next(u for u in resp.json() if u["email"] == "target@example.com")

        resp = await client.patch(f"/api/admin/users/{target['id']}", json={
            "role": "superadmin",
        })
        assert resp.status_code == 422


class TestAdminStats:
    async def test_returns_stats(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        resp = await client.get("/api/admin/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_users"] >= 1
        assert data["active_users"] >= 1
        assert "total_projects" in data
        assert "projects_by_status" in data
        assert "total_tasks" in data
        assert "tasks_by_status" in data
        assert "total_spend_usd" in data
        assert "spend_by_model" in data
        assert "task_completion_rate" in data

    async def test_stats_with_data(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)

        # Create a project
        resp = await client.post("/api/projects", json={
            "name": "Stats Test", "requirements": "Test",
        })
        assert resp.status_code == 201

        resp = await client.get("/api/admin/stats")
        data = resp.json()
        assert data["total_projects"] == 1

    async def test_non_admin_stats_returns_403(self, app_client, tmp_db):
        client = await _get_admin_client(app_client, tmp_db)
        token = await _get_non_admin_client(client)
        client.headers["Authorization"] = f"Bearer {token}"

        resp = await client.get("/api/admin/stats")
        assert resp.status_code == 403
