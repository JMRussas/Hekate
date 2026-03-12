# Orchestration Engine — Code Review Remediation Plan

Addresses all 35 issues from the comprehensive code review, plus 3 additional issues found during plan review.
Organized into 6 phases with dependency ordering — each phase builds on the previous.

---

## Phase 1: Security Critical ✅ DONE

Everything in this phase is exploitable or would be flagged as a showstopper in any security review. No other work matters until these are closed.

> **Completed:** All 7 items implemented, 160 tests passing (29 new Phase 1 tests).

### 1.1 — JWT Secret Validation at Startup

**Issue:** `AUTH_SECRET_KEY` defaults to `""` in `config.py:116`. PyJWT signs tokens with an empty key, making them trivially forgeable.

**Files:** `backend/config.py`, `backend/app.py`

**Changes:**
- Add a `validate_config()` function in `config.py` that asserts `AUTH_SECRET_KEY` is non-empty and at least 32 characters. Called explicitly, not at import time.
- Call `validate_config()` in `app.py` lifespan, before `db.init()`. Fail fast with a clear error message: `"FATAL: auth.secret_key is missing or too short in config.json"`.
- Also validate `ANTHROPIC_API_KEY` is set (non-empty) with a warning (not fatal, since Ollama-only usage is valid).

**Tests:** Unit test that `validate_config()` raises on empty/short secret. Integration test that app startup fails with bad config.

---

### 1.2 — Add Auth to All Unprotected Routes

**Issue:** `projects.py`, `tasks.py`, `usage.py`, `services.py` have zero auth dependencies. Any unauthenticated request hits them.

**Files:** `backend/routes/projects.py`, `backend/routes/tasks.py`, `backend/routes/usage.py`, `backend/routes/services.py`

**Changes:**
- Add `current_user: dict = Depends(get_current_user)` to every endpoint in all four route files.
- `services.py` endpoints: require auth but no ownership (infrastructure health is global).
- `usage.py` endpoints: require auth. `get_usage_summary` with a `project_id` param requires project ownership (see 1.3). Global budget status: admin-only via `require_admin` dependency.
- Pass `current_user` through to service calls where needed for ownership checks (Phase 1.3).

**Tests:** Existing tests already use `authed_client` fixture, but add explicit tests that unauthenticated requests return 401.

---

### 1.3 — Project/Task Ownership Enforcement

**Issue:** Any authenticated user can CRUD any project/task. The `owner_id` column exists on `projects` but is never set or checked.

**Files:** `backend/routes/projects.py`, `backend/routes/tasks.py`, `backend/routes/events.py`, `backend/routes/usage.py`

**Changes:**

**projects.py:**
- `create_project`: Set `owner_id = current_user["id"]` in the INSERT.
- `list_projects`: Add `WHERE owner_id = ?` filter (admins see all).
- `get_project`, `update_project`, `delete_project`: Fetch project, check `row["owner_id"] == current_user["id"]` or admin role. Return 403 if not.
- `trigger_plan`, `approve_plan`, `start_execution`, `pause_execution`, `cancel_project`: Same ownership check.
- Extract a shared `_get_owned_project(db, project_id, user) -> Row` helper that returns the row or raises 403/404.

**tasks.py:**
- Every endpoint receives a `project_id` from the task's parent project. Add a join or secondary query to verify the current user owns the parent project. Same admin exemption.
- For `list_tasks(project_id)`: verify project ownership before returning tasks.

**events.py:**
- Already has auth (`get_user_from_token_param`). Add project ownership check before opening SSE stream. Return 403 if user doesn't own the project.

**usage.py:**
- `get_usage_summary(project_id=...)`: verify project ownership when project_id is provided.
- `get_usage_by_project`: filter to owned projects (admins see all).

**Migration:** The `owner_id` column already exists. Existing projects in dev DBs have `NULL` owner_id. Add a one-time migration that assigns orphan projects to the first admin user, or make ownership checks treat `NULL` owner_id as admin-owned.

**Tests:** Add tests for: user A can't read/modify user B's project. Admin can access all. Unauthenticated returns 401.

---

### 1.4 — Fix Email Enumeration

**Issue:** Register endpoint returns 409 "Email already registered", allowing account enumeration.

**Files:** `backend/services/auth.py`, `backend/routes/auth.py`

**Changes:**
- `auth.py` `register()`: On duplicate email, raise the same generic `ValueError("Registration failed")` instead of `"Email already registered"`.
- `routes/auth.py` `register()`: Return 400 with `"Registration failed"` (not 409). This makes the response indistinguishable from a validation error.
- Add a timing-safe login path: when `user` is `None` in `login()`, still run `bcrypt.checkpw(password, dummy_hash)` against a pre-computed dummy hash to prevent timing side-channel enumeration.

**Tests:** Update existing tests that assert on the "Email already registered" message.

---

### 1.5 — Fix SQL Injection Surface in RAG Tools

**Issue:** `rag.py:238` LIKE pattern doesn't escape wildcards. `rag.py:244-247` FTS MATCH query has incomplete sanitization.

**Files:** `backend/tools/rag.py`

**Changes:**
- **LIKE query** (line 238): Use `INSTR()` instead of LIKE to avoid wildcard interpretation:
  ```sql
  WHERE INSTR(LOWER(name), LOWER(?)) > 0
  ```
  Or escape wildcards: replace `%` with `\%`, `_` with `\_`, and add `ESCAPE '\'`.
- **FTS query** (line 244-247): Strip all FTS5 operators before building the MATCH clause. Replace the current `safe = name.replace('"', '""')` with a function that removes `*`, `(`, `)`, `+`, `-`, `^` and the keywords `AND`, `OR`, `NOT`, `NEAR`. Wrap the entire term in double quotes for phrase matching.
- Wrap both queries in try/except to fall back gracefully on malformed input.

**Tests:** Add tests with inputs containing `%`, `_`, `*`, `OR`, `"`, and verify they don't alter query semantics.

---

### 1.6 — Validate Email Format

**Issue:** `RegisterRequest.email` accepts any 3-character string.

**Files:** `backend/models/schemas.py`, `requirements.txt`

**Changes:**
- Add `email-validator` to `requirements.txt`.
- Change `email: str = Field(..., min_length=3)` to `email: EmailStr` (from `pydantic`).
- This gives proper RFC 5322 validation at the API boundary.

**Tests:** Test that `"abc"`, `"@"`, `"a@b"` are rejected. Test that `"user@example.com"` passes.

---

### 1.7 — Issue Short-Lived SSE Tokens

**Issue:** The full access token is passed as a query parameter for SSE (`?token=...`), exposing it in server access logs, proxy logs, and browser history. A compromised log yields full account access.

**Files:** `backend/services/auth.py`, `backend/routes/events.py`, `backend/middleware/auth.py`, `frontend/src/api/client.ts`

**Changes:**
- Add a new token type `"sse"` to `AuthService` with a short TTL (e.g., 60 seconds) and a `project_id` claim scoping it to a single project:
  ```python
  def create_sse_token(self, user_id: str, project_id: str) -> str:
      payload = {
          "sub": user_id,
          "type": "sse",
          "project_id": project_id,
          "exp": datetime.now(timezone.utc) + timedelta(seconds=60),
      }
      return jwt.encode(payload, AUTH_SECRET_KEY, algorithm=AUTH_ALGORITHM)
  ```
- Add a new endpoint `POST /api/events/{project_id}/token` (requires Bearer auth + ownership) that returns the SSE token.
- Modify `get_user_from_token_param` in `middleware/auth.py` to accept `type == "sse"`, validate the `project_id` claim matches the route parameter, and reject expired tokens.
- Frontend: before opening an `EventSource`, call the token endpoint to get a short-lived SSE token, then use that in the query parameter instead of the access token.

**Why this matters:** Even if access logs are compromised, the SSE token is useless after 60 seconds and only works for one project's event stream. The access token never appears in a URL.

**Tests:** Test that SSE token works for its scoped project. Test that it's rejected for other projects. Test that it expires.

---

## Phase 2: Data Integrity ✅ DONE

Transaction safety and schema consistency. These prevent data corruption under concurrent load.

> **Completed:** All 6 items implemented, 177 tests passing (17 new Phase 2 tests).

### 2.1 — Add Transaction Context Manager to Database

**Issue:** `execute_write` auto-commits per statement. `execute_many_write` has no rollback. No way to do atomic read+write sequences.

**Files:** `backend/db/connection.py`

**Changes:**
- Add an `_in_transaction` flag and `asynccontextmanager` method `transaction()` on `Database`:
  ```python
  def __init__(self):
      ...
      self._in_transaction = False

  @asynccontextmanager
  async def transaction(self):
      """Atomic read+write transaction. Rolls back on exception."""
      if self._in_transaction:
          # Already in a transaction — just yield (no nesting).
          # SQLite doesn't support true nested transactions without SAVEPOINTs,
          # and for our use case flat transactions are sufficient.
          yield self.conn
          return
      await self.conn.execute("BEGIN IMMEDIATE")
      self._in_transaction = True
      try:
          yield self.conn
          await self.conn.commit()
      except Exception:
          await self.conn.rollback()
          raise
      finally:
          self._in_transaction = False
  ```
  `BEGIN IMMEDIATE` acquires a write lock upfront, preventing other writers from interleaving.

- **Nesting safety:** `execute_write` must respect the `_in_transaction` flag to avoid premature commits:
  ```python
  async def execute_write(self, sql, params=()):
      cursor = await self.conn.execute(sql, params)
      if not self._in_transaction:
          await self.conn.commit()
      return cursor
  ```
  This means `execute_write` is safe to call both inside and outside a `transaction()` block. Inside a transaction, it participates in the outer transaction. Outside, it auto-commits as before.

- Refactor `execute_many_write` to use `transaction()` internally:
  ```python
  async def execute_many_write(self, statements):
      async with self.transaction():
          for sql, params in statements:
              await self.conn.execute(sql, params)
  ```

**Tests:** Test that a failed statement in `execute_many_write` rolls back all prior statements. Test that `transaction()` context manager commits on success and rolls back on exception. Test that `execute_write` inside a `transaction()` block does NOT auto-commit (the transaction controls the commit).

---

### 2.2 — Fix Admin Race Condition

**Issue:** Two concurrent first-registration requests can both become admin.

**Files:** `backend/services/auth.py`

**Depends on:** 2.1 (needs `transaction()`)

**Changes:**
- Wrap the register flow in a `transaction()`:
  ```python
  async with self._db.transaction() as conn:
      existing = await conn.execute("SELECT id FROM users WHERE email = ?", (email,))
      if await existing.fetchone():
          raise ValueError("Registration failed")
      count = await conn.execute("SELECT COUNT(*) as cnt FROM users")
      row = await count.fetchone()
      role = "admin" if row[0] == 0 else "user"
      await conn.execute("INSERT INTO users ...", (...))
  ```
  With `BEGIN IMMEDIATE`, the second concurrent request blocks until the first commits, then correctly sees `cnt == 1`.
- Also add a `UNIQUE(email)` constraint to the users table (via Alembic migration) as a defense-in-depth.

**Tests:** Concurrent registration test using `asyncio.gather` to verify only one admin is created.

---

### 2.3 — Fix TOCTOU Budget Race

**Issue:** `can_spend` reads current totals, but between the check and `record_spend`, other tasks can also pass the check.

**Files:** `backend/services/budget.py`, `backend/services/executor.py`

**Changes:**
- Add in-memory reservation tracking to `BudgetManager`, protected by an `asyncio.Lock`:
  ```python
  def __init__(self, db):
      ...
      self._lock = asyncio.Lock()
      self._reserved_daily: float = 0.0
      self._reserved_monthly: float = 0.0
      self._last_daily_key: str = ""
      self._last_monthly_key: str = ""
  ```
- New method `reserve_spend(estimated_cost) -> bool`:
  ```python
  async def reserve_spend(self, estimated_cost: float) -> bool:
      async with self._lock:
          # Reset reservations on period rollover
          daily_key = _today_key()
          monthly_key = _month_key()
          if daily_key != self._last_daily_key:
              self._reserved_daily = 0.0
              self._last_daily_key = daily_key
          if monthly_key != self._last_monthly_key:
              self._reserved_monthly = 0.0
              self._last_monthly_key = monthly_key

          status = await self.get_budget_status()
          daily_ok = status["daily_spent"] + self._reserved_daily + estimated_cost <= status["daily_limit"]
          monthly_ok = status["monthly_spent"] + self._reserved_monthly + estimated_cost <= status["monthly_limit"]
          if not (daily_ok and monthly_ok):
              return False
          self._reserved_daily += estimated_cost
          self._reserved_monthly += estimated_cost
          return True
  ```
- New method `release_reservation(estimated_cost)`: decrements both `_reserved_daily` and `_reserved_monthly` (clamped to 0). Called after `record_spend` (which writes the actual cost to DB) or on task failure.
- In `executor.py`, replace `can_spend(est)` calls with `reserve_spend(est)` and add `release_reservation` in the task completion/failure paths (both success and exception handlers in `_execute_task`).
- `_reserved` resets to 0 on app restart (acceptable since DB has the committed spend).

**Trade-off note:** The reservation is approximate — a task reserved before midnight but completing after midnight creates a stale daily reservation. This is bounded by `MAX_CONCURRENT_TASKS * max_single_task_cost` and self-corrects within one tick (2s). Document this in the code.

**Tests:** Test concurrent `reserve_spend` calls that together would exceed budget — verify only one succeeds. Test period rollover resets reservations.

---

### 2.4 — Sync Schema: models_metadata.py ← connection.py

**Issue:** 8 foreign keys exist in the inline SQL schema but are missing from SQLAlchemy Table definitions. Alembic autogenerate is blind to them.

**Files:** `backend/db/models_metadata.py`

**Changes:**
Add `ForeignKey(...)` to every column that has a `REFERENCES` clause in `connection.py`:

| Table | Column | Add |
|-------|--------|-----|
| `projects` | `owner_id` | `ForeignKey("users.id")` |
| `plans` | `project_id` | `ForeignKey("projects.id", ondelete="CASCADE")` |
| `tasks` | `project_id` | `ForeignKey("projects.id", ondelete="CASCADE")` |
| `tasks` | `plan_id` | `ForeignKey("plans.id", ondelete="CASCADE")` |
| `task_deps` | `task_id` | `ForeignKey("tasks.id", ondelete="CASCADE")` |
| `task_deps` | `depends_on` | `ForeignKey("tasks.id", ondelete="CASCADE")` |
| `usage_log` | `project_id` | `ForeignKey("projects.id")` |
| `usage_log` | `task_id` | `ForeignKey("tasks.id")` |

After updating, run `alembic revision --autogenerate` to confirm no new migration is generated (the actual DB already has these FKs from the inline schema or prior migrations — the metadata file just needs to match for future autogenerate accuracy).

**Tests:** No new tests needed — this is metadata alignment.

---

### 2.5 — Fix Engine Leak in migrate.py

**Issue:** `engine.dispose()` skipped if `command.upgrade()` raises.

**Files:** `backend/db/migrate.py`

**Changes:**
- Wrap in `try/finally`:
  ```python
  engine = create_engine(url)
  try:
      # ... inspector check, command.upgrade ...
  finally:
      engine.dispose()
  ```
- Remove the unused `conn` variable from the `with engine.connect() as conn:` block (line 39). The `inspect(engine)` call doesn't need it.
- Wrap `run_migrations()` call in `Database.init()` with `await asyncio.to_thread(run_migrations, ...)` to avoid blocking the event loop during migration.

**Tests:** Existing migration tests should still pass. No new tests needed.

---

### 2.6 — Fix App Lifespan Cleanup Gap

**Issue:** If `resource_monitor.start_background()` fails at `app.py:57`, `db` was already initialized at line 54 but is never closed. The `yield` is never reached, so the shutdown block never runs. Same for other partial-startup failures.

**Files:** `backend/app.py`

**Changes:**
- Use `contextlib.AsyncExitStack` for robust startup/shutdown:
  ```python
  @asynccontextmanager
  async def lifespan(app: FastAPI):
      logger.info("Orchestration Engine starting...")
      db = container.db()
      resource_monitor = container.resource_monitor()
      executor = container.executor()

      async with AsyncExitStack() as stack:
          await db.init(DB_PATH, run_migrations=True)
          stack.push_async_callback(db.close)

          await resource_monitor.start_background()
          stack.push_async_callback(resource_monitor.stop_background)

          await executor.start()
          stack.push_async_callback(executor.stop)

          yield
      logger.info("Orchestration Engine shutting down")
  ```
  If any step fails, all previously registered callbacks run in reverse order. No manual try/except chains needed.

**Tests:** No new tests needed — this is infrastructure robustness.

---

## Phase 3: Concurrency & Reliability ✅ DONE

Fix the executor and tool system to be robust under concurrent load.

> **Completed:** All 6 items implemented, 198 tests passing (21 new Phase 3 tests).

### 3.1 — Fix Semaphore Held During Retry Sleep

**Issue:** `executor.py:304` sleeps inside `async with self._semaphore`, blocking a concurrency slot for up to 80+ seconds.

**Files:** `backend/services/executor.py`

**Changes:**
- On transient error, release the semaphore BEFORE sleeping. Restructure the retry flow:
  ```python
  # Inside _execute_task, after catching transient error:
  # 1. Set task back to PENDING in DB (already done)
  # 2. Remove from _dispatched set so next tick can re-dispatch
  # 3. Return from _execute_task (releases semaphore via `async with` exit)
  ```
- The existing tick loop will re-find the task as PENDING and re-dispatch it. The backoff delay is handled by adding a `retry_after` timestamp column (or an in-memory dict `_retry_after: dict[str, float]`) that the dispatch logic checks:
  ```python
  # In _tick, when selecting ready tasks:
  if task_id in self._retry_after and time.time() < self._retry_after[task_id]:
      continue  # Skip, not yet ready for retry
  ```
- Remove the `asyncio.sleep(delay)` from inside the semaphore scope entirely.
- Clean up `_retry_after` entries when a task completes, fails permanently, or on `stop()`.

**Granularity note:** The tick runs every 2 seconds, so a 5-second backoff may actually be 4-6 seconds depending on tick alignment. This is acceptable — the backoff is a minimum delay, not a precise timer.

**Tests:** Test that a retrying task does not block other tasks from executing. Test that `_retry_after` is cleaned on task completion and on `stop()`.

---

### 3.2 — Move RAG _indexes Into DI / ToolRegistry

**Issue:** `rag.py:79` `_indexes` is a module-level mutable global with no invalidation or thread safety.

**Files:** `backend/tools/rag.py`, `backend/tools/registry.py`

**Changes:**
- Remove the module-level `_indexes` dict.
- Move index management into the tool classes themselves. Each `SearchKnowledgeTool` and `LookupTypeTool` instance holds its own `_indexes: dict[str, _RAGIndex]` initialized in `__init__`.
- Since both tools share the same indexes, have `ToolRegistry` pass a shared `_RAGIndexCache` instance to both tool constructors:
  ```python
  class _RAGIndexCache:
      def __init__(self):
          self._indexes: dict[str, _RAGIndex] = {}
          self._lock = asyncio.Lock()

      async def get(self, db_name: str) -> _RAGIndex | None:
          async with self._lock:
              if db_name not in self._indexes:
                  ...
              return self._indexes.get(db_name)
  ```
- Register `_RAGIndexCache` as a singleton in the DI container, or create it in `ToolRegistry.__init__` and pass to tool constructors.

**Tests:** Test that two concurrent `get()` calls don't create duplicate indexes.

---

### 3.3 — Fix _RAGIndex.load() Error Recovery

**Issue:** Setting `_loaded = True` in the except branch permanently disables the index.

**Files:** `backend/tools/rag.py`

**Changes:**
- Replace the boolean `_loaded` with a tri-state: `_state: Literal["unloaded", "loaded", "failed"]`.
- On success: `_state = "loaded"`.
- On failure: `_state = "failed"`, store the error, and record `_failed_at = time.time()`.
- In `load()`, allow retry after a cooldown (e.g., 60 seconds):
  ```python
  if self._state == "failed" and time.time() - self._failed_at < 60:
      return  # Still in cooldown
  if self._state == "loaded":
      return
  # ... attempt load ...
  ```
- This allows transient failures (file locked, OOM) to recover on the next attempt.

**Tests:** Test that a failed load can recover after cooldown. Test that a permanent failure (file not found) stays failed.

---

### 3.4 — Move Blocking Sync I/O Off the Event Loop

**Issue:** RAG SQLite queries, resource monitor HTTP checks, and file tool I/O block the async event loop.

**Files:** `backend/tools/rag.py`, `backend/tools/file.py`, `backend/services/resource_monitor.py`

**Changes:**

**rag.py:**
- Wrap all sync SQLite calls in `asyncio.to_thread()`:
  ```python
  rows = await asyncio.to_thread(conn.execute, sql, params).fetchall()
  ```
  Or better: make `_format_results` and `LookupTypeTool._query` async methods that use `to_thread` internally.
- Wrap `_RAGIndex.load()` in `to_thread` as well (numpy + sqlite init).
- **Thread safety:** Add a `threading.Lock` to each `_RAGIndex` instance to serialize sync SQLite access. `check_same_thread=False` disables Python's safety check but does NOT make the connection thread-safe for concurrent reads from different threadpool threads. The lock ensures that `to_thread` calls are serialized per-index:
  ```python
  class _RAGIndex:
      def __init__(self, ...):
          ...
          self._lock = threading.Lock()

      def query_sync(self, sql, params):
          with self._lock:
              return self.conn.execute(sql, params).fetchall()
  ```
  The `asyncio.to_thread(idx.query_sync, sql, params)` pattern keeps the lock acquisition off the event loop.

**file.py:**
- Wrap `read_text()` and `write_text()` in `asyncio.to_thread()`.
- Move `base.mkdir()` out of `_safe_path` for reads (only create dirs on write).

**resource_monitor.py:**
- Replace `urllib.request.urlopen` with `httpx.AsyncClient` for health checks.
- Make `check_all()` an `async def` that runs checks concurrently with `asyncio.gather`.
- Update `routes/services.py` to use `async def` handlers.
- Replace `asyncio.get_event_loop()` with `asyncio.get_running_loop()` in `_check_loop`.
- Fix URL parsing: use `urllib.parse.urlparse` instead of string splitting.

**Tests:** Existing tests should pass. Add a test that verifies health checks run concurrently (mock with delays).

---

### 3.5 — Add Per-Round Budget Check in Tool Loop

**Issue:** Executor tool loop runs up to 10 Claude API calls with no mid-loop budget check.

**Files:** `backend/services/executor.py`

**Depends on:** 2.3 (reservation model)

**Changes:**
- Track cumulative cost within the tool loop. After each `record_spend`, compare the cumulative actual cost against the original reservation amount. If the actual cost exceeds the reservation (meaning the estimate was too low), check `budget.can_spend(0.001)` as a hard stop. If budget is exhausted mid-loop, break out with a partial result rather than continuing to make API calls.
- Add a log message: `"Budget exhausted mid-tool-loop for task %s after %d rounds, returning partial result"`.
- On break, the partial output is still saved to the task (completes with whatever output was generated so far, not marked as failed).

**Tests:** Mock a tool loop where budget runs out after round 2 of 10. Verify it stops and returns partial output.

---

### 3.6 — Reuse httpx Clients Across Tool Calls

**Issue:** `rag.py`, `ollama.py`, `comfyui.py` each create a new `httpx.AsyncClient` per call.

**Files:** `backend/tools/rag.py`, `backend/tools/ollama.py`, `backend/tools/comfyui.py`, `backend/container.py`

**Changes:**
- Create a shared `httpx.AsyncClient` in the DI container with a generous base timeout (e.g., 300s). Individual tools override per-request via `httpx`'s per-request timeout support:
  ```python
  # Shared client with high base timeout
  container: http_client = providers.Singleton(httpx.AsyncClient, timeout=300.0)

  # Tool usage — per-request override:
  resp = await self._http.post(url, json=body, timeout=OLLAMA_REQUEST_TIMEOUT)
  ```
  This gives connection pooling while allowing Ollama (120s), embedding (30s), ComfyUI prompt (30s), and health checks (2s) to each have appropriate timeouts.
- Pass the shared client to tool constructors via `ToolRegistry`.
- Add cleanup in app lifespan: `await http_client.aclose()` (via the `AsyncExitStack` from 2.6).
- Move the hardcoded Ollama generation timeout (120s) to config as `ollama.request_timeout` (key already exists in `config.example.json`).
- Move ComfyUI's hardcoded checkpoint name (`sd_xl_base_1.0.safetensors` at `comfyui.py:128`) to config as `comfyui.default_checkpoint`.
- For ComfyUI polling: keep a separate per-call client for the long-lived polling loop, since it may block for up to `COMFY_TIMEOUT` (300s). Only use the shared client for the initial prompt submission.

**Tests:** Verify tool calls don't create new client instances (mock the shared client). Verify per-request timeout overrides work.

---

## Phase 4: Validation & Error Handling ✅ DONE

Input validation, error types, and defensive coding.

> **Completed:** All 8 items implemented, 211 tests passing (5 new Phase 4 tests).

### 4.1 — Add Bounds to TaskUpdate and Other Schemas ✅ DONE

**Files:** `backend/models/schemas.py`

**Changes:**
- `TaskUpdate.max_tokens`: add `Field(ge=1, le=32768)`.
- `TaskUpdate.priority`: add `Field(ge=0, le=100)`.
- `TaskUpdate.title`: add `Field(min_length=1, max_length=500)`.
- `TaskUpdate.description`: add `Field(max_length=10000)`.
- `ProjectCreate.requirements`: add `Field(max_length=50000)`.
- `ProjectUpdate.name`: add `Field(min_length=1, max_length=200)` when provided.
- `ProjectUpdate.requirements`: add `Field(max_length=50000)` when provided.
- `RegisterRequest.password`: add `Field(max_length=128)`.
- `LoginRequest`: add `Field(min_length=1)` on both fields.

**Tests:** Add parametrized tests for boundary values (0, negative, max+1).

---

### 4.2 — Add Retry Count Limit to Task Retry Endpoint ✅ DONE

**Issue:** `tasks.py:161-181` allows infinite retries.

**Files:** `backend/routes/tasks.py`, `backend/config.py`

**Changes:**
- Add `MAX_TASK_RETRIES = cfg("execution.max_task_retries", 5)` to config.
- In the `retry_task` endpoint, check `row["retry_count"] >= MAX_TASK_RETRIES` and return 400 `"Maximum retry limit reached"`.
- Add `max_task_retries` to `config.example.json`.

**Tests:** Test that retry at the limit is rejected.

---

### 4.3 — Error on Unknown Model in Cost Calculation ✅ DONE

**Issue:** `model_router.py:29-30` silently returns 0.0 for unknown models.

**Files:** `backend/services/model_router.py`

**Changes:**
- If model not in `MODEL_PRICING`, log a warning: `"Unknown model '%s' — cost will be recorded as $0.00"`.
- Add a `_warned_models: set` to avoid log spam.
- In `get_model_id`: validate that the resolved model ID exists in `MODEL_PRICING` at startup (in `validate_config()`), not at call time.
- Remove the dead `TaskType` import.

**Tests:** Test that unknown model logs a warning. Test that `validate_config` catches model/pricing mismatch.

---

### 4.4 — Fix Hardcoded Token Estimates ✅ DONE

**Issue:** Planner uses `2000/2000`, executor uses `1500`, decomposer uses `1500` — all hardcoded.

**Files:** `backend/services/planner.py`, `backend/services/executor.py`, `backend/services/decomposer.py`

**Changes:**
- Define named constants at the top of each file:
  ```python
  # planner.py
  _ESTIMATED_PLANNING_INPUT_TOKENS = 3000   # system prompt (~2k) + requirements
  _ESTIMATED_PLANNING_OUTPUT_TOKENS = 4096  # matches max_tokens parameter

  # executor.py / decomposer.py
  _ESTIMATED_TASK_INPUT_TOKENS = 2000       # system prompt + context + tools
  ```
- Use these constants in the `calculate_cost` calls.
- Add comments explaining the estimates.

**Tests:** No new tests needed — behavioral change is minimal.

---

### 4.5 — Fix JSON Extraction Regex in Planner ✅ DONE

**Issue:** Greedy `\{[\s\S]*\}` captures from first `{` to last `}` in entire response.

**Files:** `backend/services/planner.py`

**Changes:**
- Before regex, strip markdown code fences:
  ```python
  # Remove ```json ... ``` wrappers
  cleaned = re.sub(r'```(?:json)?\s*', '', response_text)
  cleaned = re.sub(r'```\s*$', '', cleaned, flags=re.MULTILINE)
  ```
- Then try `json.loads(cleaned.strip())` first (in case the entire response is valid JSON).
- Fall back to the regex only if direct parse fails, and use a balanced-brace parser instead of regex for robustness.

**Tests:** Test with: raw JSON, JSON in code fences, JSON with trailing text, nested braces.

---

### 4.6 — Improve Decomposer Validation (Partial — cycle detection done) ✅

**Issue:** Silent drop of non-numeric dependency indices. No cycle detection. No plan status check. `_update_blocked_status` runs outside the main transaction.

**Files:** `backend/services/decomposer.py`

**Depends on:** 2.1 (needs `transaction()`)

**Changes:**
- **Plan status check**: Before decomposing, verify `plan_row["status"] == PlanStatus.DRAFT.value`. Raise `ValueError("Plan already approved")` if not.
- **Dependency warnings**: When `dep_raw` is dropped (non-numeric, out of range, self-ref), log a warning with the task title and the invalid value.
- **Cycle detection**: After building the dependency edges, run a topological sort (Kahn's algorithm is simple — maintain in-degree counts). If a cycle is detected, raise `ValueError(f"Circular dependency detected involving tasks: {cycle_tasks}")`.
- **Atomic blocked-status update**: Move `_update_blocked_status` into the main `execute_many_write` batch (or wrap the entire sequence in a `db.transaction()` block). Currently `_update_blocked_status` runs as a separate write after the task inserts commit (decomposer.py:116). If the app crashes between the two, tasks that should be BLOCKED remain PENDING. The executor's ready-task SQL handles this correctly at runtime, but the UI would show incorrect status. With `transaction()` from Phase 2.1, the fix is straightforward:
  ```python
  async with db.transaction():
      # ... all task inserts, dep inserts, plan approval, project status ...
      await _update_blocked_status(db, task_ids)
  ```

**Tests:** Add tests for: already-approved plan, circular deps, non-numeric dep indices, out-of-range indices.

---

### 4.7 — Create Exception Hierarchy for HTTP Mapping ✅ DONE

**Issue:** Services raise `ValueError` for everything — routes must pattern-match on message strings to decide HTTP status codes.

**Files:** New file `backend/errors.py`, then update all services and routes.

**Changes:**
- Create a small hierarchy:
  ```python
  class AppError(Exception):
      status_code: int = 500

  class NotFoundError(AppError):
      status_code = 404

  class ForbiddenError(AppError):
      status_code = 403

  class ConflictError(AppError):
      status_code = 409

  class BudgetExceededError(AppError):
      status_code = 402

  class ValidationError(AppError):
      status_code = 400
  ```
- Add a global exception handler in `app.py`:
  ```python
  @app.exception_handler(AppError)
  async def app_error_handler(request, exc):
      return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
  ```
- Migrate services from `raise ValueError(...)` to specific exception types.
- Routes can then remove their try/except blocks for ValueError → HTTPException mapping.

**Tests:** Test that each exception type maps to the correct HTTP status code.

---

### 4.8 — ToolRegistry Partial Registration ✅ DONE

**Issue:** One broken tool prevents the entire app from starting.

**Files:** `backend/tools/registry.py`

**Changes:**
- Wrap each tool instantiation in try/except:
  ```python
  for tool_cls in [SearchKnowledgeTool, LookupTypeTool, ...]:
      try:
          tool = tool_cls()
          self._tools[tool.name] = tool
      except Exception as e:
          logger.warning("Failed to register tool %s: %s", tool_cls.__name__, e)
  ```
- Log which tools registered successfully at INFO level.
- The executor already handles missing tools gracefully (returns error string to Claude).

**Tests:** Mock a tool class that raises on `__init__`. Verify other tools still register.

---

## Phase 5: Frontend (Partial)

> **Partially completed:** 5.1, 5.2, 5.4 done. 5.3, 5.5 remaining.

### 5.1 — Add ErrorBoundary Component ✅ DONE

**Files:** New `frontend/src/components/ErrorBoundary.tsx`, update `frontend/src/App.tsx`

**Changes:**
- Create a React class component `ErrorBoundary` with `componentDidCatch` and `getDerivedStateFromError`.
- Render a user-friendly error screen with "Something went wrong" and a "Reload" button.
- Wrap `<Routes>` in `<ErrorBoundary>` in `App.tsx`.

---

### 5.2 — Add 404 Catch-All Route ✅ DONE

**Files:** New `frontend/src/pages/NotFound.tsx`, update `frontend/src/App.tsx`

**Changes:**
- Create a simple `NotFound` page with "Page not found" message and a link back to `/`.
- Add `<Route path="*" element={<NotFound />} />` inside the `<Layout>` route group (for authenticated users) and outside it (for unauthenticated users).

---

### 5.3 — Fix Token Refresh Race Condition

**Issue:** Multiple concurrent 401s trigger multiple refresh requests.

**Files:** `frontend/src/api/client.ts`, `frontend/src/api/auth.ts`

**Changes:**
- Add a module-level `let refreshPromise: Promise<boolean> | null = null` in `auth.ts`.
- In `apiRefresh()`:
  ```ts
  export function apiRefresh(): Promise<boolean> {
    if (refreshPromise) return refreshPromise
    refreshPromise = _doRefresh().finally(() => { refreshPromise = null })
    return refreshPromise
  }
  ```
- All concurrent callers share the same in-flight promise.

---

### 5.4 — Route apiGetMe Through authFetch ✅ DONE

**Issue:** `apiGetMe` bypasses the `authFetch` wrapper, missing auto-refresh on 401.

**Files:** `frontend/src/api/auth.ts`

**Changes:**
- Rewrite `apiGetMe` to use `authFetch`:
  ```ts
  export async function apiGetMe(): Promise<User> {
    const resp = await authFetch(`${BASE}/me`)
    if (!resp.ok) throw new Error('Not authenticated')
    return resp.json()
  }
  ```
- Remove the manual `getAccessToken()` + `fetch()` pattern.

---

### 5.5 — Extract useFetch Hook

**Issue:** Every page repeats the same 15-line useEffect/useState/catch pattern.

**Files:** New `frontend/src/hooks/useFetch.ts`, then refactor all pages.

**Changes:**
- Create a generic hook:
  ```ts
  function useFetch<T>(fetcher: () => Promise<T>, deps: any[] = []) {
    const [data, setData] = useState<T | null>(null)
    const [error, setError] = useState<string>('')
    const [loading, setLoading] = useState(true)

    const refresh = useCallback(() => {
      setLoading(true)
      setError('')
      fetcher().then(setData).catch(e => setError(String(e))).finally(() => setLoading(false))
    }, deps)

    useEffect(() => { refresh() }, [refresh])

    return { data, error, loading, refresh }
  }
  ```
- Refactor `Dashboard`, `ProjectDetail`, `TaskDetail`, `Usage`, `Services` to use it.
- Pages that use `Promise.all` should use `Promise.allSettled` instead to handle partial failures.

---

## Phase 6: Code Quality, Tests & Docs

### 6.1 — Deduplicate Auth Middleware

**Files:** `backend/middleware/auth.py`

**Changes:**
- Extract shared logic into `_validate_access_token(auth, raw_token) -> dict`:
  ```python
  async def _validate_access_token(auth: AuthService, token: str) -> dict:
      try:
          payload = auth.decode_token(token)
      except jwt.PyJWTError:
          raise HTTPException(status_code=401, detail="Invalid token")
      if payload.get("type") != "access":
          raise HTTPException(status_code=401, detail="Invalid token type")
      user = await auth.get_user(payload["sub"])
      if not user or not user.get("is_active", True):
          raise HTTPException(status_code=401, detail="User not found or inactive")
      return user
  ```
- `get_current_user` and `get_user_from_token_param` both call this helper.

---

### 6.2 — Fix Rate Limiter Instance in projects.py

**Issue:** `projects.py:19` creates a standalone `Limiter` instead of using the app's.

**Files:** `backend/routes/projects.py`, `backend/app.py`

**Changes:**
- Remove the standalone `_limiter = Limiter(...)` from `projects.py`.
- Import and use the app-level `limiter` from `app.py` (or make it accessible via the container/config).
- Ensure the `@limiter.limit("5/minute")` decorator references the correct instance.

---

### 6.3 — Inject Planner and Decomposer via DI

**Issue:** `projects.py:178,230` uses inline imports instead of DI.

**Files:** `backend/services/planner.py`, `backend/services/decomposer.py`, `backend/routes/projects.py`, `backend/container.py`

**Changes:**
- Convert `generate_plan` and `decompose_plan` from module-level async functions into class methods, matching the pattern used by every other service (`AuthService`, `BudgetManager`, `Executor`, etc.):
  ```python
  class PlannerService:
      def __init__(self, db: Database, budget: BudgetManager, client: AsyncAnthropic):
          self._db = db
          self._budget = budget
          self._client = client

      async def generate_plan(self, project_id: str) -> dict:
          ...  # Move existing generate_plan body here
  ```
  Same for `DecomposerService` wrapping `decompose_plan`.
- Register both as `providers.Singleton` in `container.py`, wired to their dependencies.
- In `routes/projects.py`, inject via `Depends(Provide[Container.planner])` and `Depends(Provide[Container.decomposer])`.
- Remove the inline `from backend.services.planner import ...` imports from route handlers.
- Update `container.wiring` to include any new modules if needed.

---

### 6.4 — Fix Progress Subscriber Cleanup

**Issue:** Empty subscriber lists accumulate. No subscriber limit.

**Files:** `backend/services/progress.py`

**Changes:**
- After removing a queue in `subscribe()`'s finally block, check if the list is empty and delete it:
  ```python
  if queue in subs:
      subs.remove(queue)
  if not subs:
      del self._subscribers[project_id]
  ```
- Add a max subscriber limit per project (e.g., 10). Reject with a log warning if exceeded.

---

### 6.5 — Tighten CORS Configuration

**Files:** `backend/app.py`

**Changes:**
- Replace `allow_methods=["*"]` with `allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"]`.
- Replace `allow_headers=["*"]` with `allow_headers=["Authorization", "Content-Type"]`.

---

### 6.6 — Fix Test Infrastructure

**Files:** `tests/conftest.py`, `tests/unit/test_registry.py`, `tests/integration/test_tasks_api.py`

**Changes:**

**conftest.py:**
- Override `container.tool_registry` with a mock or minimal registry (no real RAG DB access).
- Ensure tests don't depend on `config.json` existing — mock `_config` or set a test config.

**test_registry.py:**
- Use mock tool classes instead of real ones to make it a true unit test.
- Use exact count assertion (`== 6` not `>= 6`).

**test_tasks_api.py:**
- Extract the repeated `container.db()` pattern into a fixture.

**General:**
- Add unauthenticated/unauthorized tests for all protected endpoints.
- Add concurrent operation tests for admin race condition and budget TOCTOU.
- Add tests for circular dependency detection in decomposer.
- Add RAG tool tests with special characters in input.

---

### 6.7 — Fix N+1 Query in list_projects

**Issue:** `projects.py:92` — `_row_to_project` with `include_task_summary=True` runs a `GROUP BY` query per project. For 50 projects, that's 50 extra DB queries.

**Files:** `backend/routes/projects.py`

**Changes:**
- Fetch all task summaries in a single query and join them in Python:
  ```python
  # Single query for all project task summaries
  summary_rows = await db.fetchall(
      "SELECT project_id, status, COUNT(*) as cnt "
      "FROM tasks WHERE project_id IN ({}) GROUP BY project_id, status".format(
          ",".join("?" for _ in project_ids)
      ),
      tuple(project_ids),
  )
  # Build a dict: {project_id: {status: count, ...}}
  summaries = {}
  for row in summary_rows:
      summaries.setdefault(row["project_id"], {})[row["status"]] = row["cnt"]
  ```
- Pass the pre-fetched summary dict to `_row_to_project` instead of having it query per project.

**Tests:** Existing `list_projects` tests should pass. Verify with a mock that only one summary query is issued regardless of project count.

---

### 6.8 — Remove Stale load_config() Function

**Issue:** `config.py:69-120` evaluates all constants at import time as module-level snapshots. `load_config(path)` exists as a public function, implying it can be called again to reload config, but any module that already imported constants (e.g., `from backend.config import HOST`) holds stale values. The function's existence is misleading.

**Files:** `backend/config.py`

**Changes:**
- Make `load_config()` private: rename to `_load_config()`. It's only called once at import time (line 46-47) and should not be re-called.
- Add a comment at the top of the constants section:
  ```python
  # Module-level constants — evaluated once at import time.
  # These are snapshots from _config. Do not call _load_config() after import.
  ```
- If hot-reload is ever needed in the future, the constants should be replaced with `cfg()` calls at point-of-use. But that's a separate task — for now, just close the misleading API.

**Tests:** No new tests needed.

---

### 6.9 — Update Documentation

**Files:** `.claude/architecture.md`, `CLAUDE.md`, `config.example.json`

**Changes:**
- Update architecture doc with:
  - Transaction context manager documentation
  - Ownership model description
  - Exception hierarchy
  - Budget reservation mechanism
  - Updated dependency map
- Update `CLAUDE.md` project structure if new files were added (errors.py, frontend components).
- Add new config keys to `config.example.json` (`execution.max_task_retries`, `ollama.request_timeout` if renamed).
- Update gotchas section with new findings.
- Update memory file (`MEMORY.md`) with phase completion status.

---

## Phase Summary

| Phase | Focus | Items | Scope |
|-------|-------|-------|-------|
| **1** | Security Critical | 1.1–1.7 | 7 items — auth, ownership, SSE tokens, injection, validation |
| **2** | Data Integrity | 2.1–2.6 | 6 items — transactions, races, schema, lifespan |
| **3** | Concurrency | 3.1–3.6 | 6 items — executor, tools, I/O, httpx |
| **4** | Validation | 4.1–4.8 | 8 items — bounds, errors, estimates, decomposer |
| **5** | Frontend | 5.1–5.5 | 5 items — UX robustness |
| **6** | Quality | 6.1–6.9 | 9 items — cleanup, N+1, config, tests, docs |

**Total: 41 items across 6 phases.**

## Dependency Graph

```
Phase 1 (security) ──────────────────────────────┐
                                                  │
Phase 2 (data integrity)                          │
  2.1 transaction() ──► 2.2 admin race            │
                    ──► 4.6 decomposer atomicity  ├──► Phase 6 (quality, tests, docs)
                                                  │
  2.3 budget reservation ──► 3.5 per-round check  │
                                                  │
Phase 3 (concurrency) ───────────────────────────┤
                                                  │
Phase 4 (validation) ────────────────────────────┤
  4.7 exception hierarchy benefits from 1.2       │
                                                  │
Phase 5 (frontend) — fully independent ───────────┘
```

**Parallelism:** Phases 1, 3, and 5 can run concurrently. Phase 2 should complete before Phase 3.5 and Phase 4.6. Phase 6 goes last since it documents and tests all prior changes.
