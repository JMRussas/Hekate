#  Orchestration Engine - SSE Token Tests
#
#  Tests for short-lived SSE token creation and validation.
#
#  Depends on: backend/services/auth.py
#  Used by:    pytest

import time
from unittest.mock import patch

import jwt
import pytest

from backend.services.auth import AuthService


class TestSSETokens:
    def test_sse_token_roundtrip(self):
        token = AuthService.create_sse_token("user123", "proj_abc")
        payload = AuthService.decode_token(token)
        assert payload["sub"] == "user123"
        assert payload["type"] == "sse"
        assert payload["project_id"] == "proj_abc"

    def test_sse_token_expires(self):
        with patch("backend.services.auth.AUTH_SSE_TOKEN_EXPIRE_SECONDS", -1):
            token = AuthService.create_sse_token("user", "proj")
        with pytest.raises(jwt.ExpiredSignatureError):
            AuthService.decode_token(token)

    def test_sse_token_has_short_ttl(self):
        token = AuthService.create_sse_token("user", "proj")
        payload = AuthService.decode_token(token)
        # Should expire within 120 seconds (default is 60, with some margin)
        assert payload["exp"] - time.time() < 120


class TestEmailValidation:
    def test_invalid_emails_rejected(self):
        from pydantic import ValidationError
        from backend.models.schemas import RegisterRequest

        for bad_email in ["abc", "@", "a@b", "not-an-email"]:
            with pytest.raises(ValidationError):
                RegisterRequest(email=bad_email, password="password123")

    def test_valid_email_accepted(self):
        from backend.models.schemas import RegisterRequest
        req = RegisterRequest(email="user@example.com", password="password123")
        assert req.email == "user@example.com"
