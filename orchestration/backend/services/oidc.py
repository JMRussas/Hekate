#  Orchestration Engine - OIDC Service
#
#  Generic OIDC provider support using authlib.
#  Handles discovery, authorization URL generation, code exchange,
#  user creation/linking, and provider management.
#
#  Depends on: backend/db/connection.py, backend/services/auth.py, backend/config.py
#  Used by:    container.py, routes/auth_oidc.py

import logging
import secrets
import time
import uuid

from authlib.integrations.httpx_client import AsyncOAuth2Client

from backend.config import AUTH_OIDC_PROVIDERS, AUTH_OIDC_REDIRECT_URIS, AUTH_REFRESH_TOKEN_EXPIRE_DAYS
from backend.db.connection import Database
from backend.exceptions import AccountLinkError, NotFoundError, OIDCError
from backend.services.auth import AuthService

logger = logging.getLogger("orchestration.oidc")

_METADATA_TTL = 3600  # 1 hour cache for OIDC discovery docs


class OIDCService:
    """Generic OIDC authentication for any compliant provider."""

    def __init__(self, db: Database, auth: AuthService):
        self._db = db
        self._auth = auth
        self._providers: dict[str, dict] = {}
        self._metadata_cache: dict[str, dict] = {}
        self._metadata_expiry: dict[str, float] = {}

        for prov in AUTH_OIDC_PROVIDERS:
            name = prov.get("name")
            if name and prov.get("issuer") and prov.get("client_id"):
                self._providers[name] = prov
                logger.info("OIDC provider registered: %s", name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_available_providers(self) -> list[dict]:
        """Return public info about configured providers (no secrets)."""
        return [
            {"name": p["name"], "display_name": p.get("display_name", p["name"])}
            for p in self._providers.values()
        ]

    async def get_authorization_url(
        self, provider_name: str, redirect_uri: str
    ) -> tuple[str, str, str]:
        """Build the authorization URL for the given provider.

        Returns (authorization_url, state, nonce).
        """
        prov = self._get_provider(provider_name)

        # Validate redirect_uri against allowlist (if configured)
        if AUTH_OIDC_REDIRECT_URIS and redirect_uri not in AUTH_OIDC_REDIRECT_URIS:
            raise OIDCError("Redirect URI not allowed")
        metadata = await self._fetch_metadata(prov)

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(16)
        scopes = prov.get("scopes", ["openid", "email", "profile"])

        client = self._make_client(prov)
        url, _ = client.create_authorization_url(
            metadata["authorization_endpoint"],
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
            scope=" ".join(scopes),
        )
        return url, state, nonce

    async def exchange_code(
        self, provider_name: str, code: str, redirect_uri: str, nonce: str
    ) -> dict:
        """Exchange authorization code for OIDC claims.

        Returns dict with provider_user_id, email, email_verified, display_name.
        """
        prov = self._get_provider(provider_name)
        metadata = await self._fetch_metadata(prov)
        client = self._make_client(prov)

        token_resp = await client.fetch_token(
            metadata["token_endpoint"],
            code=code,
            redirect_uri=redirect_uri,
            grant_type="authorization_code",
        )

        # Validate ID token (authlib checks signature, iss, aud, exp, nonce)
        userinfo = token_resp.get("userinfo")
        if not userinfo:
            # Fall back to userinfo endpoint
            userinfo_resp = await client.get(
                metadata["userinfo_endpoint"],
                token=token_resp,
            )
            userinfo = userinfo_resp.json()

        return {
            "provider_user_id": userinfo.get("sub", ""),
            "email": userinfo.get("email", ""),
            "email_verified": userinfo.get("email_verified", False),
            "display_name": userinfo.get("name") or userinfo.get("preferred_username", ""),
            "provider_name": provider_name,
        }

    async def oidc_login(
        self, provider_name: str, code: str, redirect_uri: str, nonce: str
    ) -> dict:
        """Full OIDC login flow: exchange code, find/create user, return JWT tokens."""
        claims = await self.exchange_code(provider_name, code, redirect_uri, nonce)

        if not claims.get("email_verified"):
            raise OIDCError("Email not verified by provider — cannot authenticate")

        email = claims["email"]
        provider_uid = claims["provider_user_id"]
        prov = self._providers[provider_name]
        auto_link = prov.get("auto_link_by_email", False)

        async with self._db.transaction() as conn:
            # Check if this provider identity already exists
            existing = await conn.execute(
                "SELECT user_id FROM user_identities "
                "WHERE provider = ? AND provider_user_id = ?",
                (provider_name, provider_uid),
            )
            row = await existing.fetchone()

            if row:
                # Existing linked identity — verify account is active
                user_id = row["user_id"]
                active_check = await conn.execute(
                    "SELECT is_active FROM users WHERE id = ?", (user_id,)
                )
                active_row = await active_check.fetchone()
                if active_row and not active_row["is_active"]:
                    raise OIDCError("Account is deactivated")
            else:
                # New identity — check for existing user by email
                user_row = None
                if auto_link and email:
                    cursor = await conn.execute(
                        "SELECT id FROM users WHERE email = ?", (email,)
                    )
                    user_row = await cursor.fetchone()

                if user_row:
                    # Auto-link to existing user — verify account is active
                    user_id = user_row["id"]
                    active_check = await conn.execute(
                        "SELECT is_active FROM users WHERE id = ?", (user_id,)
                    )
                    active_row = await active_check.fetchone()
                    if active_row and not active_row["is_active"]:
                        raise OIDCError("Account is deactivated")
                else:
                    # Create new user (no password)
                    user_id = str(uuid.uuid4())
                    now = time.time()
                    display = claims.get("display_name") or email.split("@")[0]

                    # First user becomes admin
                    count_cursor = await conn.execute("SELECT COUNT(*) as cnt FROM users")
                    count_row = await count_cursor.fetchone()
                    role = "admin" if count_row[0] == 0 else "user"

                    await conn.execute(
                        "INSERT INTO users (id, email, password_hash, display_name, role, is_active, created_at) "
                        "VALUES (?, ?, NULL, ?, ?, 1, ?)",
                        (user_id, email, display, role, now),
                    )

                # Create identity link
                await conn.execute(
                    "INSERT INTO user_identities (id, user_id, provider, provider_user_id, provider_email, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), user_id, provider_name, provider_uid, email, time.time()),
                )

            # Update last login
            await conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (time.time(), user_id),
            )

        # Fetch full user for token creation
        user = await self._auth.get_user(user_id)
        if not user:
            raise OIDCError("User not found after OIDC login")
        if not user.get("is_active"):
            raise OIDCError("Account is deactivated")

        access_token = self._auth.create_access_token(user["id"], user["role"])
        refresh_token, family_id = self._auth.create_refresh_token(user["id"])

        # Store refresh token for family tracking
        expire_ts = time.time() + (AUTH_REFRESH_TOKEN_EXPIRE_DAYS * 86400)
        await self._auth._store_refresh_token(user["id"], family_id, refresh_token, expire_ts)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": {
                "id": user["id"],
                "email": user["email"],
                "display_name": user["display_name"],
                "role": user["role"],
                "has_password": user.get("has_password", True),
                "linked_providers": user.get("linked_providers", []),
            },
        }

    async def link_provider(
        self, user_id: str, provider_name: str, code: str, redirect_uri: str, nonce: str
    ) -> dict:
        """Link an additional OIDC provider to an existing user account."""
        claims = await self.exchange_code(provider_name, code, redirect_uri, nonce)
        provider_uid = claims["provider_user_id"]

        async with self._db.transaction() as conn:
            # Check if this provider account is already linked to someone else
            existing = await conn.execute(
                "SELECT user_id FROM user_identities "
                "WHERE provider = ? AND provider_user_id = ?",
                (provider_name, provider_uid),
            )
            row = await existing.fetchone()
            if row:
                if row["user_id"] == user_id:
                    raise AccountLinkError("This provider is already linked to your account")
                raise AccountLinkError("This provider account is already linked to another user")

            await conn.execute(
                "INSERT INTO user_identities (id, user_id, provider, provider_user_id, provider_email, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), user_id, provider_name, provider_uid, claims.get("email"), time.time()),
            )

        return {
            "provider": provider_name,
            "provider_user_id": provider_uid,
            "provider_email": claims.get("email"),
        }

    async def unlink_provider(self, user_id: str, provider_name: str) -> None:
        """Remove a linked OIDC provider from a user account."""
        async with self._db.transaction() as conn:
            # Check that the user retains at least one auth method
            user_cursor = await conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            )
            user_row = await user_cursor.fetchone()
            if not user_row:
                raise NotFoundError("User not found")

            count_cursor = await conn.execute(
                "SELECT COUNT(*) as cnt FROM user_identities WHERE user_id = ?",
                (user_id,),
            )
            count_row = await count_cursor.fetchone()

            has_password = user_row["password_hash"] is not None
            identity_count = count_row[0]

            if not has_password and identity_count <= 1:
                raise AccountLinkError(
                    "Cannot unlink the only authentication method. Set a password first."
                )

            cursor = await conn.execute(
                "DELETE FROM user_identities WHERE user_id = ? AND provider = ?",
                (user_id, provider_name),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"No linked identity for provider '{provider_name}'")

    async def get_user_identities(self, user_id: str) -> list[dict]:
        """Return all linked OIDC identities for a user."""
        rows = await self._db.fetchall(
            "SELECT provider, provider_email, created_at FROM user_identities WHERE user_id = ?",
            (user_id,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_provider(self, name: str) -> dict:
        if name not in self._providers:
            raise NotFoundError(f"OIDC provider '{name}' is not configured")
        return self._providers[name]

    def _make_client(self, prov: dict) -> AsyncOAuth2Client:
        return AsyncOAuth2Client(
            client_id=prov["client_id"],
            client_secret=prov.get("client_secret", ""),
        )

    async def _fetch_metadata(self, prov: dict) -> dict:
        """Fetch and cache the OIDC discovery document."""
        name = prov["name"]
        now = time.time()

        if name in self._metadata_cache and now < self._metadata_expiry.get(name, 0):
            return self._metadata_cache[name]

        issuer = prov["issuer"].rstrip("/")
        discovery_url = f"{issuer}/.well-known/openid-configuration"

        async with AsyncOAuth2Client(
            client_id=prov["client_id"],
            client_secret=prov.get("client_secret", ""),
        ) as client:
            resp = await client.get(discovery_url)
            resp.raise_for_status()
            metadata = resp.json()

        self._metadata_cache[name] = metadata
        self._metadata_expiry[name] = now + _METADATA_TTL
        logger.info("Fetched OIDC discovery for '%s' from %s", name, discovery_url)
        return metadata
