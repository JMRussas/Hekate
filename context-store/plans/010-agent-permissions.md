# Plan 010 — Agent Permissions Model

## Summary

Add a permission system that controls what agents (models) can do within conversations. Inspired by Claude Code's permission model (bypass, ask-before-edit, auto-edit, plan-mode), adapted for the ideation assistant's node-based context store.

## Problem

Right now every model can call every skill without restriction. There's no concept of:
- Read-only exploration vs. destructive mutation
- User approval before an agent creates/modifies nodes
- Per-model or per-conversation permission scoping
- Escalation (model asks for permission, user grants/denies)

## Permission Levels

| Level | Label | Read Nodes | Create Nodes | Mutate Nodes | Execute Skills | Delete Nodes |
|-------|-------|-----------|-------------|-------------|---------------|-------------|
| 0 | **Observe** | yes | no | no | read-only skills | no |
| 1 | **Suggest** | yes | propose (pending approval) | no | read-only skills | no |
| 2 | **Assist** | yes | yes | propose (pending approval) | all skills | no |
| 3 | **Auto** | yes | yes | yes | all skills | propose |

### Skill Classification

Each skill gets a `permissionLevel` in skills.json:

| Skill | Level Required | Why |
|-------|---------------|-----|
| `search_ideas` | 0 (Observe) | Read-only query |
| `get_node_details` | 0 (Observe) | Read-only query |
| `list_threads` | 0 (Observe) | Read-only query |
| `route_to_model` | 1 (Suggest) | Creates turn nodes in another model's context |
| `create_node` | 2 (Assist) | Creates new nodes |
| `update_node` | 2 (Assist) | Modifies existing nodes |
| `delete_node` | 3 (Auto) | Destructive |
| `execute_code` | 3 (Auto) | Side effects outside the DB |

### Where Permissions Live

**Conversation-level default** — stored as an attribute on the `conversation` node:
```
conversation.attributes["permission_level"] = "2"  // Assist
```

**Per-model override** — stored as attributes on the conversation with model-scoped keys:
```
conversation.attributes["permission:haiku"] = "1"   // Haiku limited to Suggest
conversation.attributes["permission:opus"] = "3"    // Opus gets Auto
```

Resolution: per-model override > conversation default > system default (2 = Assist).

## Approval Flow (Suggest/Assist pending actions)

When a model at level 1-2 tries an action above its permission:

1. **ChatService** checks permission before executing the skill
2. If denied: creates a `pending_action` node under the conversation thread
   - `node_type`: `pending_action`
   - `name`: e.g., "Create idea: Weather Dashboard caching"
   - `attributes`: `{ action: "create_node", skill: "create_node", params: "{...}", model: "haiku", status: "pending" }`
3. **SSE event** `permission_request` sent to frontend with the pending action details
4. **Frontend** shows an inline approval card in the chat stream:
   - Action description, which model requested it, what it wants to do
   - Approve / Deny buttons
   - Optional "Always allow this" checkbox (upgrades the model's permission for this conversation)
5. **User response** → `POST /api/action/{id}/approve` or `/deny`
6. If approved: ChatService executes the skill, stores result, streams back to chat
7. If denied: model gets a tool result saying "Permission denied by user"

## UI Changes

### Header — Permission Indicator

Small badge in the header showing current permission level for the active conversation:
```
[Observe] [Suggest] [Assist] [Auto]
```
Clickable to change. Only shown in chat view.

### Chat — Approval Cards

Inline card when a model requests permission:
```
┌─────────────────────────────────────────────┐
│ 🔒 haiku wants to: Create idea node         │
│    "Weather Dashboard caching strategy"      │
│                                              │
│    [Approve]  [Deny]  □ Always allow haiku   │
└─────────────────────────────────────────────┘
```

### Context (Stats) Panel

Show current permission config:
- Conversation default level
- Per-model overrides (if any)
- Pending action count

## Implementation Phases

### Phase 1 — Backend Permission Check (3 files)

1. Add `permissionLevel` field to skill definitions in `skills.json`
2. Add `PermissionChecker` service:
   - `CheckPermission(conversationId, model, skill) → Allowed | NeedsApproval | Denied`
   - Reads conversation attributes for permission config
   - Compares skill's required level against model's effective level
3. Wire into ChatService tool-use loop: before executing a skill, check permission

### Phase 2 — Pending Actions (4 files)

1. Add `pending_action` node type to `NodeTypes.cs`
2. Add `PendingActionService`:
   - `CreatePendingAction(conversationId, threadId, model, skill, params) → actionId`
   - `ApproveAction(actionId) → executes the skill, returns result`
   - `DenyAction(actionId) → marks denied`
   - `ListPending(conversationId) → pending actions`
3. Add API endpoints: `POST /api/action/{id}/approve`, `POST /api/action/{id}/deny`, `GET /api/actions/{conversationId}`
4. Add SSE event `permission_request` in ChatService when action is pended

### Phase 3 — Frontend (3 files)

1. Add permission level selector to header (or stats panel)
2. Add approval card component rendered inline in ChatPanel when `permission_request` SSE event arrives
3. Add permission display to StatsBar

### Phase 4 — Per-Model Config (2 files)

1. Add UI for per-model permission overrides (conversation settings dropdown or modal)
2. Store as conversation attributes via existing `updateAttributes` API

## Node Types Added

| Type | Purpose |
|------|---------|
| `pending_action` | Queued action awaiting user approval |

## SSE Events Added

| Event | Payload | When |
|-------|---------|------|
| `permission_request` | `{ actionId, model, skill, description, params }` | Model tries action above its permission level |
| `action_resolved` | `{ actionId, approved: bool, result? }` | User approves or denies |

## Risks

- **Latency**: Approval flow blocks the model's tool-use loop. The model needs to either wait or move on and come back.
- **Stale closures**: If the model's response stream is waiting for approval, the SSE connection must stay open. Current concurrent streaming handles this naturally.
- **Permission escalation**: "Always allow" effectively upgrades a model permanently for that conversation. Need clear UX for revoking.

## Non-Goals (this plan)

- Cross-conversation permission policies (global model restrictions)
- Rate limiting / quotas per model
- Audit log of all permission checks (the pending_action nodes serve as partial audit trail)
- Permission inheritance from projects

## Dependencies

- `skills.json` skill definitions (exists)
- Conversation attributes (exists)
- ChatService tool-use loop (exists)
- SSE streaming infrastructure (exists)
