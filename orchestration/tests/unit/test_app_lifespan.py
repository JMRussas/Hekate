#  Orchestration Engine - App Lifespan Tests
#
#  Tests for FastAPI app lifespan (startup/shutdown), rate limit handler,
#  and RequestIDMiddleware.
#
#  Depends on: backend/app.py
#  Used by:    pytest

from unittest.mock import MagicMock

from fastapi import Request
from slowapi.errors import RateLimitExceeded

from backend.app import rate_limit_handler


# ---------------------------------------------------------------------------
# Rate limit handler
# ---------------------------------------------------------------------------

class TestRateLimitHandler:
    async def test_returns_429(self):
        """rate_limit_handler returns 429 JSON response."""
        request = MagicMock(spec=Request)
        mock_limit = MagicMock()
        mock_limit.limit = "5 per minute"
        exc = RateLimitExceeded(mock_limit)

        response = await rate_limit_handler(request, exc)
        assert response.status_code == 429
        assert b"Rate limit exceeded" in response.body


# ---------------------------------------------------------------------------
# Lifespan startup/shutdown
# ---------------------------------------------------------------------------

class TestLifespan:
    async def test_startup_initializes_services(self, app_client):
        """App lifespan starts all services (verified via app_client fixture).

        The app_client fixture goes through the full lifespan.
        If startup fails, the fixture itself fails. This test just verifies
        the app is functional after startup.
        """
        resp = await app_client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_request_id_header(self, app_client):
        """Requests get X-Request-ID header from RequestIDMiddleware."""
        resp = await app_client.get("/api/health")
        rid = resp.headers.get("x-request-id")
        assert rid is not None
        assert len(rid) == 12

    async def test_request_id_unique_per_request(self, app_client):
        """Each request gets a unique request ID."""
        resp1 = await app_client.get("/api/health")
        resp2 = await app_client.get("/api/health")
        rid1 = resp1.headers.get("x-request-id")
        rid2 = resp2.headers.get("x-request-id")
        assert rid1 != rid2
