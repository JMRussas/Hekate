# Hekate Platform Analysis — 2026-03-20

Hekate analyzing itself. Six components, ~127k lines of code, 97k Python / 13k C# / 16k TypeScript.

---

## Executive Summary

**What's good:** The gods pipeline is a genuinely well-designed event-driven architecture. The PromptSpec/CLIProvider abstractions are clean. Test coverage in the gods pipeline (30 files, ~12k lines) and orchestration (96 files) is strong. The dual-backend DB with transparent Postgres/SQLite switching is clever engineering. Secret redaction in the extension is unusually thoughtful.

**What's dangerous:** Hades is an unauthenticated remote shell on the LAN. The LLM Gateway has no auth either. Seven stale worktrees with broken permissions are accumulating. ~34 debug JSON files are committed to the repo.

**What needs work:** Massive uncommitted delta (71 files, +2.5k/-10.7k lines). Dead code across every component. Two competing dispatch systems in the gods pipeline. Several runtime bugs waiting to fire.

---

## I. CRITICAL — Fix Now

### 1. Hades is an unauthenticated RCE service
- **hades/server.py** — Bound to `0.0.0.0:5201`, no auth, `allow_origins=["*"]`
- `POST /exec` accepts arbitrary shell commands. Any device on the LAN can run anything as LocalSystem.
- `DELETE /services/{name}?confirm=true` can remove NSSM services.
- `POST /deploy` triggers full deployment.
- **Fix:** Add API key auth (even a shared secret header). Allowlist `/exec` commands or remove the endpoint entirely.

### 2. LLM Gateway has no authentication
- **llm-gateway/server.py:266** — Bound to `0.0.0.0:5210`, no auth, `allow_origins=["*"]`
- Any LAN device can invoke Claude/Gemini CLIs, spending money.
- **Fix:** Shared secret header at minimum.

### 3. Missing `import asyncio` in orchestration app.py
- **orchestration/backend/app.py:143** — `await asyncio.sleep(2)` in `_graceful_shutdown()` but `asyncio` is never imported.
- Graceful shutdown will crash with `NameError`.
- **Fix:** Add `import asyncio` at the top of the file.

### 4. Duplicate function definition in task_lifecycle.py
- **orchestration/backend/services/task_lifecycle.py:704-843** — `create_reassessment_intervention` is defined twice, identically. Second shadows first. References `rationale` and `plan_id` columns that don't exist on the `checkpoints` table.
- **Fix:** Delete the duplicate. Remove the dead column references.

---

## II. HIGH — Fix Soon

### 5. Two Odin dispatch systems racing
- `Odin/gods/handlers/odin.py` (pipeline path) and `Odin/gods/odin/server.py` (standalone MCP server) both:
  - Read the same orchestration DB
  - Modify task statuses
  - Dispatch tasks to providers
  - Run on different tick intervals
- If both run simultaneously, they race on task assignment — potentially double-dispatching.
- **Fix:** Deprecate `odin/server.py` or add coordination (claimed_by lock).

### 6. Pipeline cursor advance before dispatch
- **Odin/gods/pipeline.py:543** — `_last_seen_id` is advanced in `_poll()` before handlers run. If the process crashes mid-dispatch, those events are permanently lost.
- This is at-most-once delivery by design, but undocumented.
- **Fix:** Advance cursor after successful dispatch, or document the at-most-once guarantee.

### 7. Athena swallows decomposition failure
- **Odin/gods/handlers/athena_leveled.py:651** — If `_decompose_plan` throws, the project is still marked `planned` with zero tasks. Odin then "completes" it immediately having done nothing.
- **Fix:** Check task count > 0 before emitting `project_planned`.

### 8. Schema mismatches (orchestration)
- **connection.py:258-280** vs **models_metadata.py:263-294** — `sentinel_observations` and `odin_decisions` tables have different columns. Tests use `connection.py`, production uses Alembic. Tests may pass on wrong schema.
- **Fix:** Sync `connection.py` inline schema with the Alembic-managed schema, or remove the inline DDL.

### 9. `fix_queue.count_by_status()` will crash at runtime
- **orchestration/backend/services/fix_queue.py:161** — `await self._db.fetchall(...)` called with no params tuple. Will fail.
- **Fix:** Add `params=()`.

### 10. Hephaestus handler is dead code
- **Odin/gods/handlers/hephaestus.py:77** — Expects `affected_files` in `task_verified` events. Mimir never includes this field. Handler always returns `None`.
- **Fix:** Have Mimir include `affected_files` in its emit, or remove Hephaestus from registration.

### 11. Mimir 600s timeout blocks all pipeline processing
- **Odin/gods/handlers/mimir.py:105** — LLM verification timeout of 600s. Pipeline processes events sequentially, so one slow verification blocks everything.
- **Fix:** Run Mimir verification as a background task (like Hermes), or reduce timeout + add fallback.

---

## III. MEDIUM — Tech Debt

### 12. God classes need decomposition
| File | Lines | Responsibilities |
|------|-------|-----------------|
| `orchestration/backend/services/executor.py` | 1165 | Dispatch, worktree mgmt, quota, stale recovery, wave PRs, auto-merge, branches |
| `orchestration/backend/services/task_lifecycle.py` | ~900 | Execution, verification, review, checkpoints, migration validation, diagnostics, telemetry |
| `context-store/Api/Services/ChatService.cs` | 1277 | SSE, CLI spawning, Ollama HTTP, tool calling, skills, conversation CRUD, embedding, permissions, model routing |

### 13. Dead code inventory
**Gods pipeline (can delete):**
| File | Reason |
|------|--------|
| `Odin/gods/handlers/athena.py` | Replaced by `athena_leveled.py`, imports monolith |
| `Odin/gods/handlers/hermes.py` | Synchronous, replaced by `hermes_async.py` |
| `Odin/gods/handlers/hermes_cli.py` | Only used by dead `hermes.py` |
| `Odin/gods/gates.py` | 342 lines, fully implemented, zero registrations |
| `Odin/gods/review.py` | Only imported by dead `athena.py` |
| `Odin/gods/repair.py` | `repair_command` events never emitted |
| `Odin/gods/plan_rules.py` | Duplicate of `plan_levels.py` |

**Orchestration:**
| Item | Location |
|------|----------|
| `_teardown_plan_sentinel` | executor.py:977 — empty `pass` method |
| `BudgetGuard` + `AsyncBudgetManager` | budget.py:334-565 — near-identical classes |
| `generate_plan()` wrapper | planner.py:910-922 |
| `decompose_plan()` wrapper | decomposer.py:301-303 |
| 22 deleted sentinel files | Still in git status as uncommitted deletes |

**Context Store:**
| Item | Location |
|------|----------|
| `InterpreterService.ParseResponse()` | Never called, replaced by `ParseResolvedResponse` |
| `ContextAssembler.GetCrossProjectIdeas()` | Private, never called |
| `ChatService._classifier` field | Stored but never accessed |
| `GetAvailableModels()` | `async` with no `await` |
| Root `Program.cs` (CodeStoragePoc) | Demo/seeder, never deployed |

### 14. Triple-hardcoded ProjectId GUID
- `ChatService`, `ExtractionService`, `PendingActionService` all hardcode `8196b44e-6299-45a0-a5b0-bbd111f2990b`
- **Fix:** Single constant or config value.

### 15. IVFFlat index with `lists=1`
- **context-store/Schema.cs:94** — `WITH (lists = 1)` makes the vector index useless (sequential scan). Comment says "POC" but this is deployed.
- **Fix:** Set `lists` to `sqrt(row_count)` or at least 10.

### 16. No interfaces in Context Store C#
- Every service is a concrete class. DI registration is all `AddSingleton` with `new`. Testing requires a live Postgres.
- **Fix:** Extract interfaces for testability. Start with `INodeRepository`, `IChatService`.

### 17. Error handling patterns
| Pattern | Locations |
|---------|-----------|
| Bare `except: pass` | model_router.py:284, cli_common.py:211 |
| `catch { }` empty catch | NodeService:393, :476; InterpreterService:828, :858 |
| Fire-and-forget with no retry | ChatService:902 (embeddings), ExtractionService:226 |
| Codex return code not checked | llm-gateway/server.py:318-331 |

### 18. Async violations in orchestration
| Issue | Location |
|-------|----------|
| Sync `subprocess.run` in async | task_lifecycle.py:107 |
| Sync file I/O in async | task_lifecycle.py:130-143 |
| Sync file read in async | decomposer.py:42 |

### 19. Gateway provider health check too aggressive
- **Odin/gods/handlers/odin.py:261** — `_get_provider_availability()` calls LLM Gateway on every tick. If gateway is down, ALL dispatch stops even for providers that don't need it.
- **Fix:** Cache availability with TTL, or only check for providers that route through the gateway.

---

## IV. LOW — Cleanup

### 20. Seven stale worktrees with broken permissions
All created by LocalSystem (NSSM), owned by `S-1-5-32-544`, inaccessible to your user:
- `comfyui-mcp-server`
- `iterative-wave-reassessment`
- `orch-odin-orchestration-brain-god`
- `plan-status-sync-to-context-store`
- `retry-diagnosis-before-retry`
- `sentinel-server-restart-capability`
- `whys-logic-decision-rationale`

**Fix:** From admin terminal: `git worktree remove --force <name>` for each.

### 21. ~34 debug JSON artifacts in orchestration/backend/
Files like `ec36_full.json`, `pl.json`, `slot_tasks.json`, `sentinel_full.json`, etc. Debug dumps from testing.
**Fix:** Add to `.gitignore` and delete.

### 22. Massive uncommitted delta
71 files changed, +2483/-10782 lines. Sentinel system deletion is the bulk.
**Fix:** Commit in logical chunks (sentinel deletion, gods pipeline changes, new features).

### 23. Dead imports in LLM Gateway
- `import uuid` and `from collections import deque` — never used
- `GeminiPool._pool_size` — stored, never referenced
- `GeminiPool._lock` — created, never used

### 24. Context Store index stale
- 16 files modified since last `build_index`. Extension has no index at all.
- **Fix:** `mcp__hekate__build_index` on both.

### 25. Hardcoded path in CliResolver
- **context-store/CliResolver.cs:51** — `@"C:\Users\jruss\AppData\Roaming\npm"` hardcoded to your profile.

### 26. Extension: no tests, command handlers have no try/catch
- Zero test files. Command handlers (`startProject`, `pauseProject`, etc.) have no error handling — failures propagate as unhandled rejections.
- SSE "exponential backoff" comment but implementation is constant delay.
- N+1 API calls in `listProjects`.

### 27. `root_path NOT NULL` vs nullable API
- **context-store/Schema.cs:41** — `root_path TEXT NOT NULL`
- **Program.cs:337** — Allows null via `DBNull.Value`. Will throw constraint violation at runtime.

### 28. Log tailing reads entire file
- **hades/server.py:793** — `f.readlines()` loads complete log file into memory. Could be gigabytes.
- **Fix:** Use `deque(f, maxlen=N)` or seek from end.

---

## V. What's Good

### Gods Pipeline Architecture
The event-driven design with ID-based cursors, handler isolation, and cursor persistence is solid. The gate system (even though unused) is well-engineered. Handler error boundaries are correct — exceptions are caught, logged, and turned into `handler_error` events. The pipeline never crashes.

### Hermes Async Execution
Background task launching with slot management, dedup, heartbeats, and `asyncio.shield()` for cleanup writes. Tasks never get stuck — every path writes a `worker_event`. This is battle-tested code.

### PromptSpec / CLIProvider Abstraction
Model-agnostic prompt specs that render per-provider. `CLIProvider` in `cli_provider.py` wraps 40+ CLI flags per provider with proper capability flags and stream parsing. Clean separation.

### Test Coverage
| Component | Test Files | Estimated Lines |
|-----------|-----------|----------------|
| Gods pipeline | 30 | ~12,000 |
| Orchestration unit | 60+ | ~15,000+ |
| Orchestration integration | 20+ | ~5,000+ |
| Orchestration frontend | 20+ | ~2,000+ |
| Context Store UI | 3 (e2e) | ~500 |

The `test_production_bugs.py` file (regression tests from real failures) is particularly good practice.

### Dual-Backend Database
Transparent Postgres/SQLite switching with SQL translation. WAL mode + busy_timeout on SQLite. Proper async backends (asyncpg / aiosqlite). Enables local dev without infrastructure.

### Secret Redaction (Extension)
First-class concern with both server-side (before sending to providers) and client-side (before display) implementations. Proper CSP nonces in webviews.

### Plan Levels Validation
Pure-function structural checks with circular dependency detection via DFS. Progressive validation L1→L5. Clean, well-tested code.

### Defensive LLM Output Handling
`safe_json.py` and `response_validator.py` — every function returns defaults on failure, never throws. `validate_verdict` ALWAYS returns a valid dict even from garbage input. Born from production pain, essential for LLM pipelines.

---

## VI. Codebase Metrics

| Metric | Value |
|--------|-------|
| **Total source files** | 522 (excl. worktrees, node_modules, obj) |
| **Python** | 361 files, 97,192 lines |
| **C#** | 46 files, 13,299 lines |
| **TypeScript/TSX** | 108 files, 16,051 lines |
| **JavaScript** | 7 files, 498 lines |
| **Alembic migrations** | 29 (001-029, sequential, no gaps) |
| **Test files** | ~160 (gods: 30, orch: 96, frontend: 20+, e2e: 6) |
| **NSSM services** | 8 |
| **Stale worktrees** | 7 |
| **Debug JSON artifacts** | ~34 |
| **TODO/FIXME comments** | ~3 (surprisingly few) |
| **C# project dependencies** | CodeStoragePoc → Api (no cycles) |
| **Hekate static analysis** | All reviewed files pass (0 violations) |
| **Context Store index** | Stale (16 files modified) |
| **Extension index** | Missing |

---

## VII. Recommended Priority Order

1. **Auth on Hades + LLM Gateway** — security exposure on every device on your LAN
2. **Commit the sentinel deletion** — 10k lines of deleted code in limbo
3. **Fix runtime bugs** — asyncio import, fix_queue params, duplicate function
4. **Clean worktrees** — admin terminal, `git worktree remove --force`
5. **Delete debug JSON files** — add `.gitignore` rules
6. **Wire Hephaestus or remove it** — dead handler cluttering the pipeline
7. **Document at-most-once delivery** — or fix cursor advance ordering
8. **Decompose god classes** — executor.py, task_lifecycle.py, ChatService.cs
9. **Remove dead code** — ~2000 lines across gods handlers, orchestration wrappers, C# services
10. **Rebuild indexes** — context store + extension
