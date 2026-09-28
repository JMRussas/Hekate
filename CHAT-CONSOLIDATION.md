# Shared chat runtime consolidation

## Current handoff — September 28, 2026

Read [ChatAgent integration handoff](CHATAGENT-INTEGRATION-HANDOFF.md) before
overlapping implementation. It pins the implemented source checkpoint, specifies
the task contract, maps reusable code to Hekate, and records migration decisions
and acceptance tests. ChatRuntime now has background summaries, protocol v1,
durable documentation tasks and an opt-in conversation/task bridge. These facts
supersede the older next-slice instructions below. Hekate context-store integration
remains a proposal, not an implemented adapter.

## Historical planning checkpoint — September 25, 2026

The following preserves the original source inspection and proposed ownership.
Its statements about adapters and next steps are historical, not deployment status.

2026-09-25 — planning checkpoint, not an implemented migration.

**Runtime ownership ADR recorded:** ChatAgent repo's
`docs/adr/0001-chat-runtime-ownership.md`. Decision: ChatRuntime (the ChatAgent
repo) keeps owning chat request-handling/context logic; Hekate's context-store
is reused as ChatRuntime's durable persistence backend via a new adapter,
rather than porting context logic into this repo's Python codebase. Two
findings from tracing this repo's actual code (not assumptions) drove that
call: `chat_agent.py` builds the model's message history from client-supplied
messages only — `/api/brain/assemble`'s output only feeds the system prompt,
not turn history — and `/api/chat/stream` plus `/api/brain/*` have **no
auth/project-isolation boundary at all** today (explicit "no auth for now"
comment in `orchestration/backend/app.py`; conversation scope is a bare,
guessable GUID). Also confirmed while tracing: the gods pipeline is not part of
the chat path in any way, and `Odin/gods/providers/` (this repo's CLI provider
abstraction) is actually wired into `hermes_async.py` — this repo's own
CLAUDE.md's "built, not wired" annotation is stale.
Full evidence and the protocol v1 proposal are in the ADR; nothing in this
repo was changed to produce it.

The user wants Hekate, Iris, and ChatAgent to share the chat runtime: conversation
history/context, model selection, provider execution, and streaming lifecycle.
Keep application-specific workflows and presentation in each application.
Use a versioned service protocol across Python, C# and TypeScript.

## Hekate assets to assess

- `orchestration/backend/routes/chat.py` and `services/chat_agent.py`: streaming
  chat with context-store resolve/assemble and turn persistence calls.
- `orchestration/backend/services/model_router.py`, `model_discovery.py`,
  `provider_quota.py`, `cli_provider.py`: routing and provider/CLI candidates.
- `context-store/Api/Program.cs`: conversation and brain APIs.
- `context-store/ContextRouter/ContextAssembler.cs` and `EmbeddingService.cs`:
  context/retrieval candidates.
- `llm-gateway` and `Odin/gods`: trace the active execution paths before choosing
  an owner. CLAUDE.md marks parts of orchestration as legacy; do not build the
  shared runtime around a retired path merely because its abstractions exist.

## Required decisions and verification

Hekate is an integration candidate, not yet the agreed runtime host. Record an ADR
for runtime/persistence ownership, standalone use, auth/project isolation, and
service-unavailable behavior. Avoid parallel provider/catalog implementations.

Verify continuity: the inspected chat service builds prompt history from client
messages while Iris's SSE handler does not persist the returned conversation ID.
Existing chat truncation drops individual messages using a character estimate;
adopt exchange preservation, output reservation and immutable queued context.
Keep summaries internal with retained source records and attribution. Evaluate
existing graph/vector retrieval against recall cases rather than duplicating it.

Protocol v1 needs conversation/turn/task/attempt identity, ordered event identity,
answer revisions, deltas, activity, model/usage metadata, and explicit terminal
states. Specify idempotency, reconnect and cancellation. Subscription adapters
must preserve permission boundaries and distinguish unknown cost from free use.

## Next slice and acceptance

Preserve ChatAgent's completed 01A context work. Reconcile its 01B and later specs
before overlapping implementation. First integrate one Iris turn with fast/deep
responses through an adapter, with a rollback switch; keep old clients working.
Test two-turn continuity, isolation, retries, reconnect, cancellation, partial
failure, late deep updates, tool pairs, budgets and source lookup after compaction.
Only retire old paths after parity and separately recorded live checks.

Detailed handoff: ChatAgent `docs/implementation/07-shared-chat-runtime.md`.
Iris companion: root `CHAT-CONSOLIDATION.md`. Repository naming may change; the
contract and owner ADR, not a folder name, define the architecture.

This note records source inspection only. No deployment, migration or live
verification was performed. Preserve unrelated in-progress changes.
