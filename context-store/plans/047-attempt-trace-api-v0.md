# 047 — Attempt trace API v0 (read-only)

Status: integrated and exercised live (2026-10-08). Root msgs 2449, 2450, 2458, 2461 and 2473.
- **Commits:** `a15f5d9` and `a3c3ba0`, reviewed and integrated into the primary branch.
- **Capture side:** the files and journal blocks this API reads are plan 046.
- **UI client:** `context-store/ui` (`planContract/api.ts` `getAttemptTrace`,
  `components/managed/AttemptTrace.tsx`), shown in the Plans view's central column since `7fc017f`.
- **Live use:** the Claude `readme-trace-001` and Codex `codex-trace-001` rehearsals both served
  their traces through this endpoint.
  - A runner-owned Api, then a post-run viewer, each started with `HEKATE_TRACE_ROOT` on that
    command only, served the traces.
  - In both, statuses went running/unverified → exited/verified, and every UI request was a GET.
  - Retained evidence: `D:/hekate-coordinator/view-trace-001/captures-trace-001` and
    `captures-codex-001` (each with a manifest).

## Endpoint

`GET /api/plan-contract/v1/nodes/{nodeId}/attempts/{attemptId}/trace?afterSeq={n}&limit={1..500}`

- The endpoint is in the loopback-only plan-contract group, so it is mapped only when
  `HEKATE_PLAN_CONTRACT=1`. It is GET only and writes nothing.
- `limit` defaults to 200.
- `afterSeq` is the last record `seq` the client already holds. Without it the request starts the
  trace, and only that first page carries the prompt.
- `HEKATE_TRACE_ROOT` is read for each request from the Api's environment. It is a run-scoped
  setting for the runner-owned Api and for a post-run viewer; it is not global configuration.

## Identity binding

All reads happen in one REPEATABLE READ transaction. Every query is parameterized and limited to
a fixed number of rows.

1. Read the node's root and current attempt from `plan_node_state`.
   - Unknown node: 404 `node_not_found`.
2. Read the `attempt_started` events of (node, attemptId): their epochs and claim keys.
   - None: 404 `ATTEMPT_NOT_FOUND`.
3. Find exactly one `claimed` receipt for (root, node, attemptId).
   - No receipt: `not_captured`, reason `no_claim_receipt`.
   - No receipt, but an event names a claim key: 409 `TRACE_IDENTITY_CONFLICT`.
   - More than one receipt: 409 `TRACE_IDENTITY_CONFLICT`.
   - The receipt's epoch must be one of the started epochs, and every claim key on an event
     must equal the receipt's. Otherwise: 409 `TRACE_IDENTITY_CONFLICT`.
4. Look up the journal by (root as lowercase uuid text, the receipt's claim key).
   - Records read: `launch_intent` and `exited`, at most one of each. A duplicate is 409
     `TRACE_IDENTITY_CONFLICT`.
   - A payload longer than 64 KiB is refused in SQL and returns 409 `TRACE_INVALID`.
   - No journal schema: 503 `TRACE_JOURNAL_UNAVAILABLE`.
   - No `launch_intent` and a compacted stream or summary: `not_captured`, reason
     `journal_compacted`.
   - No `launch_intent` otherwise: `not_captured`, reason `no_trace_block`.
   - A `launch_intent` without a `trace` block is an older run: `not_captured`, reason
     `no_trace_block`.

## Files

`launch_intent.data.trace = {version: "hekate-attempt-trace.v0", executionKind, runDir, prompt, trace}`.
`prompt` and `trace` are file names relative to `runDir`.

**Confinement.**
- Each name must be a bare file name.
- `runDir` must be absolute.
- The joined path must lie strictly inside `HEKATE_TRACE_ROOT`.
- Every existing component below the root must be a real entry. A symlink, junction or other
  reparse point is refused, in a parent or as the leaf.
- The leaf must be a regular file.
- Refusal: 409 `TRACE_PATH_REFUSED`. Unset root: 503 `TRACE_ROOT_NOT_CONFIGURED`.
- An absent file is `missing`.

**Reading.**
- Both files are read once per request, sharing with a writer that may still be appending.
- A file over 64 MiB is refused with 409 `TRACE_FILE_TOO_LARGE`.
- Only newline-terminated lines are records. A partial last line is not read yet.
- Each line's `seq` must equal its index, and its fields must match the record shape. Otherwise
  the response is 409 `TRACE_INVALID`.

## Status and integrity

| status | when | integrity |
| --- | --- | --- |
| `not_captured` | see Identity binding, with its `reason` | `none` |
| `missing` | a file named by `launch_intent` does not exist | `none` |
| `running` | no `exited`, and the node's current attempt (id and epoch) is this one with work `in_progress` | `unverified` |
| `unfinished` | no `exited`, and this is not the node's current in-progress attempt | `unverified` |
| `exited` | `exited.data.trace` present | `verified` when `complete` is true, `writeError` is false, and both retained files equal the recorded bytes and sha256; `unverified` when not complete or on a write error |

- A complete trace whose retained bytes differ is refused: 409 `TRACE_INTEGRITY_MISMATCH`, with no
  content.
- The status describes what is known about the trace. It does not mean a process is alive.
- Acceptance is never derived from a trace.
- `exit` (`{code, killReason}`) is present only for `exited`. `killReason` is the exited `reason`
  unless that reason is `exit`.

## Response

Every key is always present, with `null` when it does not apply:

```json
{"contractVersion": "plan-contract/v1", "nodeId": "…", "attemptId": "…", "attemptEpoch": 1, "claimKey": "…|null",
 "status": "…", "reason": "…|null", "integrity": "…", "executionKind": "…|null",
 "exit": {"code": 0, "killReason": null} | null, "prompt": {"text": "…", "bytes": 0} | null,
 "records": [{"seq": 0, "tMs": 0, "stream": "stdout|stderr|hekate", "text": "…", "cut": false, "redacted": false}],
 "nextAfterSeq": 0 | null, "capped": false}
```

**Size budget.**
- The whole serialized response must fit in 4 MiB of UTF-8. The header and the prompt are
  measured first, and records are added while they fit.
- `nextAfterSeq` is the last returned `seq` when more complete records exist.
- A first record that alone exceeds the remaining budget gets a typed refusal: 409
  `TRACE_RECORD_TOO_LARGE`. It is never an empty page with a cursor that cannot advance.
- A prompt larger than the budget is refused the same way.

## Tests

- `PlanContracts.Tests/AttemptTraceTests.cs` (pure, temp files): ref and exited parsing,
  confinement (names, outside or prefix paths, relative `runDir`, unset root, a directory as the
  leaf, a junction or link in a parent or as the leaf), integrity, paging and cursors, partial
  lines, seq and shape errors, the budget, and the oversized-record refusal.
- `PlanStore.LiveTests/AttemptTraceLiveTests.cs` (live database with the fixture journal schema):
  - a verified finished trace, with paging and the prompt on the first page only;
  - a running trace with a partial line;
  - an unfinished trace;
  - a write error left unverified, and a hash mismatch refused;
  - older, compacted and unclaimed attempts;
  - an unknown node or attempt;
  - a duplicate receipt conflict;
  - an outside path, an unset root, and missing files;
  - a database without a journal;
  - the status mapping.

## Limits

- No live push. The UI asks again with `afterSeq` and does not poll.
- No trace rows in the database, and no backfill of older attempts.
- Files are read in full on each request, bounded at 64 MiB.
- The journal schema is the supervisor fixture schema created by the local store. A database
  without it gets 503 rather than an empty trace.
