# Auth API

Authentication and authorization endpoints -- registration, JWT login, token refresh, API key management, and OIDC provider integration.

**Base URL:** `http://localhost:5200/api`

---

## Registration

Create a new user account. The first user registered automatically becomes an admin.

```
POST /api/auth/register
```

**Rate limit:** 5 requests per minute.

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `email` | string | Yes | Valid email address |
| `password` | string | Yes | Password |
| `display_name` | string | No | Display name |

### Response (`UserOut`)

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | User ID |
| `email` | string | Email address |
| `display_name` | string | Display name |
| `role` | string | `admin` or `user` |
| `created_at` | float | Unix timestamp |

### Error Responses

| Status | Condition |
|--------|-----------|
| 400 | Email already registered or invalid input |
| 403 | Registration disabled (if configured) |

### Example

```bash
curl -X POST http://localhost:5200/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "dev@example.com",
    "password": "secure-password",
    "display_name": "Developer"
  }'
```

```json
{
  "id": "u1a2b3c4d5e6",
  "email": "dev@example.com",
  "display_name": "Developer",
  "role": "admin",
  "created_at": 1711900000.0
}
```

---

## Login

Authenticate and receive access + refresh tokens.

```
POST /api/auth/login
```

**Rate limit:** 5 requests per minute.

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `email` | string | Yes | Email address |
| `password` | string | Yes | Password |

### Response (`LoginResponse`)

| Field | Type | Description |
|-------|------|-------------|
| `access_token` | string | JWT access token (HS256) |
| `refresh_token` | string | JWT refresh token |
| `token_type` | string | Always `bearer` |
| `user` | object | `UserOut` object |

### Error Responses

| Status | Condition |
|--------|-----------|
| 401 | Invalid credentials or account locked |

### Login Lockout

After 5 consecutive failed login attempts within a 300-second window, the account is temporarily locked. The lockout resets after the window expires or after a successful login.

### Example

```bash
curl -X POST http://localhost:5200/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "dev@example.com", "password": "secure-password"}'
```

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "user": {
    "id": "u1a2b3c4d5e6",
    "email": "dev@example.com",
    "display_name": "Developer",
    "role": "admin"
  }
}
```

---

## Refresh Token

Exchange a refresh token for new access + refresh tokens.

```
POST /api/auth/refresh
```

**Rate limit:** 10 requests per minute.

### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `refresh_token` | string | Yes | Current refresh token |

### Response (`RefreshResponse`)

| Field | Type | Description |
|-------|------|-------------|
| `access_token` | string | New JWT access token |
| `refresh_token` | string | New refresh token (old one is invalidated) |
| `token_type` | string | `bearer` |

### Example

```bash
curl -X POST http://localhost:5200/api/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "eyJhbGci..."}'
```

---

## Current User

Get the authenticated user's profile.

```
GET /api/auth/me
```

### Response

Returns a `UserOut` object.

### Example

```bash
curl http://localhost:5200/api/auth/me \
  -H "Authorization: Bearer $TOKEN"
```

---

## API Keys

API keys provide long-lived authentication for MCP servers and external executors. Keys are prefixed with `hk_` for easy identification.

### Create API Key

```
POST /api/auth/api-keys
```

**The full key is returned only once -- store it securely.**

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Descriptive name for the key |

#### Response (`ApiKeyCreated`)

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Key ID (for management) |
| `name` | string | Key name |
| `key` | string | Full API key (only shown once) |
| `created_at` | float | Unix timestamp |

#### Example

```bash
curl -X POST http://localhost:5200/api/auth/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "MCP Server Key"}'
```

```json
{
  "id": "k1a2b3c4d5e6",
  "name": "MCP Server Key",
  "key": "hk_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6",
  "created_at": 1711900000.0
}
```

### List API Keys

```
GET /api/auth/api-keys
```

Returns an array of `ApiKeyOut` objects (key value is masked).

#### Example

```bash
curl http://localhost:5200/api/auth/api-keys \
  -H "Authorization: Bearer $TOKEN"
```

### Revoke API Key

```
DELETE /api/auth/api-keys/{key_id}
```

Permanently revokes an API key. Cannot be undone.

#### Example

```bash
curl -X DELETE http://localhost:5200/api/auth/api-keys/k1a2b3c4d5e6 \
  -H "Authorization: Bearer $TOKEN"
```

---

## OIDC Endpoints

OAuth/OpenID Connect integration for external identity providers (Google, GitHub, etc.).

### List Providers

List configured OIDC providers. Public endpoint -- no authentication required.

```
GET /api/auth/oidc/providers
```

#### Response

```json
[
  {
    "name": "google",
    "display_name": "Google",
    "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth"
  }
]
```

### Start Login

Begin the OIDC login flow. Returns the authorization URL and a state token for CSRF protection.

```
GET /api/auth/oidc/{provider}/login
```

**Rate limit:** 5 requests per minute.

#### Query Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `redirect_uri` | string | Yes | URL to redirect back to after authentication |

#### Response

```json
{
  "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?...",
  "state_token": "eyJhbGci..."
}
```

The `state_token` is a JWT (5-minute TTL) containing the OAuth state and nonce. Pass it back in the callback.

### Handle Callback

Exchange the authorization code for JWT tokens.

```
POST /api/auth/oidc/{provider}/callback
```

**Rate limit:** 5 requests per minute.

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | Yes | Authorization code from the provider |
| `state` | string | Yes | OAuth state parameter |
| `state_token` | string | Yes | State token from the login step |
| `redirect_uri` | string | Yes | Same redirect_uri used in the login step |

#### Response

Returns a `LoginResponse` (same as the login endpoint).

### Link Provider

Link an OIDC provider to the current user's account. Requires authentication.

```
POST /api/auth/oidc/link/{provider}
```

#### Request Body

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `code` | string | Yes | Authorization code |
| `state` | string | Yes | OAuth state |
| `state_token` | string | Yes | State token |
| `redirect_uri` | string | Yes | Redirect URI |

### Unlink Provider

Remove an OIDC identity from the current user's account.

```
DELETE /api/auth/oidc/link/{provider}
```

Returns `204 No Content`.

### List Identities

List all linked OIDC identities for the current user.

```
GET /api/auth/oidc/identities
```

---

## Auth Configuration

| Setting | Value | Description |
|---------|-------|-------------|
| Algorithm | HS256 | JWT signing algorithm |
| Access token TTL | 30 minutes | `access_token_expire_minutes` |
| Refresh token TTL | 7 days | `refresh_token_expire_days` |
| SSE token TTL | 60 seconds | Short-lived, project-scoped |
| OIDC state token TTL | 5 minutes | For slow auth flows |
| Login lockout threshold | 5 attempts | Consecutive failures |
| Login lockout window | 300 seconds | Time window for counting failures |

## Using Auth in Requests

All authenticated endpoints accept either:

1. **JWT Bearer token** in the `Authorization` header:
   ```
   Authorization: Bearer eyJhbGci...
   ```

2. **API key** in the `Authorization` header:
   ```
   Authorization: Bearer hk_a1b2c3d4...
   ```

Both are accepted by the auth middleware. API keys are resolved to the user who created them.
