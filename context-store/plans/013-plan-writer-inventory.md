# Plan 013 — Legacy plan-node writer inventory

**Status: legacy writer inventory, 2026-10-06; acceptance milestones below are current. Remaining integration requires a separate bounded implementation decision.**
**Purpose:** before plan contracts v1 ([012](012-plan-node-contracts-v1.md)) are wired to a store, list every path that can change plan nodes. Each writer is classified so content and state invariants cannot be bypassed through a generic writer.

## Contract acceptance status

| Increment | Status |
|---|---|
| 1 — local plan-only launcher (`scripts/local/`) | Live startup, restart persistence, backup/AGE restore, ownership and shutdown verified with Docker Desktop. See [`scripts/local/VALIDATION.md`](../../scripts/local/VALIDATION.md) for evidence and exclusions. |
| 2a — pure plan contracts (`PlanContracts/`) | **Accepted** by codex-hekate, pure scope only. |
| 2b1 — PostgreSQL store + opt-in loopback API + write fence | **Accepted by codex-hekate after independent final runs.** It covers **new** managed plans only. The fence blocks the generic writers below for managed nodes (409 `managed_plan_protected`). See [012](012-plan-node-contracts-v1.md) and the evidence in [014](014-plan-contract-integration-validation.md). |
| 3a — attempt and review provenance | **Accepted** after independent 146 pure / 34 live / 39 HTTP checks. Append-only audit, no execution adapter or worker claims. See [016](016-attempt-provenance-audit.md) and [017](017-attempt-provenance-validation.md). |
| 3b1 — durable claim receipts + attempt pins | **Accepted by codex-hekate after independent verification.** Independent root runs passed 172 pure / 48 live / 59 HTTP. Receipts and `stillCurrent` are factual correlation, not authority. No leases (3b2), no execution adapter, no worker activation. See [019](019-durable-claims-and-pins.md) and [020](020-durable-claims-validation.md). |
| 2b, remaining | Legacy-plan enrollment, the execution-ledger link, and the fate of each writer (section D) are **not started and not approved**. |

## Method and labels

The inventory comes from static source reading on branch `feat/plan-nodes-migration`; nothing was run.

- **V** — verified by reading the cited code.
- **V★** — re-checked independently a second time.
- **I** — inferred runtime behaviour, not executed.

Paths are relative to the repository root.

Plan-structure node types: `plan`, `plan_phase`, `plan_step`, `task`, `milestone`. Plans also carry child types such as `revision`, `risk`, `blocker` and `test_spec`.

## A. Repository-level primitives (`context-store/DbLayer/NodeRepository.cs`)

| Method | Scope (V) | Callers (V) | Classification |
|---|---|---|---|
| `InsertNode` (:90) | insert node + attributes; outbox vertex | API #4, seeders, CLI demo | STRUCTURE / CONTENT |
| `SetAttribute` (:160) | upsert one key | CLI demo `Program.cs:618` (`body`). `PermissionService` has its **own** private `SetAttribute` on conversation nodes (`PermissionService.cs:110,116,155`). | Depends on the key: see section D |
| `DeleteAttribute` (:175) | delete one key | **none** | dormant; would be a STATE/CONTENT bypass if exposed |
| `UpdateNode` (:305) | name/value with null-means-keep defaults, `modified_by` | CLI demo `Program.cs:542` only | CONTENT + name |
| `MutateAsync` (:336, public static) | **only** `name` and/or `value` (flag-selected), plus `modified_at = now()` and `modified_by`; runs in the caller's connection and transaction; not arbitrary columns | **none** | CONTENT + name; a bypass if wired up without the contract service |
| `DeleteSubtree` (:355) | recursive delete | **none** | STRUCTURE; dormant |
| `NextSiblingOrder` (:379-404) | `MAX(...) FOR UPDATE` | CLI and seed paths | Its row lock runs **without a transaction**, so it does nothing (V) |

## B. HTTP and service writers

| # | Writer → callers | Location | Mutation | Category | Plan-capable / identification | Gaps |
|---|---|---|---|---|---|---|
| 1 | `PUT /api/node/{id}` → `NodeService.UpdateNodeFull`. Callers: UI `NodeDetailPanel.tsx:125`; Odin `context_bridge._update_node` (:105-113, used :235) | `NodeService.cs:211-228`, `Api/Program.cs:240` | sets name **and** value; an omitted field → NULL | CONTENT + name | yes, any id, no type check | **V★:** Odin sends only `value=output_text`, so the task **name is nulled** and the requirement text is replaced by executor output. No compare-and-set. Not in one transaction with #2. AGE vertex name goes stale. `modified_by` is hard-coded `'user'`. |
| 2 | `PUT /api/node/{id}/attributes` → `NodeService.UpdateAttributes`. Callers: UI :129; Odin `_update_attrs` (:236 task, :305 plan); orchestration `context_store_client.update_attributes` (:207-214) via `task_lifecycle._sync_status_to_context_store` (:55-86, 14 call sites), `telemetry_feedback.py:111`, `sentinel/context_client.py:185` | `NodeService.cs:231-267`, `Program.cs:249` | DELETE all attributes, then re-INSERT, in one transaction | STATE + CONTENT + CORRELATION mixed | yes, by id | Replace-all clobber. **V:** Odin's completion push drops `engine_task_id` / `engine_project_id`, breaking its own later lookup. The UI writes back a snapshot, so updates can be lost. **V★:** orchestration sends a flat dict (`json=attributes`) to an endpoint that binds `UpdateAttrsRequest(Dictionary Attributes)`; **I:** `Attributes` is null, giving an error/500, so the orchestration status push is broken today. No `modified_at` bump and no NOTIFY. |
| 3 | `POST /api/node/{parent}/children` → `NodeService.CreateChildNode` + `SyncVertex`. Callers: UI :145; `orchestration/backend/services/plan_sync.py` (phase, task, epic, question, risk, revision, observation, finding); Odin tasks/findings; `ares.py:556`; `seed_hekate_roadmap.py:64` | `NodeService.cs:269-339`, `Program.cs:253-259` | create node + attributes | STRUCTURE / CONTENT | any `node_type`; **no plan structural validation** (parent type, project, depth) | Unlocked `MAX(sibling_order)+100` under READ COMMITTED, so duplicate orders are possible. The outbox row is written **after commit** on another connection, with a direct-Cypher fallback. |
| 4 | `POST /api/project/{pid}/nodes` → `repo.InsertNode` (parent null, order 0). Callers: `plan_sync.py:90`, Odin `:147`, seed scripts | `Program.cs:262-269` | create root node | STRUCTURE | creates `plan` roots | Not idempotent: every `sync_plan` / replan (`task_lifecycle.py:2408`) makes a **new** tree, and Odin `context_bridge_plan` makes a **second** tree for the same project. Outbox written outside the transaction. |
| 5 | `DELETE /api/project/{pid}/nodes?provenance=` | `NodeRepository.cs:770-785`, `Program.cs:455` | bulk delete by `modified_by` | STRUCTURE | yes: every #3 node has `modified_by='user'` | No subtree handling: `parent_id` has no cascade, so the delete fails or leaves orphans. No AGE cleanup, no type filter. |
| 6 | `POST /api/code/edge` → `AgeLayer.CreateEdge` (outbox) / `CreateTemporalEdge` (direct Cypher) | `AgeLayer.cs:158-185, 360-373`, `Program.cs:465` | create edge | STRUCTURE | any ids; `DEPENDS_ON`, `BLOCKS` and `IMPLEMENTED_BY` are whitelisted | CREATE, not MERGE, so duplicates are possible. No endpoint-existence or cycle check. The temporal path bypasses the outbox. |
| 6b | orchestration `create_edge` → `POST /api/graph/edges` | `context_store_client.py:418-423` | — | STRUCTURE | — | **V★: the route does not exist (404)**, and the payload keys don't match `CreateEdgeRequest`. So `plan_sync.py:361-380` revision `CONSTRAINS` / `INFORMS` edges are never created. |
| 7 | Outbox drain | `AgeLayer.cs:217-288`, timer `Program.cs:174` | applies vertex MERGE and edge CREATE | STRUCTURE | yes | An edge MATCH against a missing vertex matches 0 rows but is still marked synced (silent loss). Plain SELECT without row locks, so multiple instances can double-apply. A retry after partial success duplicates edges. |
| 8 | `/api/command/park\|resume` + chat direct action → `ChatService.SetIdeaStatus` | `ChatService.cs:1097-1128, 1216-1229` | upsert single `status` attribute | STATE | any id, no type check (can park a plan or task) | No `modified_at` bump, no NOTIFY. |
| 9 | `/api/brain/extract` → `ExtractionService.StoreExtractedNode` | `ExtractionService.cs:192-245` | node + attributes + `EXTRACTED` / `RELATES_TO` edges | CONTENT | **I:** the LLM-chosen type is unvalidated, so it could create plan types | Not transactional. |
| 10 | `SubtreeLock.TryClaimSubtree` (CLI `Program.cs:511`) | `SubtreeLock.cs:92-96` | `UPDATE modified_by` | provenance | by id | Overwrites the provenance that #5 deletes by. |
| 11 | CLI demo `context-store/Program.cs` | :77-83, :524-634 | unscoped wipe of all nodes/attributes/projects, then seeders rebuild plan trees | all | yes | Unscoped; AGE goes stale; ineffective sibling lock (section A). |
| 12 | `PlanToCodeGenerator.RegenerateFromPlan` | `PlanToCodeGenerator.cs:93-114` | `CloseAllEdgesFrom(task, IMPLEMENTED_BY)` via direct Cypher, plus code-node deletes | STRUCTURE | task nodes as edge sources | Not transactional with the relational writes. |
| 13 | agent-context MCP `store_node` | `tools/agent-context-mcp/server.py:193-289` | raw SQL insert of any `node_type` + attribute upsert | CONTENT / STRUCTURE | yes (`plan` / `task` allowed) | No AGE/outbox. The parent's project is unchecked, so cross-project parents are possible. Unlocked MAX order. |
| 14 | utils-mcp `store_turn`, `query_db.py store` | `utils-mcp/server.py:148-224`, `query_db.py:136` | turn rows | — | low | Unlocked order. |
| 15 | Trigger `notify_node_changed` | `DbLayer/Schema.cs:125-138` | NOTIFY on `nodes` INSERT/UPDATE | — | — | Not fired for `node_attributes` or DELETE. |

**Not present (V):** no API for reparent, reorder, single-node delete or edge delete. `ContextRouter`, `PlanContracts` and `AgentDispatcher` never write nodes.

## C. How writers identify plan nodes

- **`plan_sync.py`:** a title → node_id map in `plans.node_mapping_json` (:100-262). Duplicate task titles collide.
- **`context_bridge.py`:** scans **all** `/api/plans` trees for `engine_task_id` / `engine_project_id`, with no project scoping (:221-242, 293-310).
- **Everyone else:** a direct id.

## D. Field classification (proposed, needs a decision before 2b)

| Class | Fields | Required treatment under the contract |
|---|---|---|
| CONTENT (increment `ContentRevision`) | `nodes.value`; attributes `verification_criteria`, `success_criteria`, `scope`, `affected_files`, `requirement_ids`; possibly `rationale`, `alternatives_considered` | only through a contract operation that increments ContentRevision |
| STATE | `status`, `verification_status`, `error`, `cost_usd`, `completed_at`, `updated_at` | move to `plan_node_state`; never in the attribute bag |
| CORRELATION | `engine_task_id`, `engine_project_id`, `engine_plan_id`, `orch_plan_id`, `orch_project_id` | move to an execution-link table keyed by node id; never clobberable |
| PRESENTATION | `name`, `sibling_order`, `priority`, `target_date`, `plan_type`; possibly `confidence` | free to edit; no revision effect |

### Bypass classification: what must not stay a generic path for plan types

| Writer | Risk to invariants | Proposed fate (decision needed) |
|---|---|---|
| #1 `UpdateNodeFull`, `UpdateNode`, `MutateAsync` | rewrite CONTENT without incrementing ContentRevision | reject plan types, or require the expected content revision and route through the contract service |
| #2 `UpdateAttributes` | replace-all wipes STATE, CONTENT and CORRELATION together | reject plan types; per-key upsert only, for presentation keys |
| `SetAttribute` / `DeleteAttribute` / #8 `SetIdeaStatus` | single-key STATE/CONTENT writes that skip the contract | restrict to non-plan types, or to presentation keys |
| #3 `CreateChildNode`, #13 `store_node`, #9 `StoreExtractedNode` | create plan structure with no parent/type/project validation | plan types only through a contract "create child" operation |
| #4 root creation (plan_sync, Odin) | duplicate plan trees | a single creator with an idempotency key per (project, source plan id) |
| #5 provenance delete, #11 CLI wipe, `DeleteSubtree` | unscoped, cascade-less deletes, no AGE cleanup | exclude plan types, or add an explicit contract delete/descope operation |
| #6 / #6b / #7 AGE edges | non-transactional, lossy, no cycle check | never authoritative for dependencies; the projection is written in the contract transaction (see 012) |
| `plan_sync.py`, `context_bridge.py` | title and scan identification; clobbering pushes | candidates for retirement or a rewrite against the contract API |

## E. Most dangerous gaps for a revisioned store

1. #1 and #2 are generic, type-blind and precondition-free. Null-on-omit plus replace-all wipe content, state and correlation ids together, and the Odin path does this today.
2. Content and state share one attribute bag and one endpoint, so a status push looks identical to a requirements edit.
3. Three independent plan-tree creators (plan_sync, Odin, MCP `store_node`) have no idempotency key.
4. Orchestration write paths are already broken: the status-push payload shape is wrong and the edge route is missing. Fixing them naively would switch replace-all clobbering on at scale.
5. AGE edges are not transactional with the relational store, so `DEPENDS_ON` / `BLOCKS` must not be authoritative there.
6. Deletes do no AGE cleanup and have no subtree handling. `SubtreeLock` rewrites the `modified_by` that provenance deletes depend on.
7. There is no reliable change signal: attribute writes skip `modified_at` and NOTIFY, and sibling ordering is racy everywhere.

## Open decisions (not approved)

- (a) Block plan types on the generic writers versus require preconditions there.
- (b) The final content/state/presentation split for the "possibly" fields.
- (c) The fate of each legacy writer: migrate, restrict or retire.
- (d) Which execution ledger is authoritative (see 012 §Integration requirements).
- (e) A live-database verification plan before any 2b claim.
