#  Orchestration Engine - Auth API Integration Tests
#
#  Tests for register, login, protected routes, and refresh flow.
#
#  Depends on: backend/routes/auth.py, tests/conftest.py
#  Used by:    pytest



class TestRegisterAPI:
    async def test_register_returns_201(self, app_client):
        resp = await app_client.post("/api/auth/register", json={
            "email": "new@test.com",
            "password": "password123",
            "display_name": "New User",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "new@test.com"
        assert data["display_name"] == "New User"
        assert data["role"] == "admin"  # first user

    async def test_register_duplicate_returns_400(self, app_client):
        """Duplicate email returns generic 400, not 409 (prevents email enumeration)."""
        await app_client.post("/api/auth/register", json={
            "email": "dupe@test.com",
            "password": "password123",
        })
        resp = await app_client.post("/api/auth/register", json={
            "email": "dupe@test.com",
            "password": "password456",
        })
        assert resp.status_code == 400
        assert "Registration failed" in resp.json()["detail"]

    async def test_register_short_password_returns_422(self, app_client):
        resp = await app_client.post("/api/auth/register", json={
            "email": "user@test.com",
            "password": "short",
        })
        assert resp.status_code == 422


class TestLoginAPI:
    async def test_login_returns_tokens(self, app_client):
        await app_client.post("/api/auth/register", json={
            "email": "user@test.com",
            "password": "password123",
        })
        resp = await app_client.post("/api/auth/login", json={
            "email": "user@test.com",
            "password": "password123",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["user"]["email"] == "user@test.com"

    async def test_login_wrong_password_returns_401(self, app_client):
        await app_client.post("/api/auth/register", json={
            "email": "user@test.com",
            "password": "password123",
        })
        resp = await app_client.post("/api/auth/login", json={
            "email": "user@test.com",
            "password": "wrong",
        })
        assert resp.status_code == 401


class TestProtectedRoutes:
    async def test_projects_without_token_returns_401(self, app_client):
        resp = await app_client.get("/api/projects")
        assert resp.status_code == 401

    async def test_projects_with_token_returns_200(self, authed_client):
        resp = await authed_client.get("/api/projects")
        assert resp.status_code == 200

    async def test_services_with_token_returns_200(self, authed_client):
        resp = await authed_client.get("/api/services")
        assert resp.status_code == 200


class TestRefreshAPI:
    async def test_refresh_returns_new_tokens(self, app_client):
        await app_client.post("/api/auth/register", json={
            "email": "user@test.com",
            "password": "password123",
        })
        login_resp = await app_client.post("/api/auth/login", json={
            "email": "user@test.com",
            "password": "password123",
        })
        refresh_token = login_resp.json()["refresh_token"]

        resp = await app_client.post("/api/auth/refresh", json={
            "refresh_token": refresh_token,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data

    async def test_refresh_with_access_token_returns_401(self, app_client):
        await app_client.post("/api/auth/register", json={
            "email": "user@test.com",
            "password": "password123",
        })
        login_resp = await app_client.post("/api/auth/login", json={
            "email": "user@test.com",
            "password": "password123",
        })
        access_token = login_resp.json()["access_token"]

        resp = await app_client.post("/api/auth/refresh", json={
            "refresh_token": access_token,
        })
        assert resp.status_code == 401


class TestMeEndpoint:
    async def test_me_returns_user(self, authed_client):
        resp = await authed_client.get("/api/auth/me")
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "test@example.com"
        assert data["display_name"] == "Test User"

    async def test_me_without_token_returns_401(self, app_client):
        resp = await app_client.get("/api/auth/me")
        assert resp.status_code == 401
