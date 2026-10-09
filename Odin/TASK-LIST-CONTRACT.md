# Task-list contract: `GET /api/tasks/project/{project_id}`

## Defect

`list_tasks` in `gods/api.py` returned rows without `depends_on` or `tools`,
although the frontend (`orchestration/frontend/src/api/projects.ts`, `types.ts`)
expects both.

## Bounded change

- Selects `tools_json` and projects it as `tools: list[str]`. Null, malformed
  JSON, non-array JSON, or arrays containing non-strings become `[]`.
  `tools_json` itself is not returned.
- Adds `depends_on: list[str]`, the sorted IDs each returned task depends on.
  Dependencies are read from `task_deps` in bulk, only for the tasks the
  current project/status/wave query returned. A dependency that is itself
  filtered out of the list is still reported.
- Queries are chunked at `TASK_DEPS_CHUNK_SIZE` (500) task IDs. Every returned
  task is covered; nothing is dropped to fit the bound. An empty result runs no
  dependency query.
- Row order, filters and the rest of the row shape are unchanged. `output_text`
  and `error` are not added.
- No schema, execution, retry, routing, `PlanStore` or frontend changes.

## Test harness repair

The `client` fixture in `test_api.py` never closed `engine.db`, so the suite
reported its tests as passed while the process stayed alive on the open
aiosqlite connection. The fixture now uses `try/finally` and awaits
`engine.db.close()` after the client is closed, including when setup or a test
fails. This is a test-only fix, not a production lifecycle change.

## Coverage

`TestTaskListProjection` in `test_api.py` covers chains, tasks without deps,
cross-project isolation, status/wave filters, empty results, tools validation,
and query count and chunk bounds, using in-memory SQLite with no native workers.
