#  Orchestration Engine - Security Headers Integration Tests
#
#  Verifies that SecurityHeadersMiddleware sets the expected security
#  headers on all HTTP responses.
#
#  Depends on: backend/app.py (SecurityHeadersMiddleware)
#  Used by:    CI


class TestSecurityHeaders:
    """Verify security headers are present on API responses."""

    async def test_health_has_nosniff(self, app_client):
        resp = await app_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    async def test_health_has_referrer_policy(self, app_client):
        resp = await app_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.headers.get("Referrer-Policy") == "no-referrer"

    async def test_health_has_xframe(self, app_client):
        resp = await app_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.headers.get("X-Frame-Options") == "DENY"

    async def test_api_no_csp(self, app_client):
        """API responses should NOT include CSP (Vite build has inline scripts)."""
        resp = await app_client.get("/api/health")
        assert resp.status_code == 200
        assert "Content-Security-Policy" not in resp.headers

    async def test_auth_endpoint_has_headers(self, app_client):
        """Non-health API endpoint also gets security headers."""
        resp = await app_client.post(
            "/api/auth/login",
            json={"email": "nobody@test.com", "password": "wrong"},
        )
        # Login should fail but still have security headers
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Referrer-Policy") == "no-referrer"
        assert resp.headers.get("X-Frame-Options") == "DENY"

    async def test_protected_route_has_headers(self, app_client):
        """Even 401 responses should have security headers."""
        resp = await app_client.get("/api/projects")
        assert resp.status_code == 401
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("Referrer-Policy") == "no-referrer"
        assert resp.headers.get("X-Frame-Options") == "DENY"
