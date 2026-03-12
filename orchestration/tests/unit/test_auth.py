#  Orchestration Engine - Auth Service Tests
#
#  Tests for password hashing, JWT encode/decode, registration, and login.
#
#  Depends on: backend/services/auth.py, backend/db/connection.py
#  Used by:    pytest

import time
from unittest.mock import patch

import jwt
import pytest

from backend.services.auth import AuthService


class TestPasswordHashing:
    def test_hash_and_verify_roundtrip(self):
        password = "mysecretpassword"
        hashed = AuthService.hash_password(password)
        assert AuthService.verify_password(password, hashed)

    def test_wrong_password_fails(self):
        hashed = AuthService.hash_password("correct")
        assert not AuthService.verify_password("wrong", hashed)

    def test_different_hashes_for_same_password(self):
        h1 = AuthService.hash_password("same")
        h2 = AuthService.hash_password("same")
        assert h1 != h2  # bcrypt salts differ


class TestJWT:
    def test_access_token_roundtrip(self):
        token = AuthService.create_access_token("user123", "admin")
        payload = AuthService.decode_token(token)
        assert payload["sub"] == "user123"
        assert payload["role"] == "admin"
        assert payload["type"] == "access"

    def test_refresh_token_roundtrip(self):
        token, family_id = AuthService.create_refresh_token("user456")
        payload = AuthService.decode_token(token)
        assert payload["sub"] == "user456"
        assert payload["type"] == "refresh"
        assert payload["fid"] == family_id

    def test_expired_token_raises(self):
        with patch("backend.services.auth.AUTH_ACCESS_TOKEN_EXPIRE_MINUTES", -1):
            token = AuthService.create_access_token("user", "user")
        with pytest.raises(jwt.ExpiredSignatureError):
            AuthService.decode_token(token)

    def test_invalid_token_raises(self):
        with pytest.raises(jwt.PyJWTError):
            AuthService.decode_token("not.a.valid.token")


class TestRegister:
    async def test_first_user_is_admin(self, auth_service):
        user = await auth_service.register("admin@test.com", "password123")
        assert user["role"] == "admin"

    async def test_second_user_is_regular(self, auth_service):
        await auth_service.register("first@test.com", "password123")
        user = await auth_service.register("second@test.com", "password123")
        assert user["role"] == "user"

    async def test_duplicate_email_raises(self, auth_service):
        await auth_service.register("dupe@test.com", "password123")
        with pytest.raises(ValueError, match="Registration failed"):
            await auth_service.register("dupe@test.com", "password456")

    async def test_display_name_defaults_to_email_prefix(self, auth_service):
        user = await auth_service.register("jane@example.com", "password123")
        assert user["display_name"] == "jane"


class TestLogin:
    async def test_successful_login(self, auth_service):
        await auth_service.register("user@test.com", "password123")
        result = await auth_service.login("user@test.com", "password123")
        assert "access_token" in result
        assert "refresh_token" in result
        assert result["user"]["email"] == "user@test.com"

    async def test_wrong_password_raises(self, auth_service):
        await auth_service.register("user@test.com", "password123")
        with pytest.raises(ValueError, match="Invalid email or password"):
            await auth_service.login("user@test.com", "wrongpass")

    async def test_nonexistent_user_raises(self, auth_service):
        with pytest.raises(ValueError, match="Invalid email or password"):
            await auth_service.login("nobody@test.com", "password123")

    async def test_login_updates_last_login(self, auth_service):
        await auth_service.register("user@test.com", "password123")
        before = time.time()
        await auth_service.login("user@test.com", "password123")
        row = await auth_service._db.fetchone(
            "SELECT last_login_at FROM users WHERE email = ?", ("user@test.com",)
        )
        assert row["last_login_at"] >= before


class TestRefresh:
    async def test_refresh_returns_new_tokens(self, auth_service):
        await auth_service.register("user@test.com", "password123")
        login_result = await auth_service.login("user@test.com", "password123")
        result = await auth_service.refresh_tokens(login_result["refresh_token"])
        assert "access_token" in result
        assert "refresh_token" in result

    async def test_access_token_rejected_as_refresh(self, auth_service):
        await auth_service.register("user@test.com", "password123")
        login_result = await auth_service.login("user@test.com", "password123")
        with pytest.raises(ValueError, match="not a refresh token"):
            await auth_service.refresh_tokens(login_result["access_token"])


class TestOAuthOnlyUser:
    async def test_register_without_password(self, auth_service):
        user = await auth_service.register("oauth@test.com", password=None)
        assert user["email"] == "oauth@test.com"
        # Verify password_hash is NULL in DB
        row = await auth_service._db.fetchone(
            "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
        )
        assert row["password_hash"] is None

    async def test_login_oauth_only_user_rejected(self, auth_service):
        await auth_service.register("oauth@test.com", password=None)
        with pytest.raises(ValueError, match="OAuth login"):
            await auth_service.login("oauth@test.com", "anypassword")

    async def test_set_password_on_oauth_user(self, auth_service):
        user = await auth_service.register("oauth@test.com", password=None)
        await auth_service.set_password(user["id"], "newpassword123")
        # Now login should work
        result = await auth_service.login("oauth@test.com", "newpassword123")
        assert result["user"]["email"] == "oauth@test.com"

    async def test_get_user_includes_has_password(self, auth_service):
        # Password user
        pw_user = await auth_service.register("pw@test.com", "password123")
        fetched = await auth_service.get_user(pw_user["id"])
        assert fetched["has_password"] == 1  # SQLite returns int for expression

        # OAuth-only user
        oauth_user = await auth_service.register("oauth@test.com", password=None)
        fetched = await auth_service.get_user(oauth_user["id"])
        assert fetched["has_password"] == 0

    async def test_get_user_includes_linked_providers(self, auth_service, tmp_db):
        import uuid as _uuid
        import time as _time
        user = await auth_service.register("linked@test.com", "password123")
        # Add an identity
        await tmp_db.execute_write(
            "INSERT INTO user_identities (id, user_id, provider, provider_user_id, provider_email, created_at) "
            "VALUES (?, ?, 'google', 'gid-123', 'linked@test.com', ?)",
            (str(_uuid.uuid4()), user["id"], _time.time()),
        )
        fetched = await auth_service.get_user(user["id"])
        assert "google" in fetched["linked_providers"]
