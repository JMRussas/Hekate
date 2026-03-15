# Plan 002 — Ideation Nodes & Context Router

**Level:** L2
**Status:** PLAN (awaiting approval)
**Created:** 2026-03-08
**Created by:** claude-opus-4-6
**Depends on:** Plan 001 (schema extension — completed)

## Context

Plan 001 proved that plans can live as nodes in the same PostgreSQL table as code.
Now we need the ideation layer (conversations, ideas, threads) and the context
router that assembles per-model payloads. Voice and text chat must share the same
downstream pipeline — the input transport is the only difference.

### What exists today
- Hekate chat view: in-memory history, passes last 20 messages to every model
- CodeStoragePoc: nodes table with code + plan domains, AGE graph, pgvector
- Three model CLIs on subscription: Claude, Gemini, Codex

### What needs to change
- Conversations persisted as nodes in PostgreSQL (not in-memory)
- Each model gets curated context, not raw history
- Ideas extracted from conversations and tracked through lifecycle
- Voice and text input produce identical node structures

## Thinking

### The Transport-Agnostic Principle

```
VOICE INPUT                    TEXT INPUT
    │                              │
    ▼                              ▼
┌──────────┐                ┌──────────┐
│ STT      │                │ Keyboard │
│ (Gemini  │                │ (Chat UI │
│  Live,   │                │  or CLI) │
│  Whisper)│                │          │
└────┬─────┘                └────┬─────┘
     │ text                      │ text
     └──────────┬────────────────┘
                ▼
    ┌───────────────────────┐
    │   CONVERSATION        │
    │   PIPELINE            │  ← Everything below here is identical
    │                       │     regardless of input transport
    │ 1. Create turn node   │
    │ 2. Route to model(s)  │
    │ 3. Assemble context   │
    │ 4. Get response       │
    │ 5. Create response    │
    │    turn node          │
    │ 6. Extract ideas      │
    │ 7. Update graph       │
    └───────────────────────┘
```

The only metadata that differs is `input_mode: voice|text` on the turn node.
The context router, extraction, idea tracking — all identical.

### Conversation Node Hierarchy

```
conversation "Session 2026-03-08 14:30"
├── turn [user, text] "What if we used event sourcing?"
├── turn [claude, text] "Event sourcing would work well for..."
│   └── (extraction runs here)
│       ├── idea "Event sourcing for orchestration" {status: mentioned}
│       └── question "Which event store?" {status: unresolved}
├── turn [user, voice] "Yeah and maybe we could use Kafka"
├── turn [gemini, voice] "Kafka is one option, but consider..."
│   └── (extraction runs here)
│       ├── idea "Use Kafka as event store" {status: mentioned}
│       │   └── RELATES_TO → idea "Event sourcing for orchestration"
│       └── decision "Evaluate Kafka vs EventStoreDB" {status: proposed}
└── topic "Event sourcing architecture"
    └── (groups the above ideas/questions — created by extraction)
```

Key: `turn` nodes hold the raw text. `idea`/`question`/`decision` nodes are
extracted from turns but live as children of the `conversation`, not the `turn`.
This lets you query ideas independently of the conversation flow.

Actually — ideas should be children of the `conversation` (or `topic`), not the
`turn`. Turns are the timeline. Ideas are the extracted knowledge. They're linked
by `EXTRACTED` edges in the graph, not parent-child relationships.

Revised:
```
conversation "Session 2026-03-08 14:30"
├── turn [user, text, order=100] "What if we used event sourcing?"
├── turn [claude, text, order=200] "Event sourcing would work well for..."
├── turn [user, voice, order=300] "Yeah and maybe we could use Kafka"
├── turn [gemini, voice, order=400] "Kafka is one option, but consider..."
├── topic "Event sourcing architecture" [order=1000]
│   ├── idea "Event sourcing for orchestration" {status: mentioned}
│   ├── idea "Use Kafka as event store" {status: mentioned}
│   ├── question "Which event store?" {status: unresolved}
│   └── decision "Evaluate Kafka vs EventStoreDB" {status: proposed}
└── action_item "Research EventStoreDB vs Kafka" {status: pending}

Graph edges (AGE):
  turn[200] ──EXTRACTED──→ idea "Event sourcing for orchestration"
  turn[200] ──EXTRACTED──→ question "Which event store?"
  turn[400] ──EXTRACTED──→ idea "Use Kafka as event store"
  turn[400] ──EXTRACTED──→ decision "Evaluate Kafka vs EventStoreDB"
  idea "Kafka" ──RELATES_TO──→ idea "Event sourcing"
  question "Which event store?" ──BLOCKS──→ decision "Evaluate..."
```

### Context Router Design

The context router answers: "Given this user input, what context does each model need?"

```
Input: user message + conversation_id
  │
  ▼
┌─────────────────────────────────────────┐
│ 1. INTENT CLASSIFICATION                │
│    (What kind of request is this?)       │
│                                          │
│    ideation  — exploring ideas           │
│    deepening — drilling into one idea    │
│    planning  — turning idea into steps   │
│    reviewing — evaluating prior work     │
│    executing — writing code/doing work   │
│    recalling — "what did I say about X?" │
│    parking   — "park that for now"       │
│    resuming  — "let's revisit X"         │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ 2. CONTEXT ASSEMBLY                     │
│    (What does the model need to know?)   │
│                                          │
│    Per intent, run queries:              │
│                                          │
│    ideation:                             │
│      - pgvector: similar prior ideas     │
│      - AGE: related threads              │
│      - status: open threads count        │
│                                          │
│    deepening:                            │
│      - subtree: the specific idea tree   │
│      - AGE: connected ideas/decisions    │
│      - turns: last 3 turns (not 20)      │
│                                          │
│    planning:                             │
│      - idea node + its relations         │
│      - code nodes it might touch         │
│      - existing plans in same domain     │
│                                          │
│    recalling:                            │
│      - pgvector: semantic match on ideas │
│      - turns where it was discussed      │
│      - current status of matched ideas   │
│                                          │
│    parking:                              │
│      - just the idea node to update      │
│      (no model call needed — direct DB)  │
│                                          │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ 3. MODEL SELECTION                      │
│    (Which model handles this?)           │
│                                          │
│    ideation  → conversation model        │
│                (Gemini for voice,        │
│                 any for text)            │
│    deepening → Claude (best reasoning)   │
│    planning  → Claude (structured out)   │
│    reviewing → Codex (second opinion)    │
│    executing → Claude Code (has tools)   │
│    recalling → local query (no model)    │
│    parking   → direct DB mutation        │
│    resuming  → Claude (briefs user)      │
└──────────────┬──────────────────────────┘
               ▼
┌─────────────────────────────────────────┐
│ 4. PROMPT ASSEMBLY                      │
│    Build model-specific prompt:          │
│                                          │
│    <context>                             │
│      <relevant_ideas>...</relevant>      │
│      <open_threads>...</open_threads>    │
│      <code_context>...</code_context>    │
│    </context>                            │
│    <role>...</role>                       │
│    <user_input>...</user_input>          │
└─────────────────────────────────────────┘
```

### What the Context Router is NOT

- Not a chat history manager. It doesn't store or replay messages.
- Not a model proxy. It doesn't call models — it assembles context for callers.
- Not an intent classifier model. Intent classification is keyword/pattern-based
  first (fast), with model-based fallback only for ambiguous cases.

The caller (Hekate chat, voice pipeline, CLI) calls the context router with
a user message and conversation ID. It gets back a context payload. The caller
then sends that payload to the appropriate model.

## Approach

### Step 1: Ideation Node Types + Seeder
- Add `ConversationSeeder` — seeds a sample conversation with turns, topics, ideas
- Follows same pattern as PlanSeeder (helper methods per node type)
- Creates graph edges between turns and extracted ideas

### Step 2: Conversation Renderer
- Walks conversation tree, renders as threaded transcript
- Shows turn speaker, input mode, timestamp
- Shows extracted ideas with status
- Follows PlanRenderer pattern (switch on node_type)

### Step 3: Context Router (C# library, not endpoint yet)
- `IntentClassifier` — pattern-based intent detection from user input
- `ContextAssembler` — runs DB queries per intent, returns structured context
- `PromptBuilder` — takes assembled context + user input, builds model-ready prompt
- All three are pure C# classes with NodeRepository dependency

### Step 4: Wire into Program.cs
- Seed sample conversation
- Run context router against sample inputs
- Show assembled context vs raw history (the comparison)
- Prove: same conversation, different intents → different context payloads

## Outputs

| Path | Action | What Changes |
|------|--------|-------------|
| SeedData/ConversationSeeder.cs | create | Sample conversation with turns, topics, ideas |
| Renderer/ConversationRenderer.cs | create | Conversation tree → threaded transcript |
| ContextRouter/IntentClassifier.cs | create | Pattern-based intent detection |
| ContextRouter/ContextAssembler.cs | create | DB queries per intent → context payload |
| ContextRouter/PromptBuilder.cs | create | Context + input → model-ready prompt |
| ContextRouter/Types.cs | create | Intent enum, ContextPayload, AssembledPrompt |
| Program.cs | modify | Add conversation + context router demo |
| CLAUDE.md | modify | Document new components |

## Testing

- **Unit:** ConversationSeeder creates correct node/edge structure
- **Unit:** IntentClassifier detects known patterns (ideation, parking, recalling)
- **Unit:** ContextAssembler returns different payloads for different intents on same conversation
- **Integration:** Full pipeline — seed conversation → classify intent → assemble context → build prompt → verify prompt contains relevant ideas but not full history

## Questions

1. **Intent classification**: Start with pattern matching (keywords/regex) or jump to model-based? **Proposed: Pattern-based first. Fast, deterministic, no API cost. Model fallback for ambiguous cases later.**

2. **Extraction timing**: Extract ideas synchronously after each turn, or batch after conversation ends? **Proposed: After each model response turn. Keeps ideas current for mid-conversation context assembly.**

## Risks

- **Over-extraction**: Claude might extract too many "ideas" from casual conversation. Mitigation: confidence threshold attribute, only surface high-confidence extractions.
- **Intent misclassification**: Pattern matching may be too rigid. Mitigation: default to "ideation" intent (safest/broadest context), add model fallback.
- **Graph query latency**: Many Cypher queries per context assembly. Mitigation: batch queries, cache recent results.
