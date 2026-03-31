# Hekate Action Items
**Date:** 2026-03-31
**Source:** Platform analysis (codebase + execution history)

---

## P0 — Data Loss / Stuck State Risk

### 1. Implement startup recovery
**File:** `Odin/gods/handlers/odin.py` or new `Odin/gods/recovery.py`
**Problem:** No startup recovery logic exists. On restart, tasks stuck in RUNNING/DISPATCHED are never re-triggered. The pipeline restores its cursor (`pipeline.py:285`) but orphaned tasks in the DB remain stuck. 2 projects currently stuck in "executing."
**Fix:** Scan for RUNNING tasks with no active Hermes slot → emit dispatch_command. Scan for EXECUTING projects with 0 running tasks → emit project_tick.

### 2. Fix planning failures (top failure mode)
**Files:** `Odin/gods/handlers/athena_leveled.py`, `Odin/gods/providers/response_validator.py`
**Problem:** 26 planning_failed events. JSON parse failure → empty placeholder plan → 0 tasks → project fails. Multiple project re-creations (GifterBoard x5, SSE Health x6).
**Fix:** Retry JSON extraction with feedback ("your response was not valid JSON, try again"). Reject empty-phase plans immediately. Add structured output mode for Claude CLI.

### 3. Fix relay write failure escalation
**File:** `Odin/gods/handlers/hermes_async.py`
**Problem:** Relay write failure logs CRITICAL but takes no action. Tasks stuck in running state forever.
**Fix:** On relay failure, fall back to direct DB update of task status. Emit alert event.

### 4. Fix cursor persistence error swallowing
**File:** `Odin/gods/pipeline.py` (lines 322-331)
**Problem:** `except Exception: pass` on cursor persistence. If DB fails, restart replays all events.
**Fix:** Retry with backoff. Log at ERROR. After 3 failures, emit pipeline_degraded event.

---

## P1 — Silent Failures / Fragility

### 5. Cache provider availability on failure
**File:** `Odin/gods/handlers/odin.py` (lines 91-92)
**Problem:** Gateway /providers timeout → ALL providers marked down → dispatch blocked. No retry or cache.
**Fix:** Cache last-good provider state. Use cached values when gateway is unreachable (with TTL).

### 6. Wire provider tracking into context_json
**Files:** `Odin/gods/handlers/hermes_async.py`, `Odin/gods/handlers/odin.py`
**Problem:** Provider field is NULL for all 286 tasks. Cannot analyze which provider was used.
**Fix:** Store provider name in context_json at dispatch time. Store worktree_path too.

### 7. Fix branch creation silent fallback
**File:** `Odin/gods/handlers/hephaestus.py` (lines 237-243)
**Problem:** If git checkout -b fails, silently falls back to current branch. Commits to main.
**Fix:** Fail the git stage step if branch creation fails. Emit warning event. Never commit to main silently.

### 8. Fix PR creation failure handling
**File:** `Odin/gods/handlers/hephaestus.py` (lines 295-299)
**Problem:** PR creation failure logged but project_committed emitted anyway.
**Fix:** Emit project_committed_no_pr variant. Or retry PR creation.

### 9. Bound deferred queues
**Files:** `Odin/gods/handlers/hermes_async.py`, `Odin/gods/handlers/mimir.py`
**Problem:** Both `_deferred` queues have no max size. Under sustained load, unbounded append grows memory. Hermes dispatch queue and Mimir verification queue both affected.
**Fix:** Add max_deferred_size (default 50). Reject with back-pressure when full.

### 10. Fix Mimir drain/wave_complete race
**File:** `Odin/gods/handlers/mimir.py`
**Problem:** wave_complete can fire before deferred verifications drain.
**Fix:** Track pending verification count. Block wave_complete until drain completes.

### 10a. Add circuit breaker on context store
**File:** `Odin/gods/handlers/context_bridge.py`
**Problem:** All context store errors silently caught. If context store is down, plan/task sync silently fails with no alerting. Pipeline continues blind.
**Fix:** Add circuit breaker (open after 3 consecutive failures, half-open after 30s). Emit context_store_degraded event when circuit opens.

### 10b. Guard concurrent task transitions
**File:** `Odin/gods/pipeline.py` (transition_and_emit, ~lines 684-737)
**Problem:** Transaction doesn't handle concurrent modifications. Two handlers transitioning the same task simultaneously can race.
**Fix:** Add optimistic locking (check current state in UPDATE WHERE clause). Return false if state changed underneath.

---

## P2 — Architectural Improvements

### 11. Wire budget gate into dispatch
**File:** `Odin/gods/handlers/tyche.py`
**Problem:** Budget tracking exists but gate NOT registered. All tasks dispatch regardless of budget. Neural Roguelike cost $20.28.
**Fix:** Register tyche_check_budget as pre-dispatch gate. Add per-project budget limit.

### 12. Harden state machine enforcement
**File:** `Odin/gods/task_states.py`
**Problem:** Invalid transitions logged as warning but ALLOWED (soft enforcement). State corruption possible.
**Fix:** Convert to hard error. Add 3-strike counter — after 3 invalid transitions for same task, mark needs_review.

### 13. Add project-level retry
**File:** `Odin/gods/handlers/odin.py` or new handler
**Problem:** Planning failures require manual project re-creation (v2, v3, v4).
**Fix:** On planning_failed, auto-retry with modified prompt (include error context). Max 2 retries. Emit project_retry event.

### 14. Clean up stuck state
**Problem:** 34 blocked + 33 pending tasks in deployment. 2 projects still "executing."
**Fix:** Add periodic cleanup: projects in EXECUTING with no activity for 1h → mark failed. Tasks in PENDING with project completed → mark cancelled.

### 15. Add batch DELETE limit for event cleanup
**File:** `Odin/gods/pipeline.py` (line 337)
**Problem:** DELETE without LIMIT on god_relay_events can lock table for seconds under high volume.
**Fix:** Use `DELETE ... LIMIT 10000` and batch across ticks.

---

## P3 — Code Quality / Maintainability

### 16. Remove dead code
- `dispatch.py`: Budget sensitivity code (budget_remaining never passed)
- `dispatch.py`: diagnose_failure() never called
- Gemini CLI provider code (broken, all tasks route to Claude)

### 17. Centralize row unpacking
**Files:** `odin.py`, `gates.py` (20+ instances of _val / isinstance checks)
**Fix:** Create typed row accessor class. Use it everywhere.

### 18. Add JSON schema validation for context_json
**Problem:** 10+ instances of JSON parsing without validation. Silent data degradation.
**Fix:** Define TaskDefinition JSON schema. Validate on load. Reject malformed data.

### 19. Deduplicate dedup logic
**Files:** `pipeline.py`, `relay.py`
**Problem:** Both implement event deduplication with different semantics.
**Fix:** Single dedup implementation in pipeline. Relay uses pipeline's dedup.

### 20. Fix O(n*m) context store task search
**File:** `Odin/gods/handlers/context_bridge.py`
**Problem:** Linear search through all plans × all tasks per task_verified event.
**Fix:** Maintain in-memory task_id → node_id mapping. Populate on project_planned.

---

## P4 — Future / When Needed

### 21. Wire FORK/JOIN/DECISION into planners
**Problem:** Infrastructure complete, but no code creates workflow_edges. Planners produce linear plans only.
**Action:** Add workflow edge creation to Athena's decomposer. Add branching syntax to plan JSON format. Write tests.

### 22. Implement timeout enforcement
**Problem:** TaskDefinition has response_timeout_seconds but no background enforcer.
**Action:** Background monitor checks RUNNING tasks against timeout. Emit task_timeout event.

### 23. Add task definition registry
**Problem:** get_registry() exists but registry content is minimal. Not enforced.
**Action:** Define standard task types with policies. Enforce at decomposition time.

### 24. Sync source and deployment databases
**Problem:** Source DB has 52 projects, deployment has 67. They diverge.
**Action:** Make deployment DB authoritative. Source DB only for dev testing. Document clearly.

---

## Quick Wins (< 1 hour each)

| # | Item | Impact |
|---|------|--------|
| 4 | Replace `except Exception: pass` on cursor | Reliability |
| 6 | Wire provider into context_json | Observability |
| 9 | Bound deferred queues (add max_size) | Stability |
| 10b | Optimistic locking on transitions | Correctness |
| 12 | Hard state machine enforcement | Correctness |
| 15 | Batch DELETE limit | Performance |
| 16 | Remove dead code | Clarity |
