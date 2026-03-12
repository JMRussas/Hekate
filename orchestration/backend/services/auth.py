#  Orchestration Engine - Auth Service
#
#  Password hashing, JWT encode/decode, register/login, SSE tokens.
#  First registered user becomes admin. Includes per-account brute-force
#  login protection with configurable threshold and window.
#
#  Depends on: backend/db/connection.py, backend/config.py
#  Used by:    container.py, routes/auth.py, routes/events.py, middleware/auth.py

import hashlib
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from backend.config import (
    AUTH_ACCESS_TOKEN_EXPIRE_MINUTES,
    AUTH_ALGORITHM,
    AUTH_ALLOW_REGISTRATION,
    AUTH_LOGIN_LOCKOUT_THRESHOLD,
    AUTH_LOGIN_LOCKOUT_WINDOW_SEC,
    AUTH_LOGIN_MAX_TRACKED,
    AUTH_REFRESH_TOKEN_EXPIRE_DAYS,
    AUTH_SECRET_KEY,
    AUTH_SSE_TOKEN_EXPIRE_SECONDS,
)
from backend.db.connection import Database

logger = logging.getLogger("orchestration.auth")

# Pre-computed dummy hash for timing-safe login (prevents timing side-channel)
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt()).decode()


class AuthService:
    """Handles user registration, login, and JWT token management."""

    def __init__(self, db: Database):
        self._db = db
        # Brute-force protection: {email: (fail_count, first_fail_timestamp)}
        self._login_failures: dict[str, tuple[int, float]] = {}

    # ------------------------------------------------------------------
    # Password helpers
    # ------------------------------------------------------------------

    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(plain: str, hashed: str) -> bool:
        return bcrypt.checkpw(plain.encode(), hashed.encode())

    # ------------------------------------------------------------------
    # JWT helpers
    # ------------------------------------------------------------------

    @staticmethod
    def create_access_token(user_id: str, role: str) -> str:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=AUTH_ACCESS_TOKEN_EXPIRE_MINUTES
        )
        payload = {
            "sub": user_id,
            "role": role,
            "type": "access",
            "exp": expire,
        }
        return jwt.encode(payload, AUTH_SECRET_KEY, algorithm=AUTH_ALGORITHM)

    @staticmethod
    def create_refresh_token(user_id: str, family_id: str | None = None) -> tuple[str, str]:
        """Create a refresh token with family tracking.

        Returns (token, family_id). The family_id groups related refresh tokens
        so an entire lineage can be revoked if reuse is detected.
        """
        fid = family_id or uuid.uuid4().hex
        expire = datetime.now(timezone.utc) + timedelta(
            days=AUTH_REFRESH_TOKEN_EXPIRE_DAYS
        )
        payload = {
            "sub": user_id,
            "type": "refresh",
            "fid": fid,
            "jti": uuid.uuid4().hex,
            "exp": expire,
        }
        token = jwt.encode(payload, AUTH_SECRET_KEY, algorithm=AUTH_ALGORITHM)
        return token, fid

    @staticmethod
    def create_sse_token(user_id: str, project_id: str) -> str:
        """Create a short-lived token scoped to a single project's SSE stream."""
        expire = datetime.now(timezone.utc) + timedelta(
            seconds=AUTH_SSE_TOKEN_EXPIRE_SECONDS
        )
        payload = {
            "sub": user_id,
            "type": "sse",
            "project_id": project_id,
            "exp": expire,
        }
        return jwt.encode(payload, AUTH_SECRET_KEY, algorithm=AUTH_ALGORITHM)

    @staticmethod
    def decode_token(token: str) -> dict:
        """Decode and validate a JWT. Raises jwt.PyJWTError on failure."""
        return jwt.decode(token, AUTH_SECRET_KEY, algorithms=[AUTH_ALGORITHM])

    @staticmethod
    def _hash_token(token: str) -> str:
        """SHA-256 hash of a token for DB storage (never store raw tokens)."""
        return hashlib.sha256(token.encode()).hexdigest()

    async def _store_refresh_token(
        self, user_id: str, family_id: str, token: str, expires_at: float
    ) -> None:
        """Store a refresh token record for family tracking."""
        token_hash = self._hash_token(token)
        await self._db.execute_write(
            "INSERT INTO refresh_token_families "
            "(id, user_id, family_id, token_hash, is_revoked, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
            (uuid.uuid4().hex, user_id, family_id, token_hash, time.time(), expires_at),
        )

    async def _revoke_family(self, family_id: str) -> None:
        """Revoke all tokens in a family (reuse detected)."""
        await self._db.execute_write(
            "UPDATE refresh_token_families SET is_revoked = 1 WHERE family_id = ?",
            (family_id,),
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register(self, email: str, password: str | None = None, display_name: str = "") -> dict:
        """Register a new user. First user becomes admin.

        Uses BEGIN IMMEDIATE transaction to prevent race conditions where
        two concurrent first-registrations both become admin.

        Password can be None for OAuth-only users.
        """
        if not AUTH_ALLOW_REGISTRATION:
            raise PermissionError("Registration is disabled")

        user_id = str(uuid.uuid4())
        now = time.time()
        password_hash = self.hash_password(password) if password else None
        display = display_name or email.split("@")[0]

        async with self._db.transaction() as conn:
            # Check duplicate — generic error to prevent email enumeration
            existing = await conn.execute(
                "SELECT id FROM users WHERE email = ?", (email,)
            )
            if await existing.fetchone():
                raise ValueError("Registration failed")

            # First user becomes admin (safe: BEGIN IMMEDIATE serializes this)
            count_cursor = await conn.execute("SELECT COUNT(*) as cnt FROM users")
            user_count = await count_cursor.fetchone()
            role = "admin" if user_count[0] == 0 else "user"

            await conn.execute(
                "INSERT INTO users (id, email, password_hash, display_name, role, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (user_id, email, password_hash, display, role, now),
            )

        logger.info("User registered: %s (role=%s)", email, role)
        return {
            "id": user_id,
            "email": email,
            "display_name": display,
            "role": role,
        }

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def _evict_stale_failures(self) -> None:
        """Remove stale lockout entries to cap memory usage."""
        if len(self._login_failures) <= AUTH_LOGIN_MAX_TRACKED:
            return
        now = time.time()
        cutoff = now - AUTH_LOGIN_LOCKOUT_WINDOW_SEC
        # First pass: remove entries outside the window
        self._login_failures = {
            email: (count, ts)
            for email, (count, ts) in self._login_failures.items()
            if ts > cutoff
        }
        # Hard cap fallback: if still over limit (all entries within window),
        # drop the oldest entries by timestamp
        if len(self._login_failures) > AUTH_LOGIN_MAX_TRACKED:
            sorted_entries = sorted(
                self._login_failures.items(), key=lambda x: x[1][1]
            )
            keep = sorted_entries[len(sorted_entries) - AUTH_LOGIN_MAX_TRACKED:]
            self._login_failures = dict(keep)

    def _is_locked_out(self, email: str) -> bool:
        """Check if an email is currently locked out."""
        entry = self._login_failures.get(email)
        if not entry:
            return False
        fail_count, first_fail_ts = entry
        # Window expired — forget failures
        if time.time() - first_fail_ts > AUTH_LOGIN_LOCKOUT_WINDOW_SEC:
            del self._login_failures[email]
            return False
        return fail_count >= AUTH_LOGIN_LOCKOUT_THRESHOLD

    def _record_failure(self, email: str) -> None:
        """Record a failed login attempt."""
        now = time.time()
        entry = self._login_failures.get(email)
        if entry:
            fail_count, first_fail_ts = entry
            if now - first_fail_ts > AUTH_LOGIN_LOCKOUT_WINDOW_SEC:
                # Window expired, start fresh
                self._login_failures[email] = (1, now)
            else:
                self._login_failures[email] = (fail_count + 1, first_fail_ts)
        else:
            self._login_failures[email] = (1, now)

    async def login(self, email: str, password: str) -> dict:
        """Authenticate user, return tokens."""
        # Evict stale lockout entries if over memory cap
        self._evict_stale_failures()

        # Check lockout BEFORE doing anything else.
        # If locked out, still run bcrypt for timing safety, then return
        # the same error as a normal login failure (no email enumeration).
        if self._is_locked_out(email):
            self.verify_password(password, _DUMMY_HASH)
            raise ValueError("Invalid email or password")

        user = await self._db.fetchone(
            "SELECT * FROM users WHERE email = ?", (email,)
        )

        if not user:
            # Timing-safe: still run bcrypt against dummy hash so response time
            # is indistinguishable from a real user with wrong password
            self.verify_password(password, _DUMMY_HASH)
            self._record_failure(email)
            raise ValueError("Invalid email or password")

        if not user["password_hash"]:
            raise ValueError("This account uses OAuth login. Please sign in with your linked provider.")

        if not self.verify_password(password, user["password_hash"]):
            self._record_failure(email)
            raise ValueError("Invalid email or password")

        if not user["is_active"]:
            raise PermissionError("Account is disabled")

        # Successful login — clear failure counter
        self._login_failures.pop(email, None)

        # Update last login
        await self._db.execute_write(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (time.time(), user["id"]),
        )

        access_token = self.create_access_token(user["id"], user["role"])
        refresh_token, family_id = self.create_refresh_token(user["id"])

        # Store refresh token for family tracking
        expire_ts = time.time() + (AUTH_REFRESH_TOKEN_EXPIRE_DAYS * 86400)
        await self._store_refresh_token(user["id"], family_id, refresh_token, expire_ts)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": {
                "id": user["id"],
                "email": user["email"],
                "display_name": user["display_name"],
                "role": user["role"],
            },
        }

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    async def refresh_tokens(self, refresh_token: str) -> dict:
        """Issue new access + refresh tokens from a valid refresh token.

        Implements token family tracking:
        - Each refresh token belongs to a family (started at login).
        - On refresh, the old token is consumed (marked revoked) and a new one
          is issued in the same family.
        - If a consumed token is reused, the entire family is revoked (theft detection).
        - Legacy tokens (no `fid` claim) are accepted with a new family (graceful migration).
        """
        try:
            payload = self.decode_token(refresh_token)
        except jwt.PyJWTError as e:
            raise ValueError(f"Invalid refresh token: {e}")

        if payload.get("type") != "refresh":
            raise ValueError("Token is not a refresh token")

        user_id = payload["sub"]
        user = await self._db.fetchone(
            "SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)
        )
        if not user:
            raise ValueError("User not found or disabled")

        family_id = payload.get("fid")
        token_hash = self._hash_token(refresh_token)

        if family_id:
            # Atomic read-check-consume inside a transaction to prevent TOCTOU:
            # two concurrent refresh requests for the same token must not both succeed.
            # BEGIN IMMEDIATE serializes writers, so the second request will see
            # is_revoked=1 after the first commits.
            #
            # Revocation on reuse/compromise is done inside the transaction so it
            # commits before we raise. (transaction() rolls back on exception, so
            # we must NOT raise inside the block — instead set an error to raise after.)
            _refresh_error: str | None = None

            async with self._db.transaction():
                record = await self._db.fetchone(
                    "SELECT * FROM refresh_token_families WHERE token_hash = ?",
                    (token_hash,),
                )

                if not record:
                    # Token hash not in DB but has a family ID — reuse of a consumed token.
                    logger.warning(
                        "Refresh token reuse detected for family %s, user %s — revoking family",
                        family_id, user_id,
                    )
                    await self._revoke_family(family_id)
                    _refresh_error = "Token reuse detected — all sessions revoked. Please log in again."
                elif record["is_revoked"]:
                    # Token was already revoked — family compromise.
                    logger.warning(
                        "Revoked refresh token used for family %s, user %s — revoking family",
                        family_id, user_id,
                    )
                    await self._revoke_family(family_id)
                    _refresh_error = "Token has been revoked — all sessions revoked. Please log in again."
                else:
                    # Valid token — consume it (mark as revoked so it can't be reused)
                    await self._db.execute_write(
                        "UPDATE refresh_token_families SET is_revoked = 1 WHERE id = ?",
                        (record["id"],),
                    )

            # Raise AFTER transaction commits, so revocation is persisted
            if _refresh_error:
                raise ValueError(_refresh_error)
        else:
            # Legacy token (no fid) — accept gracefully, start a new family
            family_id = None
            logger.debug("Legacy refresh token for user %s — creating new family", user_id)

        # Issue new tokens in the same family (or a new one for legacy tokens)
        access_token = self.create_access_token(user["id"], user["role"])
        new_refresh, new_family_id = self.create_refresh_token(user["id"], family_id=family_id)

        expire_ts = time.time() + (AUTH_REFRESH_TOKEN_EXPIRE_DAYS * 86400)
        await self._store_refresh_token(user["id"], new_family_id, new_refresh, expire_ts)

        return {
            "access_token": access_token,
            "refresh_token": new_refresh,
            "token_type": "bearer",
        }

    # ------------------------------------------------------------------
    # Get user
    # ------------------------------------------------------------------

    async def get_user(self, user_id: str) -> dict | None:
        """Fetch user by ID. Returns dict or None."""
        row = await self._db.fetchone(
            "SELECT id, email, display_name, role, is_active, created_at, last_login_at, "
            "password_hash IS NOT NULL as has_password "
            "FROM users WHERE id = ?",
            (user_id,),
        )
        if not row:
            return None
        user = dict(row)
        # Fetch linked OIDC providers
        identities = await self._db.fetchall(
            "SELECT provider FROM user_identities WHERE user_id = ?",
            (user_id,),
        )
        user["linked_providers"] = [r["provider"] for r in identities]
        return user

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def revoke_user_tokens(self, user_id: str) -> int:
        """Revoke all refresh token families for a user. Returns count of revoked records."""
        cursor = await self._db.execute_write(
            "UPDATE refresh_token_families SET is_revoked = 1 "
            "WHERE user_id = ? AND is_revoked = 0",
            (user_id,),
        )
        return cursor.rowcount

    async def cleanup_expired_tokens(self) -> int:
        """Delete expired refresh token records. Returns count of deleted records."""
        cursor = await self._db.execute_write(
            "DELETE FROM refresh_token_families WHERE expires_at < ?",
            (time.time(),),
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Password management
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # API key management
    # ------------------------------------------------------------------

    async def create_api_key(self, user_id: str, name: str) -> dict:
        """Create a new API key for MCP/external executor auth.

        Returns dict with id, key (full, shown once), key_prefix, name, created_at.
        The key is stored as a SHA-256 hash — the plaintext is never persisted.
        """
        raw_key = f"orch_{secrets.token_hex(32)}"
        key_prefix = raw_key[:12]
        key_hash = self._hash_token(raw_key)
        key_id = uuid.uuid4().hex
        now = time.time()

        await self._db.execute_write(
            "INSERT INTO api_keys (id, key_hash, key_prefix, user_id, name, is_active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (key_id, key_hash, key_prefix, user_id, name, now),
        )
        logger.info("API key created: %s (%s) for user %s", key_prefix, name, user_id)
        return {
            "id": key_id,
            "key": raw_key,
            "key_prefix": key_prefix,
            "name": name,
            "created_at": now,
        }

    async def validate_api_key(self, raw_key: str) -> dict | None:
        """Validate an API key, return user dict if valid.

        Updates last_used_at on success. Returns None if invalid/inactive.
        """
        key_hash = self._hash_token(raw_key)
        row = await self._db.fetchone(
            "SELECT ak.*, u.id as uid, u.email, u.display_name, u.role, u.is_active as user_active "
            "FROM api_keys ak JOIN users u ON ak.user_id = u.id "
            "WHERE ak.key_hash = ? AND ak.is_active = 1",
            (key_hash,),
        )
        if not row:
            return None
        if not row["user_active"]:
            return None

        # Update last used timestamp
        await self._db.execute_write(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (time.time(), row["id"]),
        )
        return {
            "id": row["uid"],
            "email": row["email"],
            "display_name": row["display_name"],
            "role": row["role"],
        }

    async def list_api_keys(self, user_id: str) -> list[dict]:
        """List all API keys for a user (without hashes)."""
        rows = await self._db.fetchall(
            "SELECT id, key_prefix, name, is_active, created_at, last_used_at "
            "FROM api_keys WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def revoke_api_key(self, key_id: str, user_id: str) -> bool:
        """Revoke an API key. Returns True if the key was found and revoked."""
        cursor = await self._db.execute_write(
            "UPDATE api_keys SET is_active = 0 WHERE id = ? AND user_id = ?",
            (key_id, user_id),
        )
        if cursor.rowcount > 0:
            logger.info("API key %s revoked by user %s", key_id, user_id)
            return True
        return False

    # ------------------------------------------------------------------
    # Password management
    # ------------------------------------------------------------------

    async def set_password(self, user_id: str, new_password: str, caller_id: str | None = None) -> None:
        """Set or change a user's password.

        If caller_id is provided, it must match user_id (users can only change their own password).
        """
        if caller_id is not None and caller_id != user_id:
            raise PermissionError("Cannot change another user's password")
        password_hash = self.hash_password(new_password)
        await self._db.execute_write(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
