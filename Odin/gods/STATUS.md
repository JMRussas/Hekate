# Gods Pipeline — Implementation Status

## What's Built

### Framework (213 tests, 9.10s)

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `pipeline.py` | 310 | 14 | Event loop, handler dispatch, ID-based cursor dedup, gates (pass/fail/retry/exhaust), narration callback |
| `gates.py` | 250 | 20 | check_plan_created, check_code_written, check_code_parses, check_verification_ran, check_tests_pass, check_test_written_first (TDD red/green/refactor), check_files_staged, check_pr_created, compose_gates |
| `review.py` | 170 | 13 | Plan self-review: adversarial LLM prompt, structured PlanReview response, confidence threshold, feedback formatting for planner comments |
| `diagnosis.py` | 250 | 20 | 30+ error pattern signatures, strategy selection (retry_with_fix/change_provider/split_task/escalate), history-aware dedup, why_chain audit trail |
| `repair.py` | 100 | 14 | retry_with_fix_handler (re-emit with guidance), change_provider_handler (re-emit with new provider), make_repair_router (registers all on pipeline) |
| `relay.py` | 350 | 25 | EventRelay, PostgresSink, LogSink, HttpSink, FileSink, CallbackSink, Gate (rate/type/severity), poll_events |
| `base.py` | 330 | 49 | God base class, heartbeat, tool tracking, dependency health, subscribe/poll_relay/emit_relay |
| `demigod.py` | 580 | 58 | Jailed state machine runtime, parse_pick (normalization, think tags, params), _template, call_llm via gateway, execute_action via Hades REST, cancellation, event callbacks |

### Infrastructure

| Item | Status |
|------|--------|
| Migration 027 (god_relay_events + god_registry) | Written, syntax checked |
| god_mcp.py (config-driven MCP server) | Built, tested |
| demigod_mcp.py (spawn/track/cancel runs) | Built, tested |
| demigods/god.json manifest | Written |
| ARCHITECTURE.md | Written |
| conftest.py (FakeDB + SqliteDB fixtures) | Working |

## What's NOT Built

### 1. Actual Handlers (CRITICAL — the framework meets reality)

These are the functions that go inside `pipeline.register()` and call real orchestration code:

| Handler | Event In | Event Out | Wraps |
|---------|----------|-----------|-------|
| `athena_plan` | project_created | plan_generated → (review) → project_planned | planner.py `PlannerService.generate()` |
| `athena_reassess` | wave_complete | continue / replan_required / escalate | planner.py `evaluate_wave_reassessment()` |
| `odin_start` | project_planned | project_started + dispatch_commands | executor.py branch setup, wave 0 selection |
| `odin_dispatch` | task_verified / wave events | dispatch_command | dispatch.py `select_provider()` + `compute_wave_parallelism()` |
| `odin_lifecycle` | worker_event + task_verified | wave_complete / project_complete / project_failed | executor.py wave check, deadlock detection |
| `hermes_execute` | dispatch_command | worker_event (started → completed/failed) | task_lifecycle.py `execute_task()` |
| `mimir_verify` | worker_event:completed | task_verified / task_rejected | verifier.py `verify_task_output()` |
| `mimir_review` | task_verified | review_passed / review_changes_requested | code_reviewer.py |
| `mimir_knowledge` | task_verified | knowledge_extracted | knowledge_extractor.py |
| `hephaestus_worktree` | project_started | worktree_ready | git_service.py worktree creation |
| `hephaestus_commit` | task_verified | files_committed | git_service.py stage + commit |
| `hephaestus_pr` | project_complete | pr_created | git_service.py PR creation |

### 2. TDD Execution Wrapper

The TDD gate exists (checks red/green/refactor) but nothing drives the actual flow:
- Write failing test → verify it fails → write implementation → verify it passes → refactor → verify still passes

This is logic inside `hermes_execute` that sequences the Claude Code CLI calls.

### 3. Narration → SSE Wiring

`pipeline.narrate()` fires a callback, but nobody connects it to `ProgressManager.push_event()`. Need a bridge in the orchestration backend lifespan.

### 4. Pipeline → Orchestration Wiring

The pipeline lives in `Odin/gods/` but the FastAPI app doesn't know about it. Need:
- Feature flag: `PIPELINE_MODE=gods|legacy`
- Lifespan hook that creates Pipeline + registers handlers
- REST route `POST /api/projects` emits `project_created` to relay table
- Config-driven handler registration

### 5. Cursor Persistence

`_last_seen_id` is in-memory. On crash, it's lost. Should persist to `god_registry` table every tick so restart recovery works without manual cursor injection.

### 6. split_task Repair Strategy

Diagnosis can recommend it. No handler implements it. Requires creating new task rows + dependency edges — needs planner/decomposer access.

## Implementation Order

Each handler is TDD: write failing test first, then implement.

### Phase 1: athena_plan (planning)
- Test: project_created event → plan in DB → project_planned emitted
- Test: review finds gaps → regenerate with feedback
- Test: gate verifies plan has tasks
- Impl: wrapper around PlannerService.generate() + review_plan()

### Phase 2: odin_dispatch (scheduling)
- Test: project_planned → dispatch_commands for wave 0 tasks
- Test: task_verified → wave complete detection
- Test: all waves done → project_complete
- Test: deadlock → project_failed
- Impl: wrapper around dispatch.py logic + wave SQL queries

### Phase 3: hermes_execute (execution)
- Test: dispatch_command → task claimed → CLI executor called → worker_event
- Test: TDD mode: test written → fails → implement → passes
- Test: narration events emitted during execution
- Impl: wrapper around execute_task() + TDD sequencing

### Phase 4: mimir_verify (verification)
- Test: worker_event:completed → verification runs → task_verified
- Test: gaps found → task_rejected with feedback
- Test: knowledge extracted on pass
- Impl: wrapper around verify_task_output() + knowledge_extractor

### Phase 5: hephaestus (git)
- Test: project_started → worktree created
- Test: task_verified → files committed
- Test: project_complete → PR created
- Impl: wrapper around git_service.py

### Phase 6: wiring
- Feature flag + lifespan hook
- REST routes emit to relay
- Narration → SSE bridge
- Cursor persistence
