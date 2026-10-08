# Plan 049 — task navigation, views and filters

Status: implemented and verified, 2026-10-08. User direction: navigate active tasks,
saved tasks and history, with multiple views and filters.

The application header adds **Tasks** alongside Chat, Workspace and Plans.
`TaskNavigator.tsx` reads the existing plan-contract API; Hekate remains the source
of saved task state. No schema change or task execution control is introduced.

## Collections and views

- **Saved:** every stored executable leaf in the loaded plans, including completed
  and cancelled work. This is not a separate draft or bookmark store.
- **Active:** leaves whose current work is `in_progress`. It does not claim that
  a worker process is alive.
- **List / Board:** the same filtered task collection. Board columns group by
  current work state; known codes have readable labels, unknown codes retain
  their original text.
- **History:** recorded leaf-task events, sorted newest first among the loaded
  pages. Filters for status, review and readiness use current task state; the event
  kind filter uses the historical event. Per-plan paging uses the exact checked
  cursor supplied by the API. Compacted history boundaries remain visible.

Filters combine plan, task/plan/attempt text, work status, effective review
decision and ready/blocked state. Blocked means the API returned blockers; it is
not inferred from a task being unready. Clear filters keeps the chosen collection
and layout.

Selecting a task loads fresh plan detail and automatically opens its current
attempt trace when an attempt is recorded. Selecting a history event opens that
event's attempt, including an older attempt. The conversation appears above plan
tree/map/history in Tasks detail, with node facts and attempts alongside it.
Back to filtered tasks cancels detail requests and retains catalog filters and
layout. The existing Plans browser retains its original navigation behavior.

## Reads, pagination and failures

Catalog discovery loads 20 plans per page and reads at most four plans
concurrently. Unsupported and invalid plans are explicitly unavailable and do not
contribute invented tasks. Counts and filters cover loaded plans, with a visible
Load more plans control when additional plans exist. The catalog is not a global
server-side search index.

History loads only when requested, at most four plans concurrently, initially up
to 100 events per plan. Each plan exposes Load more history while a cursor exists.
Refresh and catalog expansion start a new history page chain. Errors are visible
and retryable; a failed additional history page keeps the previously read events.
Request slots abort superseded/unmounted work and discard late responses before
they can update a different selection.

The view sends only GET requests and requires explicit Refresh tasks. Filters are
retained while inspecting a task and returning, but not persisted across page
reloads or leaving the Tasks application view. Task creation, saved drafts,
execution controls and automatic live refresh remain separate work.

## Validation

`context-store/ui/e2e/task-navigation.spec.ts` covers cross-plan discovery, active
membership, shared board/list filters, combined filters, detail/trace navigation,
historical-attempt selection, both paging paths, unavailable plans, stale replies
and retryable history failures. Every test checks that API requests remain GET.

The full UI browser suite passed 81 tests, including eight new task-navigation
tests. Type checking and the production build passed. Changed navigation modules
and tests pass ESLint; the full UI lint still has two pre-existing errors and three warnings in
App.tsx, ChatPanel.tsx and DebugPanel.tsx.

A live read-only capture of the retained local Hekate plans is preserved under
`D:/hekate-coordinator/view-task-navigation-001`: saved list, board, history and
the selected Codex node's verified conversation. The manifest records 18 API
requests, all GET. The preview on port 5193 uses the existing viewer API on 5111.
