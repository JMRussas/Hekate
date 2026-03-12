#  Orchestration Engine - Auth Routes
#
#  Registration, login, token refresh, user profile, and API key management.
#
#  Depends on: container.py, services/auth.py, middleware/auth.py
#  Used by:    app.py

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends, HTTPException
from starlette.requests import Request

from backend.container import Container
from backend.rate_limit import limiter
from backend.middleware.auth import get_current_user
from backend.models.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    LoginRequest,
    LoginResponse,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    UserOut,
)
from backend.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201)
@limiter.limit("5/minute")
@inject
async def register(
    request: Request,
    body: RegisterRequest,
    auth: AuthService = Depends(Provide[Container.auth]),
) -> UserOut:
    """Register a new user. First user becomes admin."""
    try:
        user = await auth.register(body.email, body.password, body.display_name)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return UserOut(**user)


@router.post("/login")
@limiter.limit("5/minute")
@inject
async def login(
    request: Request,
    body: LoginRequest,
    auth: AuthService = Depends(Provide[Container.auth]),
) -> LoginResponse:
    """Authenticate and receive access + refresh tokens."""
    try:
        result = await auth.login(body.email, body.password)
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=401, detail=str(e))
    return LoginResponse(**result)


@router.post("/refresh")
@limiter.limit("10/minute")
@inject
async def refresh(
    request: Request,
    body: RefreshRequest,
    auth: AuthService = Depends(Provide[Container.auth]),
) -> RefreshResponse:
    """Exchange a refresh token for new access + refresh tokens."""
    try:
        result = await auth.refresh_tokens(body.refresh_token)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return RefreshResponse(**result)


@router.get("/me")
async def get_me(
    user: dict = Depends(get_current_user),
) -> UserOut:
    """Get the current authenticated user's profile."""
    return UserOut(**user)


# ------------------------------------------------------------------
# API Keys
# ------------------------------------------------------------------

@router.post("/api-keys", status_code=201)
@inject
async def create_api_key(
    body: ApiKeyCreate,
    user: dict = Depends(get_current_user),
    auth: AuthService = Depends(Provide[Container.auth]),
) -> ApiKeyCreated:
    """Create a new API key for MCP/external executor authentication.

    The full key is returned only once — store it securely.
    """
    result = await auth.create_api_key(user["id"], body.name)
    return ApiKeyCreated(**result)


@router.get("/api-keys")
@inject
async def list_api_keys(
    user: dict = Depends(get_current_user),
    auth: AuthService = Depends(Provide[Container.auth]),
) -> list[ApiKeyOut]:
    """List all API keys for the current user."""
    keys = await auth.list_api_keys(user["id"])
    return [ApiKeyOut(**k) for k in keys]


@router.delete("/api-keys/{key_id}")
@inject
async def revoke_api_key(
    key_id: str,
    user: dict = Depends(get_current_user),
    auth: AuthService = Depends(Provide[Container.auth]),
):
    """Revoke an API key. Cannot be undone."""
    revoked = await auth.revoke_api_key(key_id, user["id"])
    if not revoked:
        raise HTTPException(status_code=404, detail="API key not found")
    return {"status": "revoked", "key_id": key_id}
