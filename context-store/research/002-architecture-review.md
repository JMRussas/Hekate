# Research 002 — Architecture Review Findings (2026-03-08)

Combined review from Claude Opus 4.6 and Gemini CLI of the CodeStoragePoc unified node model.

## Review Summary

| Area | Rating | Verdict |
|------|--------|---------|
| Unified node model | Sound | Correct bet for AI-driven engineering. Traceability is the killer feature. |
| Context router pipeline | Strong | Best architectural asset. Intent-driven > history replay. |
| Transport-agnostic design | Clean | Successfully decoupled. Minor metadata gaps. |
| Code quality | POC-appropriate | Known shortcuts documented. N+1 query issue flagged. |
| Production readiness | Not yet | 5 critical gaps identified. |

## Issues Found — Must Fix

### 1. N+1 Attribute Subqueries (ContextAssembler.cs)
**Where:** `GetRecentIdeas()`, lines 137-147
**Problem:** Uses correlated subquery `(SELECT value FROM node_attributes WHERE node_id = n.id AND key = 'status')` per row. At 50 ideas, that's 50 subqueries.
**Fix:** JOIN node_attributes or use a lateral join. Same pattern used in `GetConversationStats()` is better.
**Severity:** Medium — works fine for POC, breaks at scale.

### 2. Intent Classifier Brittleness (IntentClassifier.cs)
**Where:** Hardcoded string patterns, lines 21-69
**Problem:** "Let's put this idea in the garage for a bit" won't match Parking intent. Voice transcripts especially will use varied phrasing.
**Fix (near-term):** Add more patterns, fuzzy matching.
**Fix (production):** Use a cheap/fast model (Haiku, Gemini Flash, or local qwen2.5-coder) for classification. Cost negligible.
**Severity:** Low for POC (defaults to Ideation safely), Medium for production.

### 3. No STT Confidence Scores
**Where:** ConversationSeeder.cs turn nodes
**Problem:** Voice transcripts treated with same truth value as typed text. STT errors could produce incorrect idea extractions.
**Fix:** Add `stt_confidence` attribute to turn nodes. ContextAssembler can deprioritize low-confidence segments.
**Severity:** Low — only matters when real voice pipeline is connected.

## Issues Found — Production Blockers

### 4. Graph/Relational Atomicity Gap
**Where:** AgeLayer.cs sync called after relational mutations
**Problem:** If relational insert succeeds but AGE sync fails (or app crashes between), graph becomes stale. Currently eventual consistency is accepted but no recovery mechanism exists.
**Fix:** Outbox pattern — write a `graph_sync_queue` row in same transaction as the node insert. Background worker processes the queue and syncs AGE.
**Severity:** High for production. Fine for POC.

### 5. No Node Versioning
**Where:** NodeRepository.cs UpdateNode()
**Problem:** Updates overwrite in place. If an agent ruins a method node, previous code is lost. Only `modified_at` timestamp exists.
**Fix:** `node_versions` table with temporal approach, or use Postgres temporal tables extension.
**Severity:** High for production (especially for code nodes). Fine for POC.

### 6. Embedding Refresh
**Where:** Embeddings seeded once in demo
**Problem:** When an idea's `value` changes, its embedding becomes stale. No trigger or worker to re-embed.
**Fix:** Change notification already exists (LISTEN/NOTIFY). Add a subscriber that queues changed nodes for re-embedding via nomic-embed-text.
**Severity:** Medium. Stale embeddings = degraded semantic search quality.

### 7. AGE Sync Scalability
**Where:** AgeLayer.SyncAllVertices() — one MERGE per node
**Problem:** Linear in node count. With thousands of nodes, sync becomes slow.
**Fix:** Batch vertex creation. AGE supports multi-row MERGE but the syntax is different.
**Severity:** Medium. Won't matter until >1000 nodes per sync.

### 8. Sibling Order Contention
**Where:** NodeRepository.NextSiblingOrder() with FOR UPDATE
**Problem:** Multiple agents adding children to the same parent serialize on the parent lock. Bottleneck for busy namespace/class nodes.
**Fix:** Use random gap insertion (e.g., random offset within range) or ULID-based ordering instead of sequential integers.
**Severity:** Low for single-agent use. Medium for multi-agent.

## Architecture Strengths Confirmed

1. **Cross-domain graph tracing** — Cypher query from voice turn → idea → plan step → code method. Unique capability.
2. **Context router as core asset** — Intent-driven context assembly is fundamentally better than chat history replay.
3. **Unified CRUD** — Same NodeRepository, same recursive CTE, same advisory locks work for all domains.
4. **Transport agnosticism** — Clean separation. Voice pipeline is an input adapter, not an architecture change.
5. **Plans as first-class data** — Status tracking, cross-plan queries, retrospectives all enabled by node model.

## Recommendations (Priority Order)

1. Add `finding` node type for research (next session)
2. Fix N+1 attribute query in ContextAssembler (quick win)
3. Add STT confidence attribute to turn schema (before voice integration)
4. Design outbox pattern for graph sync (before multi-agent use)
5. Add node versioning (before production code generation)
6. Move intent classification to model-based (before voice integration)
7. Add embedding refresh worker (before pgvector semantic search is relied on)
