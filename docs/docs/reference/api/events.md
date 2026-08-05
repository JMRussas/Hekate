# Events API

Server-Sent Events (SSE) streaming for real-time project progress. Used by the Iris desktop app and the orchestration dashboard.

**Base URL:** `http://localhost:5200/api`

---

## Unified SSE Stream

Stream all project and task events across the entire system. Localhost only -- no authentication required.

```
GET /api/events/stream
```

### Access Control

This endpoint is restricted to localhost connections (`127.0.0.1`, `::1`). Remote clients receive `403 Forbidden`.

### Response

Content-Type: `text/event-stream`

The stream emits events continuously until the client disconnects.

### Event Format

```
event: <event_type>
data: <json_payload>

```

Each event consists of an `event:` line specifying the type, a `data:` line with a JSON payload, and a blank line delimiter.

### Event Types

| Event Type | Description | Key Fields |
|------------|-------------|------------|
| `token` | LLM token streamed during execution | `project_id`, `task_id`, `content` |
| `phase` | Execution phase change | `project_id`, `task_id`, `phase` |
| `tool_call` | Tool invoked during execution | `project_id`, `task_id`, `tool`, `arguments` |
| `tool_result` | Tool returned a result | `project_id`, `task_id`, `tool`, `result` |
| `done` | Task execution finished | `project_id`, `task_id` |
| `status` | Task or project status change | `project_id`, `task_id`, `status`, `message` |
| `output` | Task output produced | `project_id`, `task_id`, `output_text` |
| `error` | Error occurred | `project_id`, `task_id`, `error`, `message` |
| `task_start` | Task execution started | `project_id`, `task_id`, `title` |
| `task_complete` | Task completed | `project_id`, `task_id`, `cost_usd`, `model_used` |
| `task_failed` | Task failed | `project_id`, `task_id`, `error` |
| `wave_checkpoint` | Wave boundary reached | `project_id`, `wave` |
| `project_complete` | All tasks done | `project_id` |

### Example

```bash
curl -N http://localhost:5200/api/events/stream
```

```
event: status
data: {"project_id":"a1b2c3","task_id":"t1a2b3","status":"running","message":"Starting JWT middleware task"}

event: token
data: {"project_id":"a1b2c3","task_id":"t1a2b3","content":"import jwt from"}

event: task_complete
data: {"project_id":"a1b2c3","task_id":"t1a2b3","cost_usd":0.045,"model_used":"claude-code"}

```

### JavaScript Client

```javascript
const source = new EventSource("http://localhost:5200/api/events/stream");

source.addEventListener("task_complete", (event) => {
  const data = JSON.parse(event.data);
  console.log(`Task ${data.task_id} completed ($${data.cost_usd})`);
});

source.addEventListener("error", () => {
  console.log("SSE connection lost, reconnecting...");
});
```

---

## Issue SSE Token

Issue a short-lived SSE token scoped to a single project. Used by remote clients that cannot access the unified localhost stream.

```
POST /api/events/{project_id}/token
```

**Auth required:** JWT Bearer token.

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Project ID to scope the token to |

### Response

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

### Token Lifecycle

| Property | Value |
|----------|-------|
| Type | JWT (HS256) |
| Lifetime | 60 seconds |
| Scope | Single project |
| Token type claim | `sse` |

The token is single-use -- generate a new one for each SSE connection.

### Example

```bash
SSE_TOKEN=$(curl -s -X POST http://localhost:5200/api/events/a1b2c3d4e5f6/token \
  -H "Authorization: Bearer $TOKEN" | jq -r '.token')
```

---

## Project SSE Stream

Stream events for a single project. Requires an SSE token from the token endpoint (remote clients) or localhost access (development bypass).

```
GET /api/events/{project_id}
```

### Path Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `project_id` | string | Project ID |

### Query Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `token` | string | Yes (remote) | SSE token from the token endpoint. Not required for localhost. |

### Error Responses

| Status | Condition |
|--------|-----------|
| 401 | No SSE token provided (remote client) |
| 403 | Token not valid for this project |

### Example

```bash
# Remote client (with token)
curl -N "http://host:5200/api/events/a1b2c3d4e5f6?token=$SSE_TOKEN"

# Localhost (no token needed)
curl -N http://localhost:5200/api/events/a1b2c3d4e5f6
```

---

## Reconnection Handling

SSE connections may drop due to network issues or server restarts. The standard `EventSource` API handles reconnection automatically with exponential backoff.

For manual reconnection:

1. Detect the connection drop (EventSource `error` event)
2. Wait 1-5 seconds
3. For project streams, issue a new SSE token (the old one has likely expired)
4. Reconnect to the same URL

The server sets `Cache-Control: no-cache` and `X-Accel-Buffering: no` headers to prevent proxy buffering.

### Response Headers

| Header | Value | Purpose |
|--------|-------|---------|
| `Cache-Control` | `no-cache` | Prevent caching |
| `Connection` | `keep-alive` | Persistent connection |
| `X-Accel-Buffering` | `no` | Disable nginx buffering |
| `X-Content-Type-Options` | `nosniff` | Security |
| `Referrer-Policy` | `no-referrer` | Security |
