# Plan 022 — Read-only local plan browser: validation evidence

**Status: accepted by codex-hekate after independent clean-checkout validation and codex-chatagent consumer review (msgs 728/734), 2026-10-06.**

**Date:** 2026-10-06
**Host:** fenrir (Windows 11; Node v24.15.0 at `D:/scoop/apps/nodejs-lts/current`, npm 11.12.1; Playwright Chromium headless shell 145.0.7632.6; Docker Desktop, PostgreSQL 16 + AGE in the owned `hekate-local` container)
**Implementer:** claude-hekate
**Reviewer and acceptance owner:** codex-hekate

Scope and conditions: [021](021-local-plan-browser-proposal.md) (revision 2, GO msg 689; "Implementation notes" record each condition as built). Base: accepted 3b1 commit `979d471`.

## Results (implementer's runs)

| Suite | Command | Result |
|---|---|---|
| Pure rules | `dotnet test context-store/PlanContracts.Tests` | 172 / 172 (unchanged) |
| Live store | `HEKATE_PLAN_LIVE_CONNSTR=… dotnet test context-store/PlanStore.LiveTests` | **51 / 51** (48 before; 3 new listing tests) |
| Api process (HTTP) | `pwsh scripts/local/PlanContractApi.Tests.ps1` | **66 / 66** (59 before; 7 new: `A list route absent`, 6 `L` checks), exit 0, database verified dropped |
| UI end-to-end and parser | `cd context-store/ui; $env:CI=1; npx playwright test --retries=0` | **49 / 49**: the 17 existing specs + 7 parser tests + 25 managed-plan UI tests |
| UI typecheck | `npm run typecheck` | clean |
| UI lint, new files | `npx eslint src/planContract src/components/ManagedPlansView.tsx src/components/managed e2e/managed-*.ts vite.config.ts playwright.config.ts` | clean |
| UI lint, full | `npm run lint` | **baseline only**: the same 2 errors + 3 warnings as HEAD `979d471` (see below); no new diagnostics |
| UI build | `npm run build` | succeeds (`dist/` is gitignored) |
| Real-Api smoke (one-off, not in the repo) | real Api on a new disposable `hekate_plan_api_*` database (port 5107), seeded through the contract API (plan, phase, 3 tasks, 2 edges, start/finish/accept, a claim); Vite on 5189 `--strictPort` proxied to it; one Playwright test | **1 / 1**: real responses pass every client validator and render the list, tree (`accepted` leaf), map (container roll-up label), node history (3 events) and plan history (4 events) with GET-only traffic. Database dropped and verified; Api process exited |

**Lint baseline exception (msg 705).** HEAD `979d471` already fails `npm run lint` with:
- `src/App.tsx` "Cannot access variable before it is declared" (`loadConversation`, react-hooks/immutability), now reported at line 152 instead of 151 because of one added import line;
- `src/components/DebugPanel.tsx:30` synchronous setState in an effect;
- three `react-hooks/exhaustive-deps` warnings (App ×2, ChatPanel ×1).

These are legacy and out of scope. They were not fixed or hidden. The new files lint clean.

**Isolation:**
- Every database used was new and disposable (`hekate_plan_live_*`, `hekate_plan_api_*`) and was dropped.
- Vite test servers used strict ports with `CI=1`, so an existing server is never adopted.
- No production host, NSSM service, worker, model or provider was touched, and no legacy component, `Program.cs`, the launcher, `package.json` or the lockfile was edited.
- The temporary smoke files were staged in `context-store/ui/.smoke-021/` and removed after the run.

## Acceptance criteria and evidence

| Criterion (021 rev 2 and msgs 615, 624, 689, 693, 694, 703, 705, 708, 709) | Evidence |
|---|---|
| Listing endpoint: managed plans only, optional project filter, `ORDER BY root_node_id` with a strict `>` uuid cursor in PostgreSQL's own order, `limit` 1–500 | Live `Lists_only_managed_plans_with_project_filter_and_a_gapless_uuid_cursor` (pages 2+2+1 compared with `SELECT … ORDER BY root_node_id`; an unmanaged plan node and another project's plan are absent); HTTP `L list 200 managed only, database order`, `L cursor pages` |
| Read-only listing; unsupported and corrupt plans are listed as metadata | Live `Unsupported_and_corrupt_plans_are_listed_as_metadata_and_listing_writes_nothing` (full-row fingerprint and tagged AGE edges unchanged); HTTP `L unsupported version listed as metadata` |
| Wire shape `{contractVersion, plans, nextAfterRootId}` with per-item `supported`, and no top-level `supported` | HTTP `L item shape` (asserts neither `supported` nor `supportedContractVersion` at the top level) |
| Query guards; flag off means no route | HTTP `L invalid query 400` (5 variants), `A list route absent`; live `Listing_guards_its_arguments…` |
| Unmanaged plans created through the legacy HTTP writers are absent | HTTP `L legacy unmanaged plans created` (`POST /api/project/{p}/nodes` and `POST /api/node/{id}/children`, both `plan`), then absent from the list |
| Integer guard: string- and escape-aware; plain safe integers only; unsafe values, fractions and exponents rejected; malformed input rejected | Parser specs (7): negatives, `0`/`-0`, ±(2^53−1); numeric and escaped strings; ±9007199254740993 and deep nesting; `9.007199254740993e15`, `1e3`, `1E+2`, `1.5`, `-0.0`; unterminated, `01`, `tru`, bad escape, trailing comma, empty, HTML |
| Cursor text exact; escaped and duplicate keys resolved as `JSON.parse` does (msg 703) | Parser `the top-level cursor is captured…`, `escaped and duplicate cursor keys…`; UI `events "load more" sends the exact checked cursor text` (`afterSeq=9007199254740990` on the intercepted URL); the client asserts the captured text equals the parsed value |
| Unsafe responses fail closed with no rows and no follow-up request | UI `unsafe integers fail closed in the list, a plan, and an event cursor…`, `an unsafe event cursor shows an error and no rows` |
| Full shape validation of consumed fields, enums, ranges and cross-references; old view cleared | UI `wrong-shape, wrong-root and unknown-enum plan responses fail closed…` (missing field, wrong root, unknown blocker reason, dangling edge), `node history containing another node's event fails closed`, `a contract version mismatch in a plan response is refused` |
| Unsupported version: metadata only, no plan GET; error-bearing or failing plan after a good one clears the view | UI `lists managed plans and shows the unsupported one as metadata only` (no request for that root), `switching from a good plan to an error-bearing or failing plan clears the old view` |
| Stale-response isolation (msgs 624 and 694) | UI `a slow response for plan A never paints into plan B (even an unsafe one)`, `a slow node history never appears under another node`, `the client drops a superseded response before parsing it` (the acceptance check alone, with a never-aborted signal, yields `StaleResponseError`, not `unsafe_integer`) |
| Refresh cancels every slot and starts a new chain (msg 708) | UI `Refresh cancels held plan and history requests; their late results are ignored` |
| Pending paging, no duplicate pages, errors clear rows (msg 708) | UI `list paging is pending while a page loads…`, `events paging is pending…`, `a failed next page clears prior rows and shows the error` |
| Edges and blockers distinct; blocking through a leaf and its ancestor draws both declared edges (msg 689 (3)) | UI `node detail keeps blockers through the leaf and its ancestor distinct from declared edges` (2 blocker rows with full tuples, 1 incoming edge, 3 map edges as declared) |
| Container labels use the API roll-up; stale leaves are explicit (msg 709) | UI `map and detail label containers by their API roll-up…` (`complete · accepted` for a container whose stored work is `todo`; `done · stale` in amber; container detail shows completion and acceptance and no Work field) |
| Tree, detail and pins; statuses verbatim | UI `the tree shows API statuses verbatim…`, `node detail shows raw and effective acceptance and pins` |
| Deterministic bounded map and table fallback | UI `the map layout is deterministic across loads`, `above 200 nodes the map falls back to the edge table` |
| List errors are distinct from empty | UI `a 404 list means the contract is disabled…`, `a 500 list shows its error code` |
| Read-only: managed views send GET only (UI asset and dev-server requests excluded) | `afterEach` guard on every managed spec, plus `every managed view is read-only (GET only)` |
| Existing UI unaffected | the 17 existing specs pass |

## Files

**New:**
- `context-store/ui/src/planContract/{json,api,types,requestSlot}.ts`
- `context-store/ui/src/components/ManagedPlansView.tsx`
- `context-store/ui/src/components/managed/{PlanTree,NodeDetail,DependencyMap,EventsTimeline}.tsx`
- `context-store/ui/src/components/managed/{labels,layout,eventsState}.ts`
- `context-store/ui/e2e/{managed-json.spec,managed-plans.spec,managed-helpers}.ts`
- `context-store/PlanStore.LiveTests/PlanListingLiveTests.cs`
- this document

**Edited:**
- `context-store/ui/src/App.tsx` (one import, `'plans'` view, one header button, one render branch)
- `context-store/ui/vite.config.ts` (`HEKATE_UI_API_TARGET` override, `strictPort`)
- `context-store/ui/playwright.config.ts` (`HEKATE_UI_TEST_PORT`, `--strictPort`)
- `context-store/PlanContracts/PlanStore.cs` (`ListPlansAsync`)
- `context-store/Api/PlanContractEndpoints.cs` (one GET route)
- `scripts/local/PlanContractApi.Tests.ps1` (`A list route absent` and the `L` section)
- `scripts/local/README.md` (manual UI recipe)
- `context-store/plans/021-local-plan-browser-proposal.md` (status and implementation notes)

## Known limits

- Claim lookup is deferred (021 §0).
- The launcher does not start the UI. The recipe is manual, and hosting the built UI from the Api is out of scope.
- Any future contract field, enum value or blocker reason makes the browser fail closed (`unexpected_shape`) until the client is updated. This is intended.
- Counters above 2^53−1 are refused, never displayed (msg 592). A lossless wire format remains future work.
- The local profile's `code_storage` database was not used; all evidence comes from disposable databases.

## Independent acceptance (codex-hekate)

Reviewed only the owned files over clean HEAD `979d471` in `D:/hekate-browser-review-mz_6czag`; preserved unrelated workspace changes. Windows Node 24.15.0, locked npm dependencies, and the same Chromium cache. Independent results: 172 pure rules, 51 live store, 66 HTTP checks, and the initial 49 browser/parser tests passed with no skips. Typecheck/build and scoped lint passed. Full lint reproduced the HEAD baseline of 2 errors and 3 warnings.

Consumer review identified one final compliance gap: async plan state setters checked their generation outside the queued update. The acceptance owner added guarded functional updates to all four async plan branches, plus malformed-response cases for root switching, never-aborted stale client responses, and Refresh. Final rerun passed **51 repository browser/parser tests plus one temporary desktop screenshot check (52 total)**, typecheck, scoped lint, and build. Desktop tree and map screenshots were inspected; the temporary visual spec is outside the source repository. No C# or HTTP behavior changed after those suites passed.

All 23 owned source/test files match the tested clean overlay. The isolated HTTP harness differed only in its verified original-workspace container label so it could use the owned local database while building clean sources. Both live and HTTP fixtures cleaned up their disposable databases. Consumer verdict msg 734 accepted the read-only browser after the guard fix, with no additional blockers.
