#  Orchestration Engine - OIDC API Integration Tests
#
#  Tests for OIDC auth routes with mocked OIDCService.
#
#  Depends on: backend/routes/auth_oidc.py, tests/conftest.py
#  Used by:    pytest


async def test_list_providers_returns_empty(app_client):
    """GET /auth/oidc/providers returns empty list when no providers configured."""
    resp = await app_client.get("/api/auth/oidc/providers")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_unknown_provider_login_returns_404(app_client):
    resp = await app_client.get("/api/auth/oidc/nonexistent/login?redirect_uri=http://test")
    assert resp.status_code == 404


async def test_callback_invalid_state_returns_400(app_client):
    """POST callback with bad state token returns 400."""
    resp = await app_client.post("/api/auth/oidc/testprov/callback", json={
        "code": "test-code",
        "state": "test-state",
        "state_token": "invalid-jwt-token",
        "redirect_uri": "http://test/callback",
    })
    assert resp.status_code == 400
    assert "state token" in resp.json()["detail"].lower()


async def test_identities_requires_auth(app_client):
    """GET /auth/oidc/identities without token returns 401."""
    resp = await app_client.get("/api/auth/oidc/identities")
    assert resp.status_code == 401


async def test_link_requires_auth(app_client):
    """POST /auth/oidc/link/testprov without token returns 401."""
    resp = await app_client.post("/api/auth/oidc/link/testprov", json={
        "code": "x", "state": "x", "state_token": "x", "redirect_uri": "x",
    })
    assert resp.status_code == 401


async def test_unlink_requires_auth(app_client):
    """DELETE /auth/oidc/link/testprov without token returns 401."""
    resp = await app_client.delete("/api/auth/oidc/link/testprov")
    assert resp.status_code == 401


async def test_identities_empty_for_new_user(authed_client):
    """GET /auth/oidc/identities returns empty for password-only user."""
    resp = await authed_client.get("/api/auth/oidc/identities")
    assert resp.status_code == 200
    assert resp.json() == []
