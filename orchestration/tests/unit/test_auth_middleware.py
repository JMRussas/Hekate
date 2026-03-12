#  Orchestration Engine - Auth Middleware Tests
#
#  Tests for JWT validation dependencies: _validate_token, get_current_user,
#  require_admin, get_user_from_sse_token.
#
#  Depends on: backend/middleware/auth.py, backend/services/auth.py
#  Used by:    pytest

import pytest
from fastapi import HTTPException

from backend.middleware.auth import (
    _validate_token,
    get_current_user,
    get_user_from_sse_token,
    require_admin,
)
from backend.services.auth import AuthService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_user(auth: AuthService, email="test@example.com",
                     password="testpass123", display_name="Test User"):
    """Register a user and return (user_dict, user_id)."""
    result = await auth.register(email, password, display_name)
    user_id = result["id"]
    user = await auth.get_user(user_id)
    return user, user_id


# ---------------------------------------------------------------------------
# _validate_token
# ---------------------------------------------------------------------------

class TestValidateToken:
    async def test_valid_access_token(self, tmp_db):
        """Valid access token returns (user, payload)."""
        auth = AuthService(db=tmp_db)
        user, user_id = await _make_user(auth)

        token = AuthService.create_access_token(user_id, user["role"])
        result_user, payload = await _validate_token(auth, token, "access")

        assert result_user["id"] == user_id
        assert payload["type"] == "access"
        assert payload["sub"] == user_id

    async def test_malformed_token(self, tmp_db):
        """Malformed JWT raises 401."""
        auth = AuthService(db=tmp_db)
        with pytest.raises(HTTPException) as exc_info:
            await _validate_token(auth, "not.a.jwt", "access")
        assert exc_info.value.status_code == 401
        assert "Invalid or expired" in exc_info.value.detail

    async def test_wrong_token_type(self, tmp_db):
        """SSE token passed as access type raises 401."""
        auth = AuthService(db=tmp_db)
        user, user_id = await _make_user(auth)

        sse_token = AuthService.create_sse_token(user_id, "proj_001")
        with pytest.raises(HTTPException) as exc_info:
            await _validate_token(auth, sse_token, "access")
        assert exc_info.value.status_code == 401
        assert "Invalid token type" in exc_info.value.detail

    async def test_user_not_found(self, tmp_db):
        """Token for nonexistent user raises 401."""
        auth = AuthService(db=tmp_db)
        token = AuthService.create_access_token("nonexistent_user", "user")
        with pytest.raises(HTTPException) as exc_info:
            await _validate_token(auth, token, "access")
        assert exc_info.value.status_code == 401
        assert "User not found" in exc_info.value.detail


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------

class TestGetCurrentUser:
    async def test_no_credentials(self, tmp_db):
        """Missing credentials raises 401."""
        auth = AuthService(db=tmp_db)
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None, auth=auth)
        assert exc_info.value.status_code == 401
        assert "Not authenticated" in exc_info.value.detail


# ---------------------------------------------------------------------------
# require_admin
# ---------------------------------------------------------------------------

class TestRequireAdmin:
    async def test_non_admin(self):
        """Non-admin user raises 403."""
        user = {"id": "u1", "role": "user"}
        with pytest.raises(HTTPException) as exc_info:
            await require_admin(user=user)
        assert exc_info.value.status_code == 403
        assert "Admin access required" in exc_info.value.detail

    async def test_admin(self):
        """Admin user passes through."""
        user = {"id": "u1", "role": "admin"}
        result = await require_admin(user=user)
        assert result["role"] == "admin"


# ---------------------------------------------------------------------------
# get_user_from_sse_token
# ---------------------------------------------------------------------------

class TestGetUserFromSseToken:
    async def test_valid_sse_token(self, tmp_db):
        """SSE token for correct project returns user."""
        auth = AuthService(db=tmp_db)
        user, user_id = await _make_user(auth)

        sse_token = AuthService.create_sse_token(user_id, "proj_001")
        result = await get_user_from_sse_token(
            project_id="proj_001", token=sse_token, auth=auth
        )
        assert result["id"] == user_id

    async def test_sse_token_wrong_project(self, tmp_db):
        """SSE token for project A rejected on project B → 403."""
        auth = AuthService(db=tmp_db)
        user, user_id = await _make_user(auth)

        sse_token = AuthService.create_sse_token(user_id, "proj_001")
        with pytest.raises(HTTPException) as exc_info:
            await get_user_from_sse_token(
                project_id="proj_002", token=sse_token, auth=auth
            )
        assert exc_info.value.status_code == 403
        assert "not valid for this project" in exc_info.value.detail

    async def test_unknown_token_type(self, tmp_db):
        """Refresh token (type=refresh) raises 401."""
        auth = AuthService(db=tmp_db)
        user, user_id = await _make_user(auth)

        refresh_token, _ = AuthService.create_refresh_token(user_id)
        with pytest.raises(HTTPException) as exc_info:
            await get_user_from_sse_token(
                project_id="proj_001", token=refresh_token, auth=auth
            )
        assert exc_info.value.status_code == 401
        assert "Invalid token type" in exc_info.value.detail

    async def test_access_token_rejected_for_sse(self, tmp_db):
        """Access tokens must not be accepted for SSE endpoints."""
        auth = AuthService(db=tmp_db)
        user, user_id = await _make_user(auth, email="sse_access@example.com")

        access_token = AuthService.create_access_token(user_id, user["role"])
        with pytest.raises(HTTPException) as exc_info:
            await get_user_from_sse_token(
                project_id="proj_001", token=access_token, auth=auth
            )
        assert exc_info.value.status_code == 401
        assert "Invalid token type" in exc_info.value.detail
