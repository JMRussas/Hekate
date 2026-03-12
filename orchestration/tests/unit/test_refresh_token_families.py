#  Orchestration Engine - Refresh Token Family Tests
#
#  Tests for finding #5: refresh token rotation with reuse detection.
#
#  Depends on: backend/services/auth.py, backend/db/connection.py
#  Used by:    pytest

import time

import pytest

from backend.services.auth import AuthService


class TestRefreshTokenFamilies:
    """Test token family tracking: rotation, reuse detection, revocation."""

    async def test_login_creates_family_record(self, auth_service, tmp_db):
        """Login should create a refresh token family record in DB."""
        await auth_service.register("family@test.com", "password123")
        result = await auth_service.login("family@test.com", "password123")

        token_hash = AuthService._hash_token(result["refresh_token"])
        record = await tmp_db.fetchone(
            "SELECT * FROM refresh_token_families WHERE token_hash = ?",
            (token_hash,),
        )
        assert record is not None
        assert record["user_id"] == result["user"]["id"]
        assert record["is_revoked"] == 0

    async def test_refresh_rotates_within_family(self, auth_service, tmp_db):
        """Refresh should consume old token and issue new one in same family."""
        await auth_service.register("rotate@test.com", "password123")
        login_result = await auth_service.login("rotate@test.com", "password123")

        old_token = login_result["refresh_token"]
        old_hash = AuthService._hash_token(old_token)

        # Refresh
        new_result = await auth_service.refresh_tokens(old_token)

        # Old token should be revoked
        old_record = await tmp_db.fetchone(
            "SELECT is_revoked FROM refresh_token_families WHERE token_hash = ?",
            (old_hash,),
        )
        assert old_record["is_revoked"] == 1

        # New token should exist in same family
        new_hash = AuthService._hash_token(new_result["refresh_token"])
        new_record = await tmp_db.fetchone(
            "SELECT * FROM refresh_token_families WHERE token_hash = ?",
            (new_hash,),
        )
        assert new_record is not None
        assert new_record["is_revoked"] == 0

        # Same family
        old_family = await tmp_db.fetchone(
            "SELECT family_id FROM refresh_token_families WHERE token_hash = ?",
            (old_hash,),
        )
        assert new_record["family_id"] == old_family["family_id"]

    async def test_consumed_token_reuse_revokes_family(self, auth_service, tmp_db):
        """Using a consumed (already-rotated) token should revoke the entire family."""
        await auth_service.register("reuse@test.com", "password123")
        login_result = await auth_service.login("reuse@test.com", "password123")

        old_token = login_result["refresh_token"]

        # First refresh: consumes old_token
        await auth_service.refresh_tokens(old_token)

        # Second refresh with same old_token: token is now revoked (consumed)
        with pytest.raises(ValueError, match="revoked"):
            await auth_service.refresh_tokens(old_token)

        # All tokens in the family should be revoked
        old_hash = AuthService._hash_token(old_token)
        old_record = await tmp_db.fetchone(
            "SELECT family_id FROM refresh_token_families WHERE token_hash = ?",
            (old_hash,),
        )
        family_records = await tmp_db.fetchall(
            "SELECT is_revoked FROM refresh_token_families WHERE family_id = ?",
            (old_record["family_id"],),
        )
        assert all(r["is_revoked"] == 1 for r in family_records)

    async def test_revoked_token_raises(self, auth_service, tmp_db):
        """Using a revoked token should raise and revoke the family."""
        await auth_service.register("revoked@test.com", "password123")
        login_result = await auth_service.login("revoked@test.com", "password123")

        token = login_result["refresh_token"]
        token_hash = AuthService._hash_token(token)

        # Manually revoke the token
        await tmp_db.execute_write(
            "UPDATE refresh_token_families SET is_revoked = 1 WHERE token_hash = ?",
            (token_hash,),
        )

        with pytest.raises(ValueError, match="revoked"):
            await auth_service.refresh_tokens(token)

    async def test_legacy_token_graceful_migration(self, auth_service, tmp_db):
        """Tokens without `fid` claim should be accepted and create a new family."""
        await auth_service.register("legacy@test.com", "password123")
        user = await tmp_db.fetchone("SELECT id, role FROM users WHERE email = 'legacy@test.com'")

        # Create a legacy token (no fid)
        from datetime import datetime, timedelta, timezone
        import jwt
        from backend.config import AUTH_SECRET_KEY, AUTH_ALGORITHM, AUTH_REFRESH_TOKEN_EXPIRE_DAYS

        expire = datetime.now(timezone.utc) + timedelta(days=AUTH_REFRESH_TOKEN_EXPIRE_DAYS)
        legacy_payload = {
            "sub": user["id"],
            "type": "refresh",
            "exp": expire,
        }
        legacy_token = jwt.encode(legacy_payload, AUTH_SECRET_KEY, algorithm=AUTH_ALGORITHM)

        # Refresh with legacy token should work
        result = await auth_service.refresh_tokens(legacy_token)
        assert "access_token" in result
        assert "refresh_token" in result

        # New token should have a family record
        new_hash = AuthService._hash_token(result["refresh_token"])
        record = await tmp_db.fetchone(
            "SELECT * FROM refresh_token_families WHERE token_hash = ?",
            (new_hash,),
        )
        assert record is not None

    async def test_revoke_user_tokens(self, auth_service, tmp_db):
        """revoke_user_tokens should revoke all families for a user."""
        await auth_service.register("revoke_all@test.com", "password123")
        login1 = await auth_service.login("revoke_all@test.com", "password123")
        await auth_service.login("revoke_all@test.com", "password123")

        user_id = login1["user"]["id"]
        count = await auth_service.revoke_user_tokens(user_id)
        assert count == 2

        # All should be revoked
        records = await tmp_db.fetchall(
            "SELECT is_revoked FROM refresh_token_families WHERE user_id = ?",
            (user_id,),
        )
        assert all(r["is_revoked"] == 1 for r in records)

    async def test_cleanup_expired_tokens(self, auth_service, tmp_db):
        """cleanup_expired_tokens should remove old records."""
        await auth_service.register("cleanup@test.com", "password123")
        login_result = await auth_service.login("cleanup@test.com", "password123")

        # Manually backdate the token to be expired
        token_hash = AuthService._hash_token(login_result["refresh_token"])
        await tmp_db.execute_write(
            "UPDATE refresh_token_families SET expires_at = ? WHERE token_hash = ?",
            (time.time() - 1, token_hash),
        )

        count = await auth_service.cleanup_expired_tokens()
        assert count == 1

        # Record should be gone
        record = await tmp_db.fetchone(
            "SELECT * FROM refresh_token_families WHERE token_hash = ?",
            (token_hash,),
        )
        assert record is None

    async def test_refresh_token_has_family_id(self):
        """Refresh tokens should include a fid claim."""
        token, family_id = AuthService.create_refresh_token("user123")
        payload = AuthService.decode_token(token)
        assert payload["fid"] == family_id
        assert len(family_id) == 32  # uuid hex
