# Plan 021 — Read-only local plan browser (revision 2, approved)

**Status: implementation GO by codex-hekate (msg 689) on revision 2, with conditions from msgs 689, 693, 694, 703, 705, 708 and 709 (see "Implementation notes" at the end). Implemented by claude-hekate, with final state-update guards hardened by codex-hekate; independently validated and accepted by codex-hekate with consumer review from codex-chatagent (msg 734). Evidence: [022](022-local-plan-browser-validation.md).** Revision 2 folded in codex-hekate's decisions from msg 615 and builds on accepted increment 3b1 (`979d471`; evidence in [020](020-durable-claims-validation.md)).

**Goal:** a person on the local profile can find managed plans and see their tree, readiness, blockers, dependencies and history. **Read-only.** No write controls, generic-writer changes, auth or CORS changes, Odin changes, launcher changes, workers or leases.

## 0. Decisions (msg 615)

| Question | Decision |
|---|---|
| List cursor | UUID: `ORDER BY root_node_id` with `WHERE root_node_id > @cursor`, so paging uses the same PostgreSQL ordering. Not chronological, by design |
| Project filter | Optional |
| Unsupported contract version | **Listed as metadata only.** The UI refuses to load or derive state for it; no tree, readiness or statuses are shown |
| Refresh | An explicit refresh resets the cursor to the first page |
| Vite proxy target | **An environment override is in scope**; the default is unchanged (`http://localhost:5102`). Local docs say 5179 → 5103. Nothing starts the UI automatically or takes ports |
| First UI increment | Discovery and list, an expandable tree, selected-node detail, a dependency map (SVG with a table fallback), and events. **Claim lookup is DEFERRED** to a follow-up, to keep the browser bounded |
| Integers | Full numeric-token validation (section 3) |

## 1. Exact inventory of existing files (read-only, 2026-10-06)

### context-store/ui (tracked files)

| File | Role today | Touched? |
|---|---|---|
| `package.json` | React ^19.2, Vite ^7.3.1, TS ~5.9, Tailwind 4, Playwright ^1.58.2, ESLint 9. Scripts `dev`, `build`, `lint`, `typecheck`, `preview`, `test:e2e*`. No unit runner, no router, no graph library | **No** (no new dependency) |
| `package-lock.json` | lockfile | No |
| `vite.config.ts` | port 5179, `/api` proxy hard-coded to `http://localhost:5102`, `changeOrigin` | **Edit:** environment override (section 2.3) |
| `playwright.config.ts` | `testDir ./e2e`, baseURL `:5179`, chromium, `webServer: npm run dev` (reuses an existing server outside CI) | No |
| `eslint.config.js`, `tsconfig*.json`, `index.html`, `run_ui.bat`, `README.md`, `src/main.tsx`, `src/index.css`, `src/colors.ts` | config, entry and styling | No |
| `src/App.tsx` | header view switch `AppView = 'chat' \| 'workspace'` (l.39, buttons l.311-330); right tabs `stats \| debug \| planner` (l.38, l.428-477) | **Edit:** add `'plans'` to `AppView`, one header button, and one render branch |
| `src/api.ts` | legacy client: `BASE='/api'`, `res.json()` everywhere; `listPlans` / `getPlan` on legacy `/api/plans`, `/api/plan/{id}` (l.276-308) | **No.** The contract client is a separate module, so legacy parsing is unaffected |
| `src/components/PlannerPanel.tsx` | legacy plan list and tree (no dependencies, readiness or history; swallows errors) | **No** |
| `src/components/{ChatPanel, ConversationList, DebugPanel, NodeDetailPanel, StatsBar, ThreadsSidebar, WorkspacePanel}.tsx` | other views | No |
| `e2e/helpers.ts` | `mockBaseAPIs(page)` (`/api/conversations`, SSE `/api/events`), `buildSSE`, `chatResponseSSE`, `mockChatAPI`, `mockConversationAPIs` | **No.** New helpers go in a new file |
| `e2e/{app-layout, chat-flow, conversations}.spec.ts` | existing specs | No; they must keep passing |

**Environment:** `context-store/ui/node_modules` was not installed on fenrir. `npm ci` (from the existing lockfile) and `npx playwright install chromium` are routine verification steps, authorized by codex-hekate (msgs 686 and 689); nothing was added to `package.json` or the lockfile.

### API and store

| File | Today | Touched? |
|---|---|---|
| `context-store/Api/PlanContractEndpoints.cs` | plan-contract group (loopback filter, gate-only), no list route | **Edit:** one GET route and its DTO |
| `context-store/PlanContracts/PlanStore.cs` | `LoadAsync(root)`, `ReadEventsAsync`, `ReadClaimAsync`; no listing | **Edit:** `ListPlansAsync` |
| `context-store/Api/Program.cs` | CORS `:5179` only; gate mapping l.109-110; unrelated **dirty hunk ~l.345 (preserved)** | **No** |
| `context-store/Api/Services/PlanService.cs` | legacy `/api/plans` | **No** |
| `scripts/local/HekateLocal.psm1`, `hekate-local.ps1` | launcher: DB 5434, Api 5103, does not start the UI | **No** |
| `scripts/local/README.md` | says the UI can browse plans (it can't without a manual start and proxy change) | **Edit, docs only:** a correct manual recipe |

## 2. Scope

### 2.1 API: `GET /api/plan-contract/v1/plans?projectId=&afterRootId=&limit=`

- Exists only while the gate is Enabled, behind the existing loopback filter. 404 when the flag is off.
- `PlanStore.ListPlansAsync(Guid? projectId, Guid? afterRootId, int limit)`:
  - one RepeatableRead transaction, always rolled back;
  - no fence flag, no lock, no writes;
  - SQL: `managed_plans m JOIN nodes n ON n.id = m.root_node_id WHERE (@p IS NULL OR m.project_id = @p) AND (@a IS NULL OR m.root_node_id > @a) ORDER BY m.root_node_id LIMIT @limit + 1`.
- Response: `{contractVersion, supported: <this API's version>, plans: [{rootId, projectId, name, defaultGate, contractVersion, supported: bool, createdAt, createdBy, eventSeq}], nextAfterRootId}`. `eventSeq` is the only Int64 field and goes through the section 3 guard on the client.
- Query parsing: `projectId` and `afterRootId` must be uuids. `limit` is an integer 1–500 (default 100) parsed as `NumberStyles.None`. Anything else is 400 `invalid_query`.
- No stored graph is loaded or validated by this route, so a corrupt plan is still listed (as metadata), and opening it gives the existing GET errors.

### 2.2 UI (new files)

| File | Content |
|---|---|
| `ui/src/planContract/json.ts` | `parseContractJson(text)`: the section 3 guard, then `JSON.parse` |
| `ui/src/planContract/api.ts` | A client that only uses GET: `listPlans(cursor?)`, `getPlan(root)`, `getPlanEvents(root, cursorText?)`, `getNodeEvents(node, cursorText?)`. It uses `res.text()` then `parseContractJson`. Errors become typed `{status, code, message}` and are never swallowed. A 404 on the list means "plan contract not enabled on this API" |
| `ui/src/planContract/types.ts` | Types matching the C# DTOs |
| `ui/src/components/ManagedPlansView.tsx` | List (paged, with an explicit Refresh that resets the cursor) and the selected plan. A plan whose version is unsupported shows only its metadata and an "unsupported contract version — not rendered" notice; no plan GET is sent |
| `ui/src/components/managed/PlanTree.tsx` | Expandable hierarchy (sibling order). Leaves show work, effective acceptance (stale highlighted), ready / blocked, upstream changed, and attempt id and epoch. Containers show completion, acceptance and gates hold. Values are displayed exactly as the API returns them, never derived again |
| `ui/src/components/managed/NodeDetail.tsx` | Content (value and content attributes), raw acceptance next to effective acceptance, pins, executor reference, blockers (owner, predecessor, gate and a readable reason for each `BlockerReasons` code), declared dependencies in and out |
| `ui/src/components/managed/DependencyMap.tsx` | SVG layered by longest path over declared dependencies (deterministic: layer, then id). Edges are coloured by gate and blocker state; `data-node-id` and `data-edge` attributes are added for tests. Above 200 nodes it shows a table instead. No library |
| `ui/src/components/managed/EventsTimeline.tsx` | Plan history and selected-node history, with "load more" using the checked cursor text, and `historyStartsAtSeq` / `historyBackfilled` shown verbatim |

### 2.2a Consumer correctness (codex-chatagent msg 624, via lead)

- **Stale-response isolation:** every request carries an `AbortController` and a monotonically increasing request token per view slot: list, plan, plan events and node events. When the user switches plan or node (or refreshes), in-flight requests for the old selection are aborted, and any response whose token is not the slot's latest is **dropped before parsing or rendering**. A slow response for plan A can therefore never paint into plan B, and a node's events can never appear under another node. Event pages append only if they belong to the same `(selection, cursor chain)`. A refresh starts a new chain.
- **Distinct identities:** a dependency edge is identified by `(predecessorId, successorId)` (its declared owner is the successor). A blocker is identified by `(ownerId, predecessorId, gate, reason)`, because an inherited ancestor gate gives the same predecessor through a different owner. The two are never merged or de-duplicated against each other. React keys and `data-edge` / `data-blocker` test attributes use these full tuples, and the map draws a blocker highlight only on the edge whose `(ownerId → predecessorId)` matches. Inherited blockers are listed separately with their owner.
- **Tests (added to section 4 UI):**
  - a delayed response for plan A arrives after selecting plan B, and B's view is unaffected (asserted by fixture content);
  - the same for node events;
  - a fixture where one predecessor blocks through two owners (leaf and ancestor) renders two distinct blocker rows and one dependency edge.

### 2.3 Hosting

- `vite.config.ts`: `target: process.env.HEKATE_UI_API_TARGET ?? 'http://localhost:5102'`. The default behaviour is identical.
- `scripts/local/README.md`, a manual recipe:
  1. start the local profile with `HEKATE_LOCAL_PLAN_CONTRACT=1`;
  2. in `context-store/ui`, run `HEKATE_UI_API_TARGET=http://127.0.0.1:5103 npm run dev`;
  3. open `http://localhost:5179` and choose **Plans**.

  Browser requests go through the proxy, which connects from loopback, so the loopback filter passes. No CORS change is needed.
- Nothing starts the UI automatically, and nothing reserves ports. Hosting the built UI from the Api remains out of scope.

## 3. Integer safety (msgs 592 and 615): fail closed

`parseContractJson(text)` scans the **entire raw text** with a small tokenizer before `JSON.parse`:

1. It tracks string state, including `\"` and `\\` escapes, so digits **inside strings are never treated as numbers** (for example a content value `"12345678901234567890"` is allowed).
2. Every numeric **token** outside strings must match the plain JSON integer grammar `-?(0|[1-9][0-9]*)` and satisfy `|BigInt(token)| ≤ 2^53−1`.
3. Any numeric token with a fraction or an exponent (`.`, `e`, `E`) is rejected as **`unsupported_number`**. The contract has no non-integer numbers, so nothing valid is lost, and an exponent-encoded unsafe value such as `9.007199254740993e15` can never slip through.
4. A malformed document (an unterminated string, or an invalid token as `JSON.parse` sees it) becomes `malformed_json`.
5. Any failure rejects the whole response with a typed error (`unsafe_integer` / `unsupported_number` / `malformed_json`). The view shows the error and renders **nothing** from that response.
6. When the scan passes, `JSON.parse` yields only safe integers, so every `Number` is exact.
7. Cursors: the scanner returns the **checked source text** of `nextAfterSeq`, and the client sends that string unchanged as `afterSeq`. The UUID cursor for plans is a string anyway.

## 4. Tests (required for acceptance)

**Live store** (new `PlanStore.LiveTests/PlanListingLiveTests.cs`):
- managed plans only (an unmanaged `plan` node inserted via the legacy path or raw SQL is absent);
- the `projectId` filter;
- UUID order and the `>` cursor across pages, with no gaps or duplicates (every root seen once);
- a plan with an unsupported version is listed with `supported=false`;
- a corrupt stored graph is still listed;
- read-only: a full-row fingerprint of the plan tables, `event_seq` and the tagged AGE edges is unchanged;
- argument guards.

**HTTP** (`scripts/local/PlanContractApi.Tests.ps1`, new `L` checks):
- flag off: the list route is 404;
- flag on: 200 containing the created plan;
- the unmanaged legacy plan is absent;
- the filter;
- limit and cursor pages;
- 400 `invalid_query` for a bad `limit`, `afterRootId` or `projectId`.

**Parser** (Playwright test runner, Node-side; the spec imports `src/planContract/json.ts` directly, no browser):
- fixtures for:
  - escaped numeric strings (not counters);
  - negative integers;
  - `0`, `-0` and `2^53−1`;
  - plain unsafe integers (`9007199254740993`, `-9007199254740993`);
  - an exponent-encoded unsafe value (`9.007199254740993e15`);
  - a fraction (`1.5`);
  - an unterminated string;
  - malformed JSON;
- each fixture expects either an exact value or the specific typed error;
- the cursor text round-trips byte for byte.

**UI** (new `e2e/managed-plans.spec.ts` and `e2e/managed-helpers.ts`, with mocks recorded from real API response shapes):
- **list:**
  - list, paging, and Refresh resets the cursor;
  - a 404 shows "not enabled"; a 500 shows an error, never an empty list;
  - an unsupported version shows metadata only, with no plan GET intercepted;
- **plan view:**
  - the tree shows exactly the statuses in the fixture (stale highlighted), and expand/collapse works;
  - node detail shows blockers with reasons, and raw and effective acceptance;
- **map:**
  - deterministic `data-` attributes across two loads;
  - the table fallback for a fixture with more than 200 nodes;
- **events:**
  - "load more" sends `afterSeq` exactly as the checked text, verified on the intercepted URL;
- **unsafe responses:**
  - an unsafe `stateRevision` in the plan, an unsafe `nextAfterSeq` in events and an exponent in the list each show the typed error;
  - none of them renders rows or sends a follow-up request;
- **read-only guard:**
  - a `page.route('**/api/**')` spy fails the test on any **non-GET API** request while every managed view is exercised;
  - UI asset and Vite dev-server requests are excluded;
- **regression and static checks:**
  - the existing three specs still pass;
  - `npm run typecheck` and `npm run lint` are clean.

**Optional** (not required): one manual smoke test against the real local Api through the proxy, recorded in the validation doc.

## 5. Not included

- Claim lookup (deferred follow-up).
- Any write UI, leases, executors or workers.
- Auth, CORS, `Program.cs`, the launcher, hosting the built UI from the Api.
- The legacy `PlannerPanel`, `/api/plans`, the orchestration frontend, the VS Code extension.
- Serializer or wire-format changes; new npm dependencies; graph libraries.

## 6. Implementation notes (conditions as built)

- **List wire shape (msg 689):** top level `{contractVersion, plans, nextAfterRootId}`; each item carries `supported: boolean`. There is no top-level `supported` or `supportedContractVersion` field.
- **Request guard (msg 694):** each request carries an `AbortSignal` plus an acceptance callback. The client checks `signal.aborted` and `isCurrent()` **after** `res.text()` and **before** `parseContractJson`, so a superseded response (even an unsafe one) can raise neither a result nor an error. Views re-check inside every functional state updater.
- **Refresh (msg 708):** cancels all four slots (list, plan, plan events, node events), clears the selected plan, node and both histories, resets the cursor and reloads the list.
- **Paging (msg 708):** marked pending synchronously; "Load more" is hidden while a page loads and existing rows are kept; a failed page clears the rows and shows the error.
- **Response validation (msg 703):** every consumed field is checked: type, enum membership (work, gates, decisions, effective and container statuses, blocker reasons, event kinds), counter ranges, plan root and readiness root equal to the requested root, every dependency, blocker, leaf and container id present among the plan's nodes, node-event node id equal to the requested node, strictly ascending `seq`. Anything else is `unexpected_shape`, and the old view is cleared.
- **Cursor capture (msg 703):** property names are decoded exactly as `JSON.parse` sees them (`\uXXXX` escapes included); a repeated key is last-wins and a later non-number clears the capture. The client also asserts that the captured text equals the parsed cursor.
- **Edges vs blockers (msg 689 (3)):** an edge is `(predecessorId, successorId)`; a blocker is `(ownerId, predecessorId, gate, reason)`. Blocking through a leaf and its ancestor are two declared edges, and both are drawn.
- **Status labels (msg 709):** containers show the API roll-up (`completion · acceptance`, plus "gates do not hold" when relevant), never their stored `todo` work, in the tree, map and detail; leaves show `work · effective acceptance`, and stale is shown in amber. The map has a legend. Containers are drawn as dashed boxes in the layered grid (not as group boxes); the bounded layout and >200-node table fallback are unchanged.
- **Test hosting (msg 693):** `vite.config.ts` sets `strictPort: true`. `playwright.config.ts` (an edit not listed in §1's inventory) reads `HEKATE_UI_TEST_PORT` (default 5179) and starts Vite with `--port N --strictPort`. Verification runs with `CI=1`, so an existing dev server is never adopted.
- **Lint baseline (msg 705):** HEAD already has 2 errors and 3 warnings (App.tsx `loadConversation` used before declaration, DebugPanel synchronous setState in an effect, 3 exhaustive-deps warnings). They are not fixed here; the new files lint clean and full lint shows no new diagnostics.
