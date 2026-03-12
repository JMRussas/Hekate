#  Orchestration Engine - OIDC Auth Routes
#
#  OAuth/OIDC login, callback, provider listing, link/unlink.
#
#  Depends on: container.py, services/oidc.py, middleware/auth.py
#  Used by:    app.py

import logging
from datetime import datetime, timedelta, timezone

import jwt
from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.requests import Request

from backend.config import AUTH_ALGORITHM, AUTH_SECRET_KEY
from backend.container import Container
from backend.rate_limit import limiter
from backend.exceptions import AccountLinkError, NotFoundError, OIDCError
from backend.middleware.auth import get_current_user
from backend.models.schemas import (
    LoginResponse,
    OIDCCallbackRequest,
    OIDCIdentityOut,
    OIDCLinkRequest,
    OIDCProviderInfo,
)
from backend.services.oidc import OIDCService

logger = logging.getLogger("orchestration.routes.oidc")

router = APIRouter(prefix="/auth/oidc", tags=["auth-oidc"])

_STATE_TOKEN_TTL_SECONDS = 300  # 5 minutes for slow auth flows


# ------------------------------------------------------------------
# State token helpers (stateless CSRF protection via JWT)
# ------------------------------------------------------------------

def _create_state_token(state: str, nonce: str, provider: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(seconds=_STATE_TOKEN_TTL_SECONDS)
    payload = {
        "type": "oidc_state",
        "state": state,
        "nonce": nonce,
        "provider": provider,
        "exp": expire,
    }
    return jwt.encode(payload, AUTH_SECRET_KEY, algorithm=AUTH_ALGORITHM)


def _validate_state_token(
    state_token: str, expected_state: str, expected_provider: str
) -> tuple[str, str]:
    """Validate the OIDC state JWT. Returns (state, nonce)."""
    try:
        payload = jwt.decode(state_token, AUTH_SECRET_KEY, algorithms=[AUTH_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(400, "Invalid or expired state token")

    if payload.get("type") != "oidc_state":
        raise HTTPException(400, "Invalid state token type")
    if payload.get("state") != expected_state:
        raise HTTPException(400, "State mismatch — possible CSRF")
    if payload.get("provider") != expected_provider:
        raise HTTPException(400, "Provider mismatch in state token")

    return payload["state"], payload["nonce"]


# ------------------------------------------------------------------
# Public endpoints
# ------------------------------------------------------------------

@router.get("/providers", response_model=list[OIDCProviderInfo])
@inject
async def list_providers(
    oidc: OIDCService = Depends(Provide[Container.oidc]),
):
    """Return configured OIDC providers (public — no auth required)."""
    return oidc.get_available_providers()


@router.get("/{provider}/login")
@limiter.limit("5/minute")
@inject
async def oidc_login_redirect(
    request: Request,
    provider: str,
    redirect_uri: str = Query(...),
    oidc: OIDCService = Depends(Provide[Container.oidc]),
):
    """Start OIDC login: returns authorization URL and state token."""
    try:
        url, state, nonce = await oidc.get_authorization_url(provider, redirect_uri)
    except NotFoundError:
        raise HTTPException(404, f"OIDC provider '{provider}' is not configured")
    except OIDCError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error("OIDC login redirect failed for '%s': %s", provider, e)
        raise HTTPException(502, "Failed to connect to OIDC provider")

    state_token = _create_state_token(state, nonce, provider)
    return {"authorization_url": url, "state_token": state_token}


@router.post("/{provider}/callback", response_model=LoginResponse)
@limiter.limit("5/minute")
@inject
async def oidc_callback(
    request: Request,
    provider: str,
    body: OIDCCallbackRequest,
    oidc: OIDCService = Depends(Provide[Container.oidc]),
):
    """Handle OIDC callback: exchange code for JWT tokens."""
    _state, nonce = _validate_state_token(body.state_token, body.state, provider)

    try:
        result = await oidc.oidc_login(
            provider, body.code, body.redirect_uri, nonce
        )
    except NotFoundError:
        raise HTTPException(404, f"OIDC provider '{provider}' is not configured")
    except OIDCError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error("OIDC callback failed for '%s': %s", provider, e)
        raise HTTPException(502, "OIDC authentication failed")

    return LoginResponse(**result)


# ------------------------------------------------------------------
# Authenticated endpoints (link/unlink/list identities)
# ------------------------------------------------------------------

@router.post("/link/{provider}")
@inject
async def link_provider(
    provider: str,
    body: OIDCLinkRequest,
    user: dict = Depends(get_current_user),
    oidc: OIDCService = Depends(Provide[Container.oidc]),
):
    """Link an OIDC provider to the current user's account."""
    _state, nonce = _validate_state_token(body.state_token, body.state, provider)

    try:
        result = await oidc.link_provider(
            user["id"], provider, body.code, body.redirect_uri, nonce
        )
    except NotFoundError:
        raise HTTPException(404, f"OIDC provider '{provider}' is not configured")
    except AccountLinkError as e:
        raise HTTPException(400, str(e))
    except OIDCError as e:
        raise HTTPException(400, str(e))

    return result


@router.delete("/link/{provider}", status_code=204)
@inject
async def unlink_provider(
    provider: str,
    user: dict = Depends(get_current_user),
    oidc: OIDCService = Depends(Provide[Container.oidc]),
):
    """Unlink an OIDC provider from the current user's account."""
    try:
        await oidc.unlink_provider(user["id"], provider)
    except NotFoundError as e:
        raise HTTPException(404, str(e))
    except AccountLinkError as e:
        raise HTTPException(400, str(e))


@router.get("/identities", response_model=list[OIDCIdentityOut])
@inject
async def list_identities(
    user: dict = Depends(get_current_user),
    oidc: OIDCService = Depends(Provide[Container.oidc]),
):
    """List all linked OIDC identities for the current user."""
    return await oidc.get_user_identities(user["id"])
