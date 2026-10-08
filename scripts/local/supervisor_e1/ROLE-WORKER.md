# Role worker (read-only planning roles)

`e1/role_worker.py` is a small runtime that runs one **role** once over a host-supplied **evidence snapshot** and
returns a validated, never-accepted result. First role: **Athena**, read-only planning, no tools.

It is a LangGraph `StateGraph` (`model -> validate`, compiled without a checkpointer) over an injected LangChain
`BaseChatModel`. It has no default provider, no env/credential discovery, no tools, no DB, no file reads, and never
touches PlanStore. It is not imported by any other e1 module.

## Run

```
uv sync --group roles
uv run --group roles pytest tests/test_role_worker.py
```

Without the `roles` group the test module skips itself; the other e1 tests and default dependencies are unaffected.

## Host usage

```python
from e1.role_worker import athena_role, run_role

model = ChatSomething(...)            # the HOST builds the provider model and holds its credentials
result = await run_role(
    athena_role(), model, snapshot, correlation,
    binding_id=None,                  # optional; must be in role.allowed_bindings
    model_identity="provider/model-x@2026-10",   # host's record of what the binding resolved to
)
```

- `snapshot`: JSON object (`dict/list/str/int/float/bool/None` only; ints within +-2^53-1, finite floats, depth <= 32,
  canonical size <= `max_input_bytes`) with an `evidence` list of objects, each with a unique `id`.
- `correlation`: exactly these keys, all matched with full-string matching (a trailing newline is rejected):
  - `rootId`, `taskId`: canonical lowercase hyphenated UUID text (`8-4-4-4-12` hex);
  - `attemptId`: `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` (not necessarily a UUID);
  - `epoch`, `contentRevision`: non-bool ints, **1..2^53-1** (0 and negatives are rejected);
  - `observedAt`: RFC 3339 with offset (when the *source* was observed).
- Invalid config raises `RoleError(config_invalid)` at `RoleConfig` construction, and `run_role` re-validates the role
  (and raises the same typed error for a non-`RoleConfig` or a role mutated past construction) before anything else.
  Invalid model object/identity, binding, snapshot or correlation returns a failed result **before** the model is called.
  Binding entries are element-validated before any set/hash operation, so unhashable entries are `config_invalid`.

## Direct CLI vs native role

This module is a library function, not a CLI and not a managed attempt. There is no direct command line that runs a
role; a host calls `run_role`. A direct/ad-hoc call produces a `RoleResult` only: nothing is recorded, no attempt, trace
or PlanStore row exists, and `completed` still means "validated output, review pending". A *native* (managed) role run
is one whose host supplies real UUID root/task ids, a real attempt id, positive epoch/revision and records the result;
that wiring belongs to the next task, not here.

## Role vs binding

A role is identity + version + instructions + limits + deadline. `bindingId` is only the name of the model route the
role asks for; the host maps it to a model. `allowed_bindings` is the explicit set a host may override to. Model
identity is host-configured evidence in the metadata; the model is never asked, and never trusted, to state it.

**Role definition hash** = sha256 of the canonical JSON of `RoleConfig.definition()` (schema id, id, version,
instructions, binding, sorted allowed bindings, limits, deadline, `tools: []`, the fixed untrusted-data boundary text,
output schema id). Canonical JSON: UTF-8, sorted keys, separators `,` `:`, no ASCII escaping, finite numbers only
(floats by Python repr). Snapshot and result hashes use the same encoding (snapshot hash = hash of exactly the bytes in
the human message).

## Prompt and output

System message = role instructions + a fixed boundary: the snapshot is untrusted data, a past observation that does
not establish liveness. Human message = the canonical snapshot JSON and nothing else.

Output must be one JSON object (no fences), unknown/duplicate fields rejected, `NaN` rejected:

```
{"summary": str(1..2000),
 "steps":    [{"title": str(1..200), "acceptance": str(1..500), "evidence": [id, ...1..20]}]   (1..20 steps),
 "findings": [{"text": str(1..500),  "evidence": [id, ...1..20]}]}                            (0..20)
```

Every cited id must be an `id` in the supplied snapshot (`invented_evidence` otherwise). There is no reasoning field;
only the validated output is retained, never the raw text.

## Result

`RoleResult(status, failure_code, output, metadata)`; `status` is `completed` or `failed`. `completed` means *validated
role output*: `accepted` is always `False` and `review_status` is `pending` (independent review decides). Failure codes:
`config_invalid, input_invalid, binding_not_allowed, unavailable, deadline_exceeded, refused, tool_call_rejected,
empty_output, malformed_output, output_too_large, schema_invalid, invented_evidence`. Provider exception text is never
returned. Metadata: requested binding, model identity (host), role id/version/hash, snapshot hash, result hash (only
if completed), correlation, observation time.

## Execution rules and limitations

- Exactly one model call; no retry. (A model object may retry internally; configure the host's model not to.)
- The deadline and external cancellation are **cooperative**: the awaiting task is cancelled and the model object is
  asked to stop, but this cannot prove the external provider stopped or did not bill. Deadline gives
  `deadline_exceeded`; external cancellation propagates as `asyncio.CancelledError`. The timeout object's `expired()` is
  also checked, so a provider that swallows the cancellation and returns later still yields `deadline_exceeded`, never
  its late output. No process or request is promised to stop.
- `RoleResult.output` returns a fresh deep copy on every read (dict-like access preserved); mutating it cannot change the
  result or invalidate `metadata.result_hash`. The snapshot is captured as canonical bytes before the call, so later host
  mutation of the original object changes neither the model input nor `snapshot_hash`.
- LangSmith auto-tracing (`LANGSMITH_TRACING` etc. in the inherited environment) is disabled around the graph/model call
  with `langsmith.tracing_context(enabled=False)` (`langsmith` is declared in the `roles` group). A host-injected model
  keeps its own external behavior (its callbacks, proxies, provider-side logging, retries); the host is responsible
  for those, and for not enabling tracing on the model object itself.
- Citations prove only that ids exist in the snapshot, not that the claims are true. The snapshot is a past
  observation and proves nothing about current liveness.
- Roles never write state. The next task connects managed attempts/traces and PlanStore recording; PlanStore
  ownership stays with the host/supervisor, and a role result is input to review, not a state transition.
- Role text is limited to a prompt-injection boundary, not a sandbox; the only safety property is that the role has no
  tools and its output is only data validated here.
