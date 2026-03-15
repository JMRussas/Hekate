# Plan 001 — Voice-Enabled Ideation Assistant

**Level:** L2
**Status:** PLAN (awaiting approval)
**Created:** 2026-03-08
**Created by:** claude-opus-4-6
**Origin:** Voice interaction design session (text-based ideation)

## Context

The user has a twice-exceptional brain — rapid ideation, many good ideas, inconsistent follow-through. Needs a voice-enabled AI sounding board that:
- Captures ideas as they flow during whiteboard sessions
- Tracks every idea through a lifecycle (mentioned → explored → committed → completed/parked)
- Surfaces "loose threads" — ideas mentioned but never followed up on
- Integrates with Hekate (VS Code extension for AI orchestration)

Existing infrastructure:
- **CodeStoragePoc**: PostgreSQL 16 + Apache AGE + pgvector on port 5433, stores C# code as node trees
- **Hekate**: VS Code extension with chat view, fleet tree, multi-model support (Claude, Gemini, Codex, Ollama)
- **Orchestration Engine**: FastAPI backend on port 5200
- **Local GPU**: RTX 4090 (main), RTX 3090 (server), both with Ollama + nomic-embed-text
- **AI CLIs**: Claude Code, Gemini CLI, Codex CLI — all on subscription (no per-token cost)

## Thinking

### Key Design Decision: Unified Node Model

Everything is a node in the same PostgreSQL table — code, conversations, plans, ideas. The boundaries between "I had an idea," "I made a plan," and "I wrote code" are graph edges, not separate systems.

This means:
- An idea node can have an `IMPLEMENTED_BY` edge to a plan_step node, which has a `PRODUCES` edge to a method node
- One Cypher query traces the full lineage: voice conversation → idea → plan → code
- pgvector similarity search works across domains (find code related to an idea, or ideas related to code)

### Key Design Decision: Context Router (not chat history)

The user sees the full conversation thread. Each model gets only the context it needs — assembled from DB queries (pgvector similarity, graph neighbors, thread status), not from replaying chat history. This keeps token usage minimal and context relevant.

### Key Design Decision: Split-Brain Model Roles

| Role | Model | Why |
|------|-------|-----|
| Voice conversation | Gemini Live | Native voice, sub-300ms, on subscription |
| Extraction & analysis | Claude | Best structured output, best at inferring intent |
| Session briefing | Claude | Writes "here's what you left hanging" for session start |
| Code review / 2nd opinion | Codex CLI | Different perspective on implementation |
| Local embeddings | nomic-embed-text | Free, runs on either machine |

### Key Design Decision: Plans in the Database

This very plan is the first test case. Plans are stored as node trees alongside code and ideas. The plan format maps to node types:

```
plan → plan_phase → plan_step → task
                 → risk
                 → test_spec
                 → revision
                 → retrospective
```

## Approach

### Phase 1: Schema Extension (this PR)

1. Add new node types for ideation domain: `conversation`, `turn`, `topic`, `idea`, `question`, `decision`, `action_item`, `thread`
2. Add new node types for planning domain: `plan`, `plan_phase`, `plan_step`, `task`, `risk`, `test_spec`, `revision`, `retrospective`
3. Add new AGE edge labels: `EXTRACTED`, `SPAWNED_FROM`, `IMPLEMENTED_BY`, `PRODUCES`, `MODIFIES`, `CONSTRAINS`, `BLOCKS`, `RELATES_TO`, `CONTRADICTS`, `REFERENCES` (exists), `DEPENDS_ON` (exists)
4. Build PlanSeeder — inserts THIS plan as the first test data
5. Build PlanRenderer — reads plan back from DB, renders as readable text
6. Verify round-trip: plan markdown → DB nodes → rendered text → compare

### Phase 2: Context Router Service

7. Define intent classifier (what kind of request is this?)
8. Build query assembler (pgvector + graph + status → per-model context)
9. Add context router endpoint to Orchestration Engine
10. Wire Hekate chat to use context router instead of raw history

### Phase 3: Voice Layer

11. Gemini Live integration (or Pipecat + local STT/TTS as fallback)
12. Transcript capture → extraction pipeline
13. Claude extraction (structured output → nodes in DB)
14. Session-start briefing (query DB → write Gemini system prompt)

### Phase 4: Thread Tracking

15. Idea lifecycle state machine
16. Stale thread detection queries
17. Periodic resurfacing prompts
18. Park/drop/explore commands

## Outputs

| Path | Action | What Changes |
|------|--------|-------------|
| init.sql | modify | Add new AGE edge labels |
| DbLayer/Schema.cs | modify | (no schema changes needed — TEXT node_type) |
| SeedData/PlanSeeder.cs | create | Inserts this plan as node tree |
| SeedData/IdeationSeeder.cs | create | Seed example conversation/ideas |
| Renderer/PlanRenderer.cs | create | Reads plan from DB, renders as text |
| Program.cs | modify | Add plan seeding + rendering demo step |
| CLAUDE.md | modify | Document new node types and edge labels |
| DESIGN.md | modify | Add design decision for unified node model |

## Testing

- **Unit:** PlanSeeder inserts correct node count with correct parent-child relationships
- **Unit:** PlanRenderer produces readable output matching the original plan structure
- **Integration:** Round-trip — plan markdown → DB → rendered text → structural comparison
- **Integration:** AGE graph queries — traverse from plan to idea to code nodes

## Questions

1. **Embedding dimension**: CodeStoragePoc uses 1536 (OpenAI). Local nomic-embed-text produces 768. Should we: (a) standardize on 768 for new nodes, (b) keep 1536 and pad nomic output, (c) add a dimension column? **Proposed: (a) — alter column for voice/plan project, keep 1536 for code project.**

2. **File table**: Plans and conversations don't map to files. The `file_id` column is nullable, so plans just leave it NULL. Is that sufficient or do we want a `domain` column on the project? **Proposed: file_id = NULL is fine for now.**

## Risks

- **AGE performance**: Many small Cypher queries (one per vertex MERGE) may be slow for large plans. Mitigation: batch vertex creation.
- **Node type sprawl**: 20+ node types in one table. Mitigation: application-level validation, documented in CLAUDE.md.
- **Cross-domain queries**: pgvector similarity across different embedding models (1536 vs 768) won't work. Mitigation: standardize on one dimension per project.

---

*This plan will be stored in PostgreSQL as its own first test case.*
