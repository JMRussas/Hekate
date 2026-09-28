# ChatAgent → Hekate integration handoff

Updated 2026-09-28. This is an implementation handoff, not a completed migration.
Source repository: https://github.com/JMRussas/ChatRuntime (local folder ChatAgent).
Reviewed source checkpoint: `4eb88fd14b80435040a08608ad85387ae19a3a47`.
Read source files/reports at that revision before adapting their contracts.

## Direction and ownership

Keep the responsive conversation surface separate from long-running objective and
specialist execution. ChatRuntime currently owns conversational context, streaming
and generation lifecycle. Hekate's gods provide domain planning, dispatch,
execution, verification and budget policy. Hekate's context-store remains a
candidate durable backend, not an adapter already delivered by ChatAgent.

The ChatAgent work is a standalone learning/building project. Reuse its proven
mechanisms where useful; do not migrate every experiment into Hekate or force the
chat path through the gods without an explicit integration decision. Preserve
Hekate's current in-progress code. Its `Odin/langgraph_engine/` already contains
project/task graphs, approval routing, parallel dispatch, verification and a
checkpoint/resume runner. Reconcile this implementation rather than creating a
second Hekate graph engine. Near-term shipment is user intent, not validation of
that engine's deployment or failure behavior.

## Implemented source material to incorporate

Paths below are relative to the ChatRuntime repository, not this Hekate repository.

| Concern | Source | Reuse / integration requirement |
| --- | --- | --- |
| Layered context | `src/app/contextManager.ts`, `src/app/contextBuilder.ts`, `docs/implementation/01b-evidence.md` | Immutable queued context; bounded selection; asynchronous summaries; retained original transcript and source attribution. Foreground does not await a new summary. |
| Conversation protocol | `src/app/protocolV1.ts`, `docs/implementation/07-iris-slice-evidence.md` | Preserve conversation/turn identity, ordered events, reconnect behavior, cancellation and terminal answer outcomes. This protocol slice exists; the old “first Iris turn” task is historical. |
| LangChain retrieval worker | `experiments/doc-agent/agent.py`, `retrieval.py` | Schema-validated tools, bounded calls/bytes/deadlines, pinned source revisions and host-issued evidence IDs. These are documentation lookup tools, not a replacement for Hekate's semantic retrieval. |
| LangGraph state | `experiments/doc-agent/graph_agent.py` | Model/validate/retrieve nodes with explicit routes; independent state and evidence per invocation. Adapt invariants to Hekate's richer graph. |
| Durable boundaries | `experiments/doc-agent/durable.py`, `DURABILITY.md` | One task checkpoint database, configuration/source identity checks, exclusive ownership and resume from confirmed pauses. Uncertain in-flight execution must not be replayed automatically. |
| Task lifecycle | `experiments/doc-agent/task_manager.py`, `TASKS.md` | Independent task IDs, status/list, resume and persisted cancellation. Managed execution budget excludes paused time; the standalone durable CLI has a different wall-clock policy. |
| Chat integration | `src/app/documentTasks.ts`, `experiments/doc-agent/chat_bridge.py`, `docs/implementation/11-conversation-tasks.md` | Optional Node/Python bridge, stable start-request identity, conversation-scoped task bindings, bounded admission and separate results panel. |
| Evaluation records | `experiments/doc-agent/plan_experiment.py`, `PLAN-EVALUATION.md` | Keep proposed actions, actual tool results and final answer separate. Plan/schema validity is not semantic correctness; this planning experiment is not a production graph node. |

Some older experiment READMEs still describe integration as future work. For the
current bridge contract, prefer implementation guide 11 and the source at the
pinned checkpoint over those historical paragraphs.

## Existing task API: preserve before extending

`POST /document-tasks`: `op` = start/list/status/resume/cancel, with conversationId
and userId. Start requires UUID requestId and question; status/resume/cancel require
taskId. Successful start/resume return 202; other operations return 200. Invalid
input returns 400, missing task 404, ownership/request conflict 409, capacity 429,
and bridge failure 503. A retry of the same request with the same scope/question
returns the existing task; conflicting reuse is rejected.

Current states include queued, running, paused, completed, failed, cancelled and
uncertain. Inspect source transitions before mapping these to Hekate lifecycle
states. Keep objective, conversation, message, task, execution and attempt IDs
separate; do not repurpose the protocol's deep-answer task ID as a durable objective.

The current prototype uses client-supplied user identity guards, not authenticated
multi-tenant authorization. Hekate integration must derive identity from its chosen
authentication boundary and enforce project/conversation ownership on operations
and evidence access. Do not turn the local sidecar into a remotely accessible
service without resolving that boundary.

## Recommended bounded implementation sequence

1. **Record the adapter decision.** Trace active Hekate routes, gods and gateway;
   choose whether ChatRuntime calls a Hekate task API or Hekate calls ChatRuntime.
   Name the owner of task truth, model admission and durable conversation state.
   Keep one task ledger authoritative and define status/error translation.
2. **Connect one read-only documentation objective.** Reuse Hekate's existing graph
   with an adapter or use the current worker behind the task contract. Acknowledge
   promptly; keep conversation Send independent; deliver a source-linked result.
   Feature-gate the path and leave existing clients working.
3. **Apply lifecycle invariants before general writes.** Test duplicate acceptance,
   cancellation and crash boundaries. Hekate's graph README describes best-effort
   projection writes and opaque subgraph checkpoint boundaries; review those
   explicitly. For file changes, commands or git operations, checkpoint recovery
   alone does not prove an external effect is safe to repeat. Require reconciliation
   or operation idempotency before retrying an uncertain write.
4. **Connect context deliberately.** Define source identity, revision, retention,
   invalidation and authorized lookup. Decide how a completed task becomes eligible
   for foreground context. Do not inject all task traces or summaries into every
   turn. Retain provenance and distinguish proposals, claims and verified outcomes.
5. **Measure shared inference contention.** Add first-token latency and admission
   wait telemetry before choosing foreground priority, background concurrency or
   model placement. Current documentation admission is eight jobs/one executing;
   it does not govern all foreground/gateway inference.
6. **Ship after integration evidence.** Record the exact engine and revision,
   run the matrix below and confirm deployment separately. Keep timed triggers,
   general goal revision and learned policy changes as subsequent increments.

## Acceptance matrix to carry forward

- Two conversations and multiple tasks: no state, source or cancellation leakage.
- Duplicate start, conflicting reuse and uncertain HTTP response: no duplicate work.
- Queued cancellation frees capacity; running cancellation persists; late model
  success cannot overwrite a terminal state. Remote GPU release is not implied.
- Confirmed pause survives restart with pinned sources and configuration. Changed
  revisions/configuration and uncertain in-flight ownership fail closed.
- Kill a worker during inference and around persistence; simulate a failed registry
  write and stale status read. Do not report successful durable completion from a
  failed write merely because the model returned an answer.
- Enforce model/tool/input/retrieval/deadline budgets across resumes and retries;
  distinguish paused time from active time explicitly.
- Browser: submit, keep chatting, cancel, see source-linked completion and change
  scope. Add browser restart/resume and reconnect tests; those remain untested in
  the ChatAgent browser evidence.
- Review answers against exact cited passages. A retrieved source ID alone cannot
  prove support; limited keyword search cannot establish global absence.

Port relevant fixtures from `test_lifecycle_gaps.py`, `test_durable.py`,
`test_task_manager.py`, `test_chat_bridge.py` and the TypeScript document-task
integration tests. Adapt them to Hekate's contracts rather than treating another
repository's passing tests as proof of this integration.

## Evidence and lessons, not promised performance

All report paths are in ChatRuntime:

- `reports/doc-agent/lifecycle-edge-cases-2026-09-27.md`: reproduced persistence,
  cancellation and worker-boundary failures and fixes.
- `reports/doc-agent/chat-findings-2026-09-27.md`: actual Node/Python/Ollama bridge,
  duplicate identity, owner rejection, independent cancellation and sourced answer.
- `reports/doc-agent/browser-contention-findings-2026-09-27.md`: real Chrome flow;
  compiler-helper rendering bug fixed. Same-model foreground median 295 ms alone
  (six samples), 888 ms with background work (three samples). Small, repeated-prompt
  experiment; no SLA, p95, GPU preemption or broad concurrency guarantee.
- `reports/doc-agent/plan-findings-2026-09-28.md`: eight live runs; both variants
  passed four structural cases, but citation selection and absence wording still
  failed manual review. Plans remain optional; no mandatory reasoning transcript.
- `reports/prompt-contract/findings-2026-09-26.md`: 600 scored calls; no universal
  prompt-format winner. Keep content, structure and model choice separate variables.

Historical validation: 248 TypeScript tests at the browser-fix checkpoint and 60
Python agent tests at the plan-experiment checkpoint. Not newly run Hekate tests.
Telemetry supports later learning; automatic policy training/self-improvement is
not implemented. Capture observations before deciding how to learn from them.

## Completion record

This handoff updates integration instructions only. It does not change Hekate
runtime behavior, commit unrelated engine work, deploy services, or establish a
completed context-store migration. Resume/career documents and the external
ChatAgent-learning journal are not runtime integration dependencies.
