# Plan 004: Break Uncommitted Work into Atomic Commits

The project has accumulated +1,011 lines across 10 modified files and ~3,100 lines across 18 new files — all uncommitted. This plan breaks the work into logical, atomic commits that each compile and make sense in isolation.

```xml
<plan level="L2">
  <context>
    The repository has a single initial commit (d316dc6) with the core POC.
    Since then, three major workstreams happened without committing:

    1. **Plan 003 hardening fixes** — security, locking, connection patterns, vector dims
    2. **Plan 002 implementation** — plans-as-nodes, conversations, renderers, context router
    3. **Documentation updates** — CLAUDE.md, DESIGN.md expanded to cover new domains

    Modified tracked files (10):
    - AgentCoordination/SubtreeLock.cs (+81/-26) — IAsyncDisposable, LockConnection, try/catch
    - BuildRunner/DotnetBuilder.cs (+7/-2) — concurrent stdout/stderr reads
    - CLAUDE.md (+93/-2) — new sections for node types, edge labels, conventions, context router
    - DESIGN.md (+91/-0) — sections 6-9 on unified model, plans, conversations, context router
    - DbLayer/NodeRepository.cs (+447/-174) — external conn pattern, transactions, 6 new methods
    - DbLayer/Schema.cs (+1/-1) — vector(1536) → vector(768)
    - GraphLayer/AgeLayer.cs (+32/-4) — edge whitelist, EscapeCypher, MERGE fix
    - Program.cs (+241/-13) — plan/conversation/context-router demo sections, env var conn string
    - SeedData/Vector2Seeder.cs (+6/-6) — 1536 → 768 dimensions
    - init.sql (+12/-0) — ideation/planning edge labels

    New untracked files (18):
    - CodeStoragePoc.sln (35 lines)
    - ContextRouter/*.cs (5 files, ~988 lines) — Types, IntentClassifier, ContextAssembler, PromptBuilder, NodeTypes
    - Renderer/*.cs (2 files, ~333 lines) — PlanRenderer, ConversationRenderer
    - SeedData/PlanSeeder.cs (288 lines), Plan002Seeder.cs (159), Plan003Seeder.cs (181), ConversationSeeder.cs (233)
    - plans/*.md (4 files, ~669 lines) — backup markdown plans
    - research/*.md (3 files, ~303 lines) — research findings
  </context>

  <thinking>
    The changes span three logical workstreams, but they have interdependencies:

    - **Plan 003 fixes** (SubtreeLock, DotnetBuilder, AgeLayer, Schema, Vector2Seeder, NodeRepository
      connection pattern, init.sql edge labels) are foundational — later code depends on these.
    - **NodeRepository new methods** (GetParent, GetChildren, GetSiblings, GetNodeWithAttributes,
      LoadAttributes, GetAllNodes) are needed by Renderers and ContextRouter.
    - **Renderers** depend on NodeRepository new methods but not on ContextRouter.
    - **Seeders** depend on NodeRepository and Renderers (for the demo loop).
    - **ContextRouter** depends on NodeRepository new methods and NodeTypes.
    - **Program.cs** depends on everything — each demo section references specific seeders/renderers/router.
    - **Docs** (CLAUDE.md, DESIGN.md) describe the final state and should come last.

    The cleanest split is by dependency order:

    Commit 1: Plan 003 security/hardening fixes (no new features, just making existing code robust)
    Commit 2: NodeRepository expansion (new query methods needed by all later features)
    Commit 3: Renderers (PlanRenderer + ConversationRenderer — standalone components)
    Commit 4: Plan seeders (PlanSeeder, Plan002Seeder, Plan003Seeder — need NodeRepository)
    Commit 5: Conversation seeder (ConversationSeeder — needs NodeRepository)
    Commit 6: Context router (ContextRouter/* — needs NodeRepository new methods)
    Commit 7: Program.cs demo sections + env var conn string (ties everything together)
    Commit 8: Documentation (CLAUDE.md, DESIGN.md — describes final state)
    Commit 9: Ancillary files (sln, plans/*.md, research/*.md)

    However, commits 4-5 could merge (both are seeders), and 8-9 could merge (both are docs/reference).
    Also, the sln file is useful to add early so the project opens in IDEs.

    Refined plan — 7 commits:

    C1: Plan 003 hardening (SubtreeLock, DotnetBuilder, AgeLayer, Schema, Vector2Seeder, init.sql)
    C2: NodeRepository expansion (connection pattern + new methods)
    C3: Renderers (PlanRenderer + ConversationRenderer)
    C4: Seeders (PlanSeeder, Plan002Seeder, Plan003Seeder, ConversationSeeder)
    C5: Context router (all 5 files in ContextRouter/)
    C6: Program.cs demo loop (env var, plan demos, conversation demo, context router demo)
    C7: Documentation + project/reference files (CLAUDE.md, DESIGN.md, sln, plans/*.md, research/*.md)

    Risk: Program.cs imports ContextRouter and Renderer, so it won't compile until C3+C5 are done.
    But C6 stages Program.cs after those. Each commit builds on prior ones.

    Subtlety: Program.cs currently has `using CodeStoragePoc.ContextRouter;` and
    `using CodeStoragePoc.Renderer;` in the diff. The OLD Program.cs (before diff) doesn't have
    those — so as long as we don't stage Program.cs until after C3+C5, it stays compilable.

    The env var change (const → Environment.GetEnvironmentVariable) is a Plan 003 fix (hardcoded
    credentials). It's entangled with the new using statements on adjacent lines. Splitting via
    `git add -p` is possible but fragile. Simpler: put Program.cs entirely in C6 and note the
    env var fix in the commit message.

    SubtreeLock's IAsyncDisposable addition is backward-compatible — callers that don't use
    `await using` are unaffected. So C1 is safe without Program.cs changes.
  </thinking>

  <approach>
    Each commit listed below is self-contained and should not break `dotnet build`.
    Use `git add` with specific files per commit. For Program.cs, stage it whole in C6.

    **C1: Harden SubtreeLock, DotnetBuilder, AgeLayer (Plan 003 security fixes)**
    Files: AgentCoordination/SubtreeLock.cs, BuildRunner/DotnetBuilder.cs,
           GraphLayer/AgeLayer.cs, DbLayer/Schema.cs, SeedData/Vector2Seeder.cs, init.sql
    What: IAsyncDisposable + LockConnection on SubtreeLock, concurrent stdout/stderr in
          DotnetBuilder, edge type whitelist + EscapeCypher + MERGE fix in AgeLayer,
          vector(1536)→vector(768) in Schema + Vector2Seeder, ideation edge labels in init.sql
    Message: "Harden locking, Cypher injection, and vector dimensions (Plan 003)"

    **C2: Expand NodeRepository with connection pattern and tree-navigation methods**
    Files: DbLayer/NodeRepository.cs
    What: External connection + transaction constructor, GetConnection() pattern,
          transactional InsertNode, GetParent, GetChildren, GetSiblings,
          GetNodeWithAttributes, LoadAttributes (batch), GetAllNodes
    Message: "Add external-connection pattern and tree-navigation queries to NodeRepository"

    **C3: Add plan and conversation renderers**
    Files: Renderer/PlanRenderer.cs, Renderer/ConversationRenderer.cs
    What: PlanRenderer walks plan subtree → readable text with status badges.
          ConversationRenderer walks conversation subtree → threaded transcript.
    Message: "Add PlanRenderer and ConversationRenderer for node-tree output"

    **C4: Add plan and conversation seed data**
    Files: SeedData/PlanSeeder.cs, SeedData/Plan002Seeder.cs, SeedData/Plan003Seeder.cs,
           SeedData/ConversationSeeder.cs
    What: Four seeders that populate the DB with sample plans and a conversation as nodes.
    Message: "Add seed data for plans (001-003) and sample conversation"

    **C5: Add context router for intent-driven prompt assembly**
    Files: ContextRouter/Types.cs, ContextRouter/IntentClassifier.cs,
           ContextRouter/ContextAssembler.cs, ContextRouter/PromptBuilder.cs,
           ContextRouter/NodeTypes.cs
    What: Pattern-based intent classification, DB-query-per-intent context assembly,
          XML prompt builder, node type constants.
    Message: "Add context router: intent classification, context assembly, prompt building"

    **C6: Wire up full demo loop in Program.cs**
    Files: Program.cs
    What: Env var connection string (CODESTORAGE_CONNSTR), plan-as-nodes demo (seed + render),
          Plan 002 + 003 demos, conversation demo, context router demo with 5 test inputs,
          agent context navigation demo. Renumber sections 9-13.
    Message: "Expand demo loop with plans, conversations, and context router"

    **C7: Update documentation and add project/reference files**
    Files: CLAUDE.md, DESIGN.md, CodeStoragePoc.sln, plans/*.md, research/*.md
    What: CLAUDE.md gets node types, edge labels, context router docs, connection patterns,
          known issues. DESIGN.md gets sections 6-9. Solution file for IDE support.
          Plan markdown backups and research docs added as reference.
    Message: "Update docs for unified node model, add plans and research reference files"
  </approach>

  <outputs>
    | File | Commit | Action |
    |------|--------|--------|
    | AgentCoordination/SubtreeLock.cs | C1 | modify |
    | BuildRunner/DotnetBuilder.cs | C1 | modify |
    | GraphLayer/AgeLayer.cs | C1 | modify |
    | DbLayer/Schema.cs | C1 | modify |
    | SeedData/Vector2Seeder.cs | C1 | modify |
    | init.sql | C1 | modify |
    | DbLayer/NodeRepository.cs | C2 | modify |
    | Renderer/PlanRenderer.cs | C3 | create |
    | Renderer/ConversationRenderer.cs | C3 | create |
    | SeedData/PlanSeeder.cs | C4 | create |
    | SeedData/Plan002Seeder.cs | C4 | create |
    | SeedData/Plan003Seeder.cs | C4 | create |
    | SeedData/ConversationSeeder.cs | C4 | create |
    | ContextRouter/Types.cs | C5 | create |
    | ContextRouter/IntentClassifier.cs | C5 | create |
    | ContextRouter/ContextAssembler.cs | C5 | create |
    | ContextRouter/PromptBuilder.cs | C5 | create |
    | ContextRouter/NodeTypes.cs | C5 | create |
    | Program.cs | C6 | modify |
    | CLAUDE.md | C7 | modify |
    | DESIGN.md | C7 | modify |
    | CodeStoragePoc.sln | C7 | create |
    | plans/001-voice-ideation-assistant.md | C7 | create |
    | plans/002-ideation-nodes-and-context-router.md | C7 | create |
    | plans/003-review-fixes.md | C7 | create |
    | plans/004-ideation-assistant-test-app.md | C7 | create |
    | research/001-voice-ai-stack.md | C7 | create |
    | research/002-architecture-review.md | C7 | create |
    | research/003-consolidated-review.md | C7 | create |
  </outputs>

  <testing>
    Build verification is the primary test. The project has no unit test suite yet.

    - **After C1**: `dotnet build` should pass — changes are backward-compatible
      (IAsyncDisposable added, vector dims fixed, edge labels added).
    - **After C2**: `dotnet build` should pass — new methods added, no callers yet
      in staged code. Existing callers (Program.cs) still use old patterns.
    - **After C3**: `dotnet build` should pass — renderers are standalone, no callers
      in staged code yet.
    - **After C4**: `dotnet build` should pass — seeders reference NodeRepository
      (already committed in C2). No callers yet.
    - **After C5**: `dotnet build` should pass — context router references NodeRepository
      (C2). No callers yet.
    - **After C6**: `dotnet build` should pass — Program.cs uses all prior components.
      This is the first commit where everything is wired together.
    - **After C7**: `dotnet build` should pass — docs and sln don't affect compilation.

    Note: Build verification requires Docker running with Postgres. If Docker is not
    running, `dotnet build` still compiles — it just can't run.
  </testing>

  <questions>
    1. **Should plans/004-ideation-assistant-test-app.md be included?**
       It exists in the untracked files but isn't mentioned in CLAUDE.md's project structure.
       Proposed: Include it in C7 with the other plan files — it's part of the project record.

    2. **Should C6 (Program.cs) be split further?**
       The env var connection string fix is technically a Plan 003 item, but it's on adjacent
       lines to the new `using` statements. Splitting via `git add -p` is possible but fragile.
       Proposed: Keep Program.cs as one commit in C6, note the env var fix in the message.

    3. **Should the sln file go in C1 or C7?**
       It's useful early for IDE users, but it's orthogonal to all code changes.
       Proposed: C7 (with docs) to keep code commits focused.
  </questions>
</plan>
```
