#  Orchestration Engine - OIDC Service Unit Tests
#
#  Tests for OIDCService: provider listing, login, link, unlink.
#
#  Depends on: backend/services/oidc.py, backend/services/auth.py, tests/conftest.py
#  Used by:    pytest

import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from backend.exceptions import AccountLinkError, NotFoundError, OIDCError
from backend.services.auth import AuthService
from backend.services.oidc import OIDCService


@pytest.fixture
async def oidc_service(tmp_db):
    """OIDCService with one test provider configured."""
    test_providers = [
        {
            "name": "testprov",
            "display_name": "Test Provider",
            "issuer": "https://test.example.com",
            "client_id": "test-client-id",
            "client_secret": "test-secret",
            "scopes": ["openid", "email", "profile"],
            "auto_link_by_email": True,
        }
    ]
    auth = AuthService(db=tmp_db)
    with patch("backend.services.oidc.AUTH_OIDC_PROVIDERS", test_providers):
        svc = OIDCService(db=tmp_db, auth=auth)
    return svc


@pytest.fixture
async def oidc_no_providers(tmp_db):
    """OIDCService with no providers configured."""
    auth = AuthService(db=tmp_db)
    with patch("backend.services.oidc.AUTH_OIDC_PROVIDERS", []):
        svc = OIDCService(db=tmp_db, auth=auth)
    return svc


# Helper: create a user directly in the DB
async def _create_user(db, email, password_hash=None, role="user"):
    uid = str(uuid.uuid4())
    now = time.time()
    await db.execute_write(
        "INSERT INTO users (id, email, password_hash, display_name, role, is_active, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (uid, email, password_hash, email.split("@")[0], role, now),
    )
    return uid


class TestProviderListing:
    async def test_returns_configured_providers(self, oidc_service):
        providers = oidc_service.get_available_providers()
        assert len(providers) == 1
        assert providers[0]["name"] == "testprov"
        assert providers[0]["display_name"] == "Test Provider"

    async def test_empty_when_none_configured(self, oidc_no_providers):
        providers = oidc_no_providers.get_available_providers()
        assert providers == []

    async def test_unknown_provider_raises(self, oidc_service):
        with pytest.raises(NotFoundError, match="not configured"):
            oidc_service._get_provider("nonexistent")


class TestOIDCLogin:
    async def test_creates_new_user(self, oidc_service, tmp_db):
        mock_claims = {
            "provider_user_id": "google-uid-123",
            "email": "new@test.com",
            "email_verified": True,
            "display_name": "New User",
            "provider_name": "testprov",
        }
        with patch.object(oidc_service, "exchange_code", new_callable=AsyncMock, return_value=mock_claims):
            result = await oidc_service.oidc_login("testprov", "code", "https://redir", "nonce")

        assert "access_token" in result
        assert "refresh_token" in result
        assert result["user"]["email"] == "new@test.com"
        # First user should be admin
        assert result["user"]["role"] == "admin"

        # Verify identity was created
        rows = await tmp_db.fetchall("SELECT * FROM user_identities")
        assert len(rows) == 1
        assert rows[0]["provider"] == "testprov"
        assert rows[0]["provider_user_id"] == "google-uid-123"

    async def test_auto_links_existing_user(self, oidc_service, tmp_db):
        uid = await _create_user(tmp_db, "existing@test.com", "somehash", role="admin")

        mock_claims = {
            "provider_user_id": "prov-uid-456",
            "email": "existing@test.com",
            "email_verified": True,
            "display_name": "Existing",
            "provider_name": "testprov",
        }
        with patch.object(oidc_service, "exchange_code", new_callable=AsyncMock, return_value=mock_claims):
            result = await oidc_service.oidc_login("testprov", "code", "https://redir", "nonce")

        assert result["user"]["id"] == uid
        assert result["user"]["email"] == "existing@test.com"

        # Verify identity was linked to the existing user
        rows = await tmp_db.fetchall("SELECT * FROM user_identities")
        assert len(rows) == 1
        assert rows[0]["user_id"] == uid

    async def test_existing_identity_returns_tokens(self, oidc_service, tmp_db):
        uid = await _create_user(tmp_db, "linked@test.com", "somehash", role="admin")
        # Pre-create identity
        await tmp_db.execute_write(
            "INSERT INTO user_identities (id, user_id, provider, provider_user_id, provider_email, created_at) "
            "VALUES (?, ?, 'testprov', 'prov-uid-789', 'linked@test.com', ?)",
            (str(uuid.uuid4()), uid, time.time()),
        )

        mock_claims = {
            "provider_user_id": "prov-uid-789",
            "email": "linked@test.com",
            "email_verified": True,
            "display_name": "Linked",
            "provider_name": "testprov",
        }
        with patch.object(oidc_service, "exchange_code", new_callable=AsyncMock, return_value=mock_claims):
            result = await oidc_service.oidc_login("testprov", "code", "https://redir", "nonce")

        assert result["user"]["id"] == uid
        # Should NOT have created a duplicate identity
        rows = await tmp_db.fetchall("SELECT * FROM user_identities")
        assert len(rows) == 1

    async def test_rejects_unverified_email(self, oidc_service):
        mock_claims = {
            "provider_user_id": "uid",
            "email": "unverified@test.com",
            "email_verified": False,
            "display_name": "Bad",
            "provider_name": "testprov",
        }
        with patch.object(oidc_service, "exchange_code", new_callable=AsyncMock, return_value=mock_claims):
            with pytest.raises(OIDCError, match="Email not verified"):
                await oidc_service.oidc_login("testprov", "code", "https://redir", "nonce")


class TestAutoLinkDisabledByDefault:
    """C1: auto_link_by_email defaults to False — prevents privilege escalation."""

    @pytest.fixture
    async def oidc_default_autolink(self, tmp_db):
        """OIDCService with auto_link_by_email omitted (should default to False)."""
        test_providers = [
            {
                "name": "testprov",
                "display_name": "Test Provider",
                "issuer": "https://test.example.com",
                "client_id": "test-client-id",
                "client_secret": "test-secret",
                # auto_link_by_email intentionally omitted
            }
        ]
        auth = AuthService(db=tmp_db)
        with patch("backend.services.oidc.AUTH_OIDC_PROVIDERS", test_providers):
            svc = OIDCService(db=tmp_db, auth=auth)
        return svc

    async def test_no_autolink_does_not_link_to_existing(self, oidc_default_autolink, tmp_db):
        """With auto_link off, matching email should NOT silently link to existing user.
        Instead, the INSERT fails with UNIQUE constraint, preventing privilege escalation."""
        await _create_user(tmp_db, "victim@test.com", "somehash", role="admin")

        mock_claims = {
            "provider_user_id": "attacker-uid",
            "email": "victim@test.com",
            "email_verified": True,
            "display_name": "Attacker",
            "provider_name": "testprov",
        }
        with patch.object(oidc_default_autolink, "exchange_code", new_callable=AsyncMock, return_value=mock_claims):
            # Cannot create duplicate email — UNIQUE constraint prevents it
            with pytest.raises(Exception):
                await oidc_default_autolink.oidc_login("testprov", "code", "https://redir", "nonce")

    async def test_no_autolink_new_email_creates_user(self, oidc_default_autolink, tmp_db):
        """With auto_link off, a genuinely new email creates a new user."""
        await _create_user(tmp_db, "existing@test.com", "somehash", role="admin")

        mock_claims = {
            "provider_user_id": "new-uid",
            "email": "newuser@test.com",
            "email_verified": True,
            "display_name": "New User",
            "provider_name": "testprov",
        }
        with patch.object(oidc_default_autolink, "exchange_code", new_callable=AsyncMock, return_value=mock_claims):
            result = await oidc_default_autolink.oidc_login("testprov", "code", "https://redir", "nonce")

        assert result["user"]["email"] == "newuser@test.com"
        assert result["user"]["role"] == "user"  # Not admin (second user)


class TestDeactivatedUserOIDC:
    """H1: Deactivated users must not be able to log in via OIDC."""

    async def test_deactivated_user_existing_identity(self, oidc_service, tmp_db):
        uid = await _create_user(tmp_db, "deactivated@test.com", "somehash", role="user")
        await tmp_db.execute_write("UPDATE users SET is_active = 0 WHERE id = ?", (uid,))
        await tmp_db.execute_write(
            "INSERT INTO user_identities (id, user_id, provider, provider_user_id, provider_email, created_at) "
            "VALUES (?, ?, 'testprov', 'deact-uid', 'deactivated@test.com', ?)",
            (str(uuid.uuid4()), uid, time.time()),
        )

        mock_claims = {
            "provider_user_id": "deact-uid",
            "email": "deactivated@test.com",
            "email_verified": True,
            "display_name": "Deactivated",
            "provider_name": "testprov",
        }
        with patch.object(oidc_service, "exchange_code", new_callable=AsyncMock, return_value=mock_claims):
            with pytest.raises(OIDCError, match="deactivated"):
                await oidc_service.oidc_login("testprov", "code", "https://redir", "nonce")

    async def test_deactivated_user_autolinked(self, oidc_service, tmp_db):
        uid = await _create_user(tmp_db, "deact2@test.com", "somehash", role="user")
        await tmp_db.execute_write("UPDATE users SET is_active = 0 WHERE id = ?", (uid,))
        # Force auto_link on for this specific test
        oidc_service._providers["testprov"]["auto_link_by_email"] = True

        mock_claims = {
            "provider_user_id": "deact2-uid",
            "email": "deact2@test.com",
            "email_verified": True,
            "display_name": "Deact2",
            "provider_name": "testprov",
        }
        with patch.object(oidc_service, "exchange_code", new_callable=AsyncMock, return_value=mock_claims):
            with pytest.raises(OIDCError, match="deactivated"):
                await oidc_service.oidc_login("testprov", "code", "https://redir", "nonce")

        # Verify no orphaned identity link was created
        row = await tmp_db.fetchone(
            "SELECT COUNT(*) as cnt FROM user_identities WHERE provider_user_id = ?",
            ("deact2-uid",),
        )
        assert row["cnt"] == 0, "Identity link should not be created for deactivated users"


class TestRedirectURIValidation:
    """H2: Redirect URIs must be validated against an allowlist."""

    async def test_disallowed_redirect_uri(self, tmp_db):
        test_providers = [
            {
                "name": "testprov",
                "issuer": "https://test.example.com",
                "client_id": "test-client-id",
                "client_secret": "test-secret",
            }
        ]
        auth = AuthService(db=tmp_db)
        with patch("backend.services.oidc.AUTH_OIDC_PROVIDERS", test_providers):
            svc = OIDCService(db=tmp_db, auth=auth)

        with patch("backend.services.oidc.AUTH_OIDC_REDIRECT_URIS", ["https://allowed.com/callback"]):
            with pytest.raises(OIDCError, match="Redirect URI not allowed"):
                await svc.get_authorization_url("testprov", "https://attacker.com/phish")

    async def test_allowed_redirect_uri(self, tmp_db):
        test_providers = [
            {
                "name": "testprov",
                "issuer": "https://test.example.com",
                "client_id": "test-client-id",
                "client_secret": "test-secret",
            }
        ]
        auth = AuthService(db=tmp_db)
        with patch("backend.services.oidc.AUTH_OIDC_PROVIDERS", test_providers):
            svc = OIDCService(db=tmp_db, auth=auth)

        with patch("backend.services.oidc.AUTH_OIDC_REDIRECT_URIS", ["https://allowed.com/callback"]), \
             patch.object(svc, "_fetch_metadata", new_callable=AsyncMock, return_value={
                 "authorization_endpoint": "https://test.example.com/authorize"
             }), patch.object(svc, "_make_client") as mock_client:
            mock_client.return_value.create_authorization_url.return_value = ("https://test.example.com/authorize?x=1", "state")
            url, state, nonce = await svc.get_authorization_url("testprov", "https://allowed.com/callback")
            assert url is not None

    async def test_empty_allowlist_allows_all(self, tmp_db):
        test_providers = [
            {
                "name": "testprov",
                "issuer": "https://test.example.com",
                "client_id": "test-client-id",
                "client_secret": "test-secret",
            }
        ]
        auth = AuthService(db=tmp_db)
        with patch("backend.services.oidc.AUTH_OIDC_PROVIDERS", test_providers):
            svc = OIDCService(db=tmp_db, auth=auth)

        with patch("backend.services.oidc.AUTH_OIDC_REDIRECT_URIS", []), \
             patch.object(svc, "_fetch_metadata", new_callable=AsyncMock, return_value={
                 "authorization_endpoint": "https://test.example.com/authorize"
             }), patch.object(svc, "_make_client") as mock_client:
            mock_client.return_value.create_authorization_url.return_value = ("https://url", "state")
            url, state, nonce = await svc.get_authorization_url("testprov", "https://any-uri.com/cb")
            assert url is not None


class TestLinkProvider:
    async def test_link_success(self, oidc_service, tmp_db):
        uid = await _create_user(tmp_db, "link@test.com", "somehash", role="admin")
        mock_claims = {
            "provider_user_id": "new-prov-uid",
            "email": "link@test.com",
            "email_verified": True,
            "display_name": "Link",
            "provider_name": "testprov",
        }
        with patch.object(oidc_service, "exchange_code", new_callable=AsyncMock, return_value=mock_claims):
            result = await oidc_service.link_provider(uid, "testprov", "code", "https://redir", "nonce")

        assert result["provider"] == "testprov"
        assert result["provider_user_id"] == "new-prov-uid"

    async def test_link_already_linked_to_other_user(self, oidc_service, tmp_db):
        uid_a = await _create_user(tmp_db, "a@test.com", "hash", role="admin")
        uid_b = await _create_user(tmp_db, "b@test.com", "hash")

        # Link to user A first
        await tmp_db.execute_write(
            "INSERT INTO user_identities (id, user_id, provider, provider_user_id, provider_email, created_at) "
            "VALUES (?, ?, 'testprov', 'shared-prov-uid', 'a@test.com', ?)",
            (str(uuid.uuid4()), uid_a, time.time()),
        )

        mock_claims = {
            "provider_user_id": "shared-prov-uid",
            "email": "b@test.com",
            "email_verified": True,
            "display_name": "B",
            "provider_name": "testprov",
        }
        with patch.object(oidc_service, "exchange_code", new_callable=AsyncMock, return_value=mock_claims):
            with pytest.raises(AccountLinkError, match="already linked to another user"):
                await oidc_service.link_provider(uid_b, "testprov", "code", "https://redir", "nonce")


class TestUnlinkProvider:
    async def test_unlink_success(self, oidc_service, tmp_db):
        uid = await _create_user(tmp_db, "unlink@test.com", "somehash", role="admin")
        await tmp_db.execute_write(
            "INSERT INTO user_identities (id, user_id, provider, provider_user_id, provider_email, created_at) "
            "VALUES (?, ?, 'testprov', 'uid-123', 'unlink@test.com', ?)",
            (str(uuid.uuid4()), uid, time.time()),
        )

        await oidc_service.unlink_provider(uid, "testprov")
        rows = await tmp_db.fetchall("SELECT * FROM user_identities WHERE user_id = ?", (uid,))
        assert len(rows) == 0

    async def test_unlink_blocked_when_only_auth_method(self, oidc_service, tmp_db):
        # OAuth-only user (no password)
        uid = await _create_user(tmp_db, "oauth@test.com", password_hash=None)
        await tmp_db.execute_write(
            "INSERT INTO user_identities (id, user_id, provider, provider_user_id, provider_email, created_at) "
            "VALUES (?, ?, 'testprov', 'uid-only', 'oauth@test.com', ?)",
            (str(uuid.uuid4()), uid, time.time()),
        )

        with pytest.raises(AccountLinkError, match="Cannot unlink"):
            await oidc_service.unlink_provider(uid, "testprov")

    async def test_unlink_not_found(self, oidc_service, tmp_db):
        uid = await _create_user(tmp_db, "noprov@test.com", "somehash", role="admin")

        with pytest.raises(NotFoundError, match="No linked identity"):
            await oidc_service.unlink_provider(uid, "testprov")


class TestGetIdentities:
    async def test_returns_linked_identities(self, oidc_service, tmp_db):
        uid = await _create_user(tmp_db, "multi@test.com", "somehash", role="admin")
        await tmp_db.execute_write(
            "INSERT INTO user_identities (id, user_id, provider, provider_user_id, provider_email, created_at) "
            "VALUES (?, ?, 'testprov', 'uid-a', 'multi@test.com', ?)",
            (str(uuid.uuid4()), uid, time.time()),
        )

        identities = await oidc_service.get_user_identities(uid)
        assert len(identities) == 1
        assert identities[0]["provider"] == "testprov"

    async def test_returns_empty_for_no_identities(self, oidc_service, tmp_db):
        uid = await _create_user(tmp_db, "noid@test.com", "somehash", role="admin")
        identities = await oidc_service.get_user_identities(uid)
        assert identities == []
