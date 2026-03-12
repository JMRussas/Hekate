#  Orchestration Engine - Auth Middleware
#
#  FastAPI dependencies for JWT authentication.
#  get_current_user: validates Bearer token, returns user dict.
#  require_admin: wraps get_current_user + role check.
#  get_user_from_sse_token: validates short-lived SSE query-param token.
#
#  Depends on: backend/services/auth.py, backend/container.py
#  Used by:    app.py, routes/*

import logging

import jwt
from dependency_injector.wiring import inject, Provide
from fastapi import Depends, HTTPException, Path, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.container import Container
from backend.services.auth import AuthService

logger = logging.getLogger("orchestration.auth")

_bearer_scheme = HTTPBearer(auto_error=False)


async def _validate_token(auth: AuthService, raw_token: str, expected_type: str = "access") -> tuple[dict, dict]:
    """Decode a JWT, check its type, and return the active user.

    Shared logic for Bearer header auth and SSE query-param auth.
    Raises HTTPException on any failure.
    """
    try:
        payload = auth.decode_token(raw_token)
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )

    user = await auth.get_user(payload["sub"])
    if not user or not user["is_active"]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or disabled",
        )

    return user, payload


@inject
async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    auth: AuthService = Depends(Provide[Container.auth]),
) -> dict:
    """Validate Bearer token and return user dict. Raises 401 on failure.

    Supports both JWT access tokens and API keys (prefixed with 'orch_').
    API keys are validated against the api_keys table instead of JWT decode.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw = credentials.credentials

    # API key path: keys start with 'orch_'
    if raw.startswith("orch_"):
        user = await auth.validate_api_key(raw)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return user

    # JWT path
    user, _payload = await _validate_token(auth, raw, "access")
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Require the current user to have admin role."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


@inject
async def get_user_from_sse_token(
    project_id: str = Path(...),
    token: str = Query(...),
    auth: AuthService = Depends(Provide[Container.auth]),
) -> dict:
    """Validate a short-lived SSE token scoped to a single project.

    Decodes the token once via _validate_token (type="sse"),
    then verifies the project_id claim matches the route parameter.
    """
    user, payload = await _validate_token(auth, token, "sse")

    if payload.get("project_id") != project_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSE token not valid for this project",
        )

    return user
