# Plan 005 — Planner View

<plan level="L2" task="Add plan contract, API endpoints, and Planner panel to the ideation assistant UI">

<context>
## Current State

The ideation assistant has a three-panel layout:
- Left: ThreadsSidebar (extracted ideas by status)
- Center: ChatPanel (streaming conversation)
- Right: Tabbed Stats + Debug panels

Plans exist in the DB as node trees (plan → plan_phase → plan_step → task, plus risk/question/test_spec/retrospective siblings). PlanRenderer.cs can render them as text. But there's no UI panel to browse, navigate, or inspect plans.

The planning node types are flat — no distinction between plan types, no goals/conditions, no external blockers, no milestones. The existing 4 seeded plans (001-004) use the current schema.

## Relevant Files
- `ui/src/App.tsx` — layout, tab routing
- `ui/src/components/ThreadsSidebar.tsx` — closest pattern to plan tree view
- `Api/Program.cs` — endpoint registration
- `DbLayer/NodeRepository.cs` — GetSubtree, GetChildren, GetAllNodes
- `ContextRouter/NodeTypes.cs` — type constants
- `Renderer/PlanRenderer.cs` — text rendering
- `SeedData/PlanSeeder.cs` — existing plan seed data

## Baseline
- 4 plans seeded in DB (001-004)
- 0 API endpoints for plan data
- 0 UI for plan viewing
- 8 planning node types (plan, plan_phase, plan_step, task, risk, test_spec, revision, retrospective)
</context>

<thinking>
## Key Design Decisions

**Plan contract (from dual Claude + Gemini review):**
- Plan node IS the objective (no separate objective type — one plan = one goal)
- failure_condition dropped: use risk with severity + abort_trigger attributes instead
- blocker is a separate type from risk (different attribute sets, not all blockers start as risks)
- milestone at plan-level (not under plan_phase — milestones span phases)
- No root_plan_id denormalization (4-level trees, recursive CTE is trivial)
- target_date replaces time_horizon (concrete > subjective)
- 4 plan types: feature, bugfix, roadmap, spike
- key_result, assumption, constraint deferred — add when real usage demands them

**UI approach:**
- New tab "Planner" in the right panel (alongside Stats and Debug)
- Plan list view → expand to tree view (like ThreadsSidebar but hierarchical)
- Reuse existing patterns: fetch from API, render as typed cards with status badges
- No edit/create UI yet — read-only view of DB plans. Editing comes later.

**API approach:**
- GET /api/plans — list all plans (lightweight: id, name, plan_type, status, priority)
- GET /api/plan/{id} — full subtree as nested JSON tree
- Reuse NodeRepository.GetSubtree() — already loads full tree in 2 queries

**Why not a separate page/route:** The three-panel layout is the whole app. Adding a tab keeps everything in view — you can chat about a plan while looking at it. SPA routing would be overkill for this POC.
</thinking>

<approach>
## Steps

### Step 1: Add new node types and attributes to schema
Add to NodeTypes.cs: `blocker`, `milestone`
Document new attributes: `plan_type`, `priority`, `target_date`, `severity` (on risk), `abort_trigger` (on risk)
No DB migration needed — types are TEXT strings, attributes are key-value pairs.

### Step 2: Add plan API endpoints
- `GET /api/plans` — query all plan nodes in project, return lightweight list
- `GET /api/plan/{id}` — call GetSubtree(), serialize as nested JSON tree with attributes
Add PlanService.cs to handle tree → DTO conversion.

### Step 3: Add API types to frontend api.ts
- `PlanSummary` interface (id, name, planType, status, priority, targetDate)
- `PlanNode` interface (id, nodeType, name, value, status, attributes, children)
- `listPlans()` and `getPlan(id)` fetch functions

### Step 4: Build PlannerPanel.tsx component
- Plan list: cards showing name, type badge, status, priority
- Click plan → load full tree → render as collapsible tree view
- Node type icons and colors (matching existing icon patterns)
- Status badges (reuse existing status color scheme)
- Expand/collapse at every level
- Back button to return to plan list

### Step 5: Wire PlannerPanel into App.tsx
- Add "Planner" tab to the right panel tab bar
- Import PlannerPanel, render when tab active
- No conversation dependency — plans are project-scoped

### Step 6: Update Plan005Seeder with enriched plan
Create a new seeded plan that uses the full contract:
- plan with plan_type, priority, target_date attributes
- plan_phase nodes with statuses
- plan_step → task hierarchy
- risk nodes with severity and abort_trigger
- blocker nodes
- milestone nodes at plan level
- decision and question nodes at various levels

### Step 7: Build, verify, test
- Build API
- Seed enriched plan (or verify existing plans render)
- Verify Planner tab shows plan list
- Verify plan tree expands with correct types and statuses
- Playwright test: navigate to Planner tab, verify plan tree renders
</approach>

<outputs>
| File | Action | What Changes |
|------|--------|--------------|
| `ContextRouter/NodeTypes.cs` | Modify | Add Blocker, Milestone constants |
| `Api/Services/PlanService.cs` | Create | Tree → DTO conversion, plan list query |
| `Api/Program.cs` | Modify | Add GET /api/plans and GET /api/plan/{id} endpoints |
| `ui/src/api.ts` | Modify | Add PlanSummary, PlanNode interfaces and fetch functions |
| `ui/src/components/PlannerPanel.tsx` | Create | Plan list + tree view component |
| `ui/src/App.tsx` | Modify | Add Planner tab to right panel |
| `SeedData/Plan005Seeder.cs` | Create | Enriched plan with new types/attributes |
| `tools/test_ui_planner.js` | Create | Playwright test for planner tab |
| `CLAUDE.md` | Modify | Update node types, project structure, plan contract docs |
</outputs>

<testing>
| Step | Type | Verification |
|------|------|-------------|
| 1 | Unit | NodeTypes.cs compiles, constants accessible |
| 2 | Integration | `curl localhost:5102/api/plans` returns plan list; `curl localhost:5102/api/plan/{id}` returns tree |
| 3 | Unit | TypeScript compiles (npx tsc --noEmit) |
| 4-5 | Visual | Planner tab visible, plan list renders, tree expands |
| 6 | Integration | Seeded plan appears in /api/plans with correct attributes |
| 7 | E2E | Playwright: open app → click Planner tab → verify plan cards → click plan → verify tree nodes |
</testing>

<questions>
### Q1: Right panel vs dedicated panel?
**Ask:** Should Planner be a tab in the right panel (256px wide) or replace the left sidebar when active?
**Proposed:** Start as a right-panel tab. If it feels cramped for deep trees, we can promote it to a wider panel later. The tab approach ships faster and keeps the chat visible.

### Q2: Seed new plan or just render existing ones?
**Ask:** Should we create a Plan005Seeder with the enriched contract, or just render the 4 existing plans first?
**Proposed:** Do both. Render existing plans immediately (they'll show with the original schema), AND seed one enriched plan to exercise the new types (blocker, milestone, severity, etc.).
</questions>

<risks>
### R1: Existing plans don't have new attributes
**Assumption:** Existing seeded plans (001-004) won't have plan_type, priority, etc.
**Blast radius:** They'll render fine but show "—" for missing attributes. Low risk.
**Rollback:** N/A — graceful degradation.

### R2: Tree depth rendering in 256px
**Assumption:** Plan trees can be 4+ levels deep. At 16px indent per level, that's 64px+ indent in a 256px panel.
**Blast radius:** Deep nodes get clipped or wrapped awkwardly.
**Mitigation:** Truncate node names, use compact indent (12px), add horizontal scroll. If still bad, promote to wider panel.
</risks>

</plan>

## Plan Contract (reference)

```
plan (plan_type: feature|bugfix|roadmap|spike, priority: p0|p1|p2, target_date?, status)
├── key_result*       # Measurable success condition (deferred — add when needed)
├── milestone         # Intermediate checkpoint (AGE edges to steps it spans)
├── plan_phase "GATHER|PLAN|APPROVE|EXECUTE|CLOSE_OUT" (status)
│   ├── plan_step (status)
│   │   ├── task (status)
│   │   ├── risk (severity: low|medium|high|critical, abort_trigger?: true)
│   │   │   └── question (proposed_answer?, status)
│   │   └── blocker (status: blocking|resolved, owner?)
│   └── plan_step ...
├── plan_phase "CLOSE_OUT"
│   ├── test_spec (test_type: unit|integration|manual, status)
│   └── retrospective (status)
├── risk              # Plan-level risks
├── decision          # ADR: why we chose X (reused from ideation domain)
└── question          # Plan-level open questions

* Deferred types (add when real usage demands):
  key_result, assumption, constraint
```

### Design Rules
1. **Parent-child = composition** — delete parent cascades to children
2. **AGE edges = cross-node relationships** — BLOCKS, DEPENDS_ON, RELATES_TO for inter-plan links
3. **blocker = external waits** — AGE DEPENDS_ON for inter-node dependencies
4. **Questions/risks/blockers live at whatever tree level they relate to** — parent IS the context
5. **One plan = one objective** — plan node's name/value fields carry the goal
6. **Plan types:** feature (default), bugfix, roadmap, spike
7. **Lifecycle:** GATHER → PLAN → APPROVE → EXECUTE → CLOSE_OUT (5 phases)
