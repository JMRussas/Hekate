# Plan 023 — Coding-worker adapter: ownership and a bounded first experiment (proposal)

**Status: design-only proposal, revised and frozen for codex-hekate review. No implementation GO;** a bounded E1 GO follows the final agreed H1 field checkpoint (msg 771). The ownership direction was approved by codex-hekate in msg 752 (on the preliminary verdict in msg 751); this document records it with sources, plus the experiment proposal. Inputs: codex-hekate msgs 735, 740, 741, 745, 747, 752, 756, 764, 769, 771; a read-only executor inventory (section 2.1); [CHATAGENT-INTEGRATION-HANDOFF.md](../../CHATAGENT-INTEGRATION-HANDOFF.md); [CHAT-CONSOLIDATION.md](../../CHAT-CONSOLIDATION.md); [015](015-execution-ledger-inventory.md); [018](018-claim-receipts-design.md); [019](019-durable-claims-and-pins.md).

**Labels:** **V** = verified by static source reading at HEAD `bb2af8b`, except explicitly identified untracked local material; **I** = inferred. Static reading does not establish deployed behavior.

## 1. Decision (approved direction)

| Concern | Owner | Writes allowed |
|---|---|---|
| Plan and task truth: readiness, claim receipts, pins, finish and release facts | **PlanStore** ([019](019-durable-claims-and-pins.md)) | All through its API, as below |
| Claim, finish (`transition → done`) and release (`transition → todo`) | **Hekate supervisor adapter**, the **only** writer of these, with durable keys it holds itself | `POST plans/{root}/claims`, `POST nodes/{id}/transition` |
| Claim correlation | Supervisor and ChatAgent | `GET plans/{root}/claims/{key}` is a **read** (key correlation, never a mutation) |
| Task and context assembly: the prepared work package and mandatory task text | **ChatAgent / ChatRuntime** (handoff "Direction and ownership"; peer H1) | **None.** A **pure** seam (no HTTP, no I/O) over a supervisor-supplied validated receipt envelope, its immutable claimed snapshots and explicit repository rules; no claim or transition client |
| Executing a prepared prompt | **Provider** (`CLIProvider`, later and gated, section 4) | None to PlanStore |
| Review decisions (`decide`) | **Independent verifier or lead** only | `POST nodes/{id}/decide` |
| Assignment, heartbeat, leases, recovery | **Separate later slice** (3b2, [018 §4](018-claim-receipts-design.md)) | not in scope |
| Launch journal (when real launches exist) | Supervisor, as **execution evidence only**; never a second plan or task truth | Its schema is an explicit follow-up, not part of the first experiment |

**Rules carried from the handoff:**
- One task ledger is authoritative.
- For file, command or git effects, checkpoint recovery alone never proves a repeat is safe.
- An uncertain effect requires reconciliation or idempotency before any retry ("Recommended bounded implementation sequence" §3).

**Launch ambiguity (msg 740):**
- A claim replay, or `stillCurrent=true`, is **never** permission to relaunch. A receipt is historical, and `stillCurrent` is factual correlation ([019 §4](019-durable-claims-and-pins.md)).
- An unknown launch or result state means **manual reconciliation**.
- There are no automatic retries, and nothing is released automatically while an effect is uncertain.

## 2. Source facts behind the decision

### 2.1 Existing launch paths and why none becomes the owner

| Path | Facts | Verdict |
|---|---|---|
| `Odin/gods/providers/base.py` `CLIProvider.execute(prompt, cwd, timeout, on_process, on_line)` (l.74-160) | **V:** A prepared-prompt boundary: `build_command(prompt, cwd)` → subprocess → `parse_output(lines)` → `StandardResult`. It is the provider layer Hermes actually calls (`hermes_async.py:28, 531, 608`), so CLAUDE.md's "built, not wired" note is stale (also recorded in CHAT-CONSOLIDATION). | **Reusable later**, behind the gates in section 4. The adapter owns outcome classification. |
| `Odin/gods/handlers/hermes_async.py` `_execute_cli` (l.520-618) | **V:** Wraps the provider with per-task worktree/add-dir overrides, relay narration events (fire-and-forget) and a process registry. It returns `output`, cost and narration **without `exit_code`** (l.613-621). It belongs to the gods dispatcher, `engine_tasks` and the relay lifecycle ([015 §1-3](015-execution-ledger-inventory.md)). | **Not reused** as the owner: its task table, relay and heartbeat lifecycle are a different ledger. |
| `orchestration/backend/services/claude_code_executor.py` `run_claude_code_task(task_row, db, budget, progress)` (l.39), `generic_cli_executor.py` `_run_cli_task` (l.98) | **V:** Builds the prompt internally from `task_row` via `cli_common.build_prompt_for_provider` (l.67 / l.115), resolves the cwd from the project, and **retries process crashes** (`CLI_CRASH_RETRIES=2`, `generic_cli_executor.py:29`). On timeout `_exec_process` kills the process and returns **exit code 0** with a message as stdout (l.211-219), so a timed-out run looks like success. | **Not reused** (legacy; prompt ownership and crash retries conflict with this design). |
| `task_lifecycle.py`, `context_bridge` | **V** ([013](013-plan-writer-inventory.md) B#2, [015 §5](015-execution-ledger-inventory.md)): replace-all attribute clobber; fenced for managed nodes since 2b1. | **Not reused.** |
| `Odin/langgraph_engine/` | **V:** untracked, SQLite only, unknown provenance ([015 §6](015-execution-ledger-inventory.md)). Its Hermes node runs `claude … --dangerously-skip-permissions` with `cwd=repo_path` and no worktree (`nodes/hermes.py:135-179`). Its README says "never executed" (`:231-235`). | **Not treated as verified, not enabled** (msg 741). |
| Gods lifecycle around Hermes | **V:** The start is read-then-write, not a compare-and-set (`hermes_async.py:141-143`). Concurrency and deferral are in memory (`:96-110`). The heartbeat writes `updated_at`, but nothing reads it (`STALENESS_THRESHOLD` is unused, `gods/odin/dispatch.py:38`). **Startup `recover_stuck_tasks` resets every `running` task to `pending` with `retry_count+1`, unconditionally** (`engine.py:539-557`): an automatic re-execution of possibly uncertain effects. Completion writes the relay event before the task row (`:359-371`). | The opposite of the "uncertain means manual" rule. **Not reused.** |
| Gods workspace and integration | **V:** `--worktree <slug>-<project[:8]>` is one worktree per **project**, not per attempt (`hermes_async.py:152-159`). Hephaestus checks out and commits in `repo_path` (`hephaestus.py:229-289`). The SHA appears only in an event; `git_commit_sha` is never written. **I:** edits in the CLI worktree and commits in `repo_path` can diverge. | Workspace ownership is an open follow-up, not reusable as is. |
| Monolith workspace | **V:** A per-project `.worktrees/<slug>`. **If creation fails it falls back to stash and checkout on the user's main tree** (`executor.py:946-953`, `git_service.py:314-321`), contradicting [LOCAL-PLANNING-HANDOFF.md](../../LOCAL-PLANNING-HANDOFF.md) l.124-125 ("never fall back to switching the user's main working tree"). | **Not reused.** |
| Monolith `/api/external` claim/result/release | **V:** The only other compare-and-set claim API (`routes/external.py:169-182`, `:219-338`), authenticated, but **without a lease**: stale sweeps skip claimed tasks, so a crashed claimant holds its task forever (`executor.py:305, 369`). It writes the monolith `tasks` ledger. | The shape is the closest analogue; the ledger is wrong. Superseded by PlanStore claims. |
| context-store `AgentDispatcher` | **V:** LISTEN on `node_changed` → it runs `claude --print` / `gemini` / `codex` with no cwd, worktree or timeout and returns 500 characters to the chat bus (`AgentDispatcher.cs:58-206`). It is answer-only and writes nothing. The plan-contract gate **requires it disabled** (`PlanContractGate.cs:69-70`). | Not a code worker. **Must stay disabled** on plan-contract hosts. |
| Monolith step trees, chat paths | **V:** A second provider abstraction, `orchestration/backend/services/cli_provider.py` (used only by `tree_runner.py:215`). The LLM gateway, `conversation.py` and the context-store Chat/Extraction services spawn answer-only CLIs (no cwd). **I:** there are at least four separate Claude command builders. | Not owners. Avoid adding another builder; reuse `gods/providers` behind the gates. |

**Requirements already recorded elsewhere** ([LOCAL-PLANNING-HANDOFF.md](../../LOCAL-PLANNING-HANDOFF.md) l.116-135):
- Assignments carry node, plan revision, role, workspace, base revision and prerequisite artifacts.
- **The context actually supplied is captured.**
- Workspaces are isolated, with a single integration owner and no main-tree fallback.
- ChatAgent's CLI bridge is answer-only: coding needs a separate adapter with explicit workspace and tool capabilities.

**Possible defect noted, not fixed here (I):** `hermes_async` applies `with_config(worktree=…, add_dirs=…)` to every provider, but `GeminiCLIConfig` has neither field (`gemini.py:31-66`), so a `gemini_cli` task would raise `TypeError` in `dataclasses.replace`.

### 2.2 Gates on provider reuse (record now; do not fix in this design; msgs 741 and 745)

1. **Outcome is not prose.** `CLIProvider.execute` returns a result with a non-zero `exit_code` when there is output (`base.py:155-159`). `StandardResult.has_output` (l.34) is not success. Hermes drops `exit_code` (section 2.1).
2. **Timeouts are not surfaced as a status.** Inactivity and overall timeouts kill the process and fall through to parsing (`base.py:126-146`). There is no `timed_out` field.
3. **Cleanup is not proven.** There is no `try/finally` around launch and read for cancellation, only `proc.kill()` of the direct child on timeout. Descendant cleanup (MCP servers, shells spawned by the CLI) is unproven. stderr goes to `DEVNULL` (l.98), so diagnostics are lost.
4. **Streaming is not completion.** `on_line` is fire-and-forget (`asyncio.ensure_future`, l.139); relay callbacks are not durable completion.
5. **Defaults are unsafe for a contract worker.** `ClaudeCodeProvider()` with no config sets `dangerously_skip_permissions=True`, `Bash(*)`, `max_turns=30`, no budget, and MCP tools including `agent-context__store_node` (`claude.py:115-163`). That is a write path outside the workspace and outside PlanStore. An adapter must pass an **explicit, reviewed configuration** and never inherit the defaults.
6. **Workspace effects.** Worktree isolation (`--worktree`, `hermes_async` overrides) and integration (merge or PR) need a single integration owner and a reconciliation step before any retry (handoff §3). This is out of scope here.

### 2.3 Digest identities (exact; msg 747)

**(a) PlanStore content digest:** `contentDigest` on receipts and events; `PlanContent.Digest()` with **no fields** (`PlanStore.cs:45`), computed by `PlanContentDigest.Compute(value, attrs)` (`PlanContentDigest.cs:24-43`).
- Canonical string: `Append(value)`, then `"attrs:" + count + ";"`, then for each attribute sorted by key with `StringComparer.Ordinal`: `Append(key)`, `Append(val)`.
- `Append(null) = "~;"` and `Append(s) = s.Length + ":" + s + ";"`.
- **`s.Length` counts UTF-16 code units** (C# `string.Length`): JavaScript `.length` matches, Python `len()` does not for astral characters.
- SHA-256 over the UTF-8 bytes, rendered as **uppercase** hex (`Convert.ToHexString`). There is no domain tag.
- Only the content keys are included: `description`, `acceptance_criteria`, `verification_criteria`, `success_criteria`, `scope`, `affected_files`, `requirement_ids` (l.18-21).
- Null and empty are distinct, and an absent key and a present key are distinct.

**(b) Prerequisite digest:** `prereqDigest` / `attemptPrereqDigest`. Domain tag `hekate-prereq/v1`, a different length-prefixed encoder over the canonical prerequisite snapshot, **lowercase** hex ([019 §3](019-durable-claims-and-pins.md)).

**(c) Any ChatAgent `AttachedReference` text hash** is a hash of raw source text bytes. It must keep its own label (for example `sourceTextSha256`) and must never be called `contentDigest`, compared with (a), or substituted for it.


## 3. First experiment (E1): fake-worker contract seam — no process launch

**Question it answers:** Can a supervisor drive one managed leaf end to end against the **real** plan-contract API, with every write behind explicit preconditions, using an immutable work package derived only from the claim receipt? And does every uncertain or non-success case stop for a human instead of writing?

**Excluded by construction (msg 752):** no subprocess, CLI, model or provider call; no new service or runtime; no auth change; no launch-journal schema; no worktree or git effects; no Odin, Hermes or legacy-runner changes. E1 itself changes no ChatAgent code. The ChatAgent side is real work in its own repository (peer **H1**, below), and E1 consumes its output.

### 3.1 Pieces (test harness only)

**Envelope (supervisor-owned I/O; msg 764 (2)).**
- The supervisor alone calls `POST claims` and `GET claims/{key}` over HTTP.
- It reads the raw body with exact integers (Python `int`) and validates its shape.
- It hands the context seam a **validated receipt envelope**: the raw bytes plus the parsed receipt.

**Pure context seam (ChatAgent, peer H1; msgs 764 (5) and 769).**
- `build_package(envelope, rules, budget) → WorkPackage | ContextBudgetError` is a **pure** function: no HTTP, no I/O, no clock. `rules` is an **explicit, versioned repository-rules input**; the receipt carries none (msg 771).
- ChatAgent's H1 implements the real receipt-to-mandatory-context seam in the ChatAgent repository.
- E1 consumes **shared representative fixtures now**: the real raw claim wire fixtures from `bb2af8b`, preserved by codex-hekate with a provenance README under [fixtures/023/](fixtures/023/) (`claim-claimed.json`, `claim-replayed.json`, `claim-no-ready-work.json`). It consumes **H1's actual package output later**.
- Until that interop exists, a stub stands in for H1 and is labelled as a stub. E1 does not grow an independent context interpreter.
- Field names finalize at H1's implementation checkpoint.
- H1's `ContextBudgetError` code `CONTEXT_TOO_LARGE` is mapped explicitly to the supervisor outcome `package_overflow`.

**Work package: semantic identity (msg 764 (4)).** The package contains:
- the receipt identities **verbatim**: `rootId`, `claimKey`, `nodeId`, `attemptId`, `attemptEpoch`, `executorRef`, `eventSeq`, `contentRevision`, `contentDigest`, `prereqDigest`;
- `contentSnapshot` and `prereqSnapshot`;
- the rules and budget versions;
- the rendered mandatory task text;
- `suppliedTaskTextSha256`, defined in the next block.

The derived identities:
- `packageDigest` = SHA-256 over a versioned, domain-separated canonical encoding of exactly those semantic fields, including the mandatory rendered text and its hash. The encoding and package schema must be fixed at the H1 field checkpoint before E1 implementation; this proposal does not supply an interchangeable digest algorithm. It **excludes** any randomly generated identifier, such as an H1 `buildContext` snapshot id, and the `runId` (msg 771).
- `packageId` is caller-held and deterministic: it is the `packageDigest`, so it is content-addressed.
- With identical rules, budget versions and rendering inputs, a replayed claim key gives identical semantic package bytes, `packageDigest` and `packageId`. A changed rules or rendering version gives a different package, even if the historical claim receipt is unchanged.

**Run envelope (separate, not digested).**
- It holds `{runId, packageId, startedAt}`, where `runId` is a fresh local id for each hand-off.
- `runId` and `packageId` are never written to PlanStore as attempt identity (msg 756).

**What `suppliedTaskTextSha256` covers (msg 769).**
- It is the SHA-256 of the exact **mandatory task-text** UTF-8 bytes produced by H1.
- It is **not** a whole-prompt digest. System and role instructions are budgeted separately and are not covered.
- It is distinct from `contentDigest` (PlanStore canonical) and from any `AttachedReference` raw-text hash.

**Budget rule.**
- Mandatory rules and the node's requirements are included whole, or the package is refused: `CONTEXT_TOO_LARGE` → `package_overflow`.
- There is no silent truncation. Optional context trimming is recorded.

**Fake worker (in-process, scripted).** `WorkResult` must carry **every** correlation field (msg 764 (3)):
- `{runId, packageId, packageDigest, rootId, nodeId, claimKey, attemptId, attemptEpoch, executorRef, contentDigest, prereqDigest, suppliedTaskTextSha256}`;
- plus the outcome fields `{exitCode, timedOut, killed, structuredResult?, artifactRef?, prose}`.

Every correlation field must equal the package or run value. Any mismatch is a typed `result_correlation_mismatch`.

**Supervisor (the only writer; msg 764 (1)).**
1. `POST claims` with the caller-held `claimKey` and an `executorRef` the caller holds as opaque correlation. The claim is immutable: a replay never mints a fresh reference.
2. **Dispatch only on a fresh claim** (`replayed == false`) within the same supervisor session. A replayed receipt is never permission to dispatch, whatever `stillCurrent` says.
3. **Classify:** success only if `exitCode == 0`, `!timedOut`, `!killed`, a valid `structuredResult` is present, and the full correlation matches. Prose is never consulted.
4. **Precondition reads:**
   - `GET claims/{key}` must give `stillCurrent=true`, and the receipt must equal the package identities.
   - Read the node's **current** `stateRevision` (the plan view) **for the compare-and-set only**.
   - Content and context come **only** from the receipt snapshots, never from the live node.
5. **Finish:** `POST nodes/{id}/transition` with:
   - `{to: "done", attemptId, attemptEpoch, artifactRef, executorRef}`;
   - `expectedStateRevision`: the current revision read in step 4;
   - `operationKey: "supervisor:<claimKey>:finish"`;
   - `actor`: the supervisor's actor.

   Hold the original payload and expected revision for an immediate replay; do not re-read a revision and rebuild that payload. The store recognizes the last operation key on a node; it is not a permanent receipt for every transition key. After an intervening mutation, reconcile manually instead of assuming this key can replay successfully (PlanRules.Prologue).
6. **PlanStore is the final authority against time-of-check/time-of-use races.** Its own pin, attempt and revision checks reject a finish that went stale after the pre-check (`stale_prerequisites`, `stale_content`, `stale_revision`). The supervisor then records needs-operator; it never retries or loops.
7. **Release:** only on explicit operator instruction, with `operationKey: "supervisor:<claimKey>:release"`, the same compare-and-set rule, and the attempt id and epoch from the package.

**Operator and verifier.** The test plays the operator (explicit release with a reason) and the independent verifier (`decide`). The supervisor never calls `decide`.

**Uncertainty (msg 764 (7)).**
- E1 has no launch journal, so it makes **no claim of restart durability**.
- The crash case is modelled explicitly. A new supervisor instance meets an existing claim only as a **replayed** receipt, and has no run evidence.
- By the dispatch rule (step 2) it refuses to dispatch or finish and reports `uncertain → needs-operator`. When given an explicit operator-state input of `uncertain` for that claim (standing in for a future E2 journal), it does the same.
- This tests the **policy**, not durability.

**Budget overflow (msg 764 (6)).** `package_overflow` means no package, no dispatch, no finish and no release. The only effect is the claim that already exists, which stays InProgress for the operator.

### 3.2 Cases (each asserts the PlanStore rows, events and receipts written or not written)

1. **Happy path:** fresh claim → package → success → reads → finish Applied, with a compare-and-set on the current revision. Events carry the claim key and pins. `decide` is a separate verifier actor.
2. **Upstream drift before the pre-check:** `stillCurrent=false` → no finish, needs-operator.
3. **Own content revised:** `stillCurrent=false` → no finish (a forced finish is 409 `stale_content`).
4. **Correlation mismatch, table-driven:** each correlation field in turn is wrong in the `WorkResult` → typed rejection, nothing written.
5. **Non-success shapes:** non-zero exit with prose, timed out, killed, or missing structured result → no finish, needs-operator, no automatic release.
6. **Replayed claim:** `replayed=true` with `stillCurrent=true` → no dispatch and no finish, needs-operator. The explicit operator release then applies with the release key.
7. **Deterministic identity:** a replay gives an identical `packageDigest` and `packageId`; two hand-offs give different `runId`s, which are absent from the digest.
8. **Pure seam:** `build_package` performs no I/O (structural check: no network, file or clock access; same input gives same bytes). It has no claim or transition capability.
9. **Digest labels:** `contentDigest` uppercase and `prereqDigest` lowercase, verbatim from the receipt. `suppliedTaskTextSha256` covers only the mandatory task text. A mislabelled or recomputed digest fails.
10. **Exact counters:** epochs, revisions and `eventSeq` stay exact integers end to end.
11. **Pinned-content mismatch:** a snapshot or digest in the package that disagrees with the receipt, or a result echoing a different digest, is rejected before any write.
12. **Overflow:** `CONTEXT_TOO_LARGE` → `package_overflow`. Nothing is written beyond the existing claim.
13. **Race:** the test changes an upstream node **between** the supervisor's pre-check and its finish. PlanStore rejects with 409 `stale_prerequisites`; the supervisor records needs-operator and does not retry.
14. **Compare-and-set:** a stale `expectedStateRevision` gives 409 `stale_revision`, with no retry loop.
15. **Idempotent writes:** an immediate repeat of the last finish key and held payload gives `Unchanged`; the same key with a different payload gives 409 `operation_key_reused`.
16. **Fixture interop:**
    - Now: the real claim wire fixtures parse into the envelope exactly, and the `claim-no-ready-work` fixture yields no package.
    - Later: H1's package output for those fixtures passes the supervisor and worker correlation checks.

    Interop with H1's output is **pending** until H1's checkpoint and is not counted as passing before then.

### 3.3 Proposed shape

- **Python with uv**, codex-hekate's preference: a test module against the real Api on a **new disposable database**, with the same isolation as `scripts/local/PlanContractApi.Tests.ps1` (owned `hekate-local` container, loopback only, database dropped and verified).
- Inputs: the real claim fixtures listed above for envelope parsing, and the live database for the API flow.
- Expected size: one module plus fixtures. No production code changes.

### 3.3a E1 split: E1a now, E1b after H1 (codex-hekate GO msg 778)

**E1a (GO msg 778; implemented, evidence in [024](024-supervisor-e1a-validation.md))** covers the supervisor and API preconditions with an explicitly **opaque** work-package token:
- the token is `e1-opaque:<claimKey>` plus the receipt identities verbatim;
- **no** context renderer, **no** package canonicalization and **no** `packageDigest` or `suppliedTaskTextSha256`.

In scope:
- whole-document validation of the real raw claim fixtures;
- cases 1–15 with the fake worker on the real API, with runtime type validation of results;
- fresh-and-current-only dispatch;
- compare-and-set, races and last-operation idempotency, where the held finish is re-sent only while it is provably the node's last operation;
- explicit operator release.

Location: `scripts/local/supervisor_e1/` (Python with uv), test-only.

**E1b (pending ChatAgent H1's accepted checkpoint; no GO):**
- H1's package fields;
- the mandatory task-text rendering and `suppliedTaskTextSha256`;
- the versioned, domain-separated `packageDigest` schema;
- case 16 interop on H1's actual output.

E1b replaces the opaque token with H1's package and must not freeze H1 text bytes until H1's commit is accepted (msg 797).

### 3.4 Exit criteria and what follows

- **E1a** passes when cases 1–15 pass with database-verified write and no-write assertions, and case 16's fixture-parsing part passes. **E1b** (H1 interop, rendering and digest) is a separate checkpoint after H1 acceptance.
- Each follow-up needs its own proposal and GO:
  - **(E2)** the launch-journal schema, as execution evidence only: intent before spawn; exit with `timedOut` and `killed`; result recorded; feeds the `uncertain` input.
  - **(E3)** a real but harmless process: a scripted child that spawns a grandchild, to prove owned-process and descendant cleanup and cancellation on Windows.
  - Only then, provider configuration review and live coding activation under the section 2.2 gates.
  - Leases and heartbeats (3b2) independently.

## 4. Not included

- Implementation of any of the above.
- Worker activation.
- The launch-journal schema.
- Worktree and integration ownership.
- Leases, heartbeats and recovery.
- Auth.
- Odin, Hermes or legacy-runner changes.
- Enabling `langgraph_engine`.

H1 (ChatAgent's pure context seam) is ChatAgent's own increment, not part of this Hekate proposal.

## 5. Decisions and remaining questions

**Decided (msg 764):**
- Python with uv.
- Operation keys `supervisor:<claimKey>:finish` and `supervisor:<claimKey>:release`, each with a stable payload. They are distinct from the reserved `hekate-claim:` prefix.
- `executorRef` is caller-held and opaque, fixed at claim time and never regenerated on replay.

**Remaining:**
1. `packageId = packageDigest` is accepted, once the canonical semantic package schema/version is fixed.
2. The accepted H1 field checkpoint and its output fixture format remain pending for case 16 interop and any E1 implementation GO.

## E1a acceptance checkpoint

E1a's test-only supervisor/API boundary is accepted by codex-hekate after an independent locked-dependency run: 103 tests passed (57 offline, 46 live). See [024](024-supervisor-e1a-validation.md). E1b remains a separate bounded interoperability check against the accepted ChatAgent H1 commit; no coding provider is activated.
