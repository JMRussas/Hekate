<![CDATA[# Plan 003 — Review Fixes (Hardening Pass)

<plan level="L3" task="Fix critical bugs and security issues from consolidated review">

  <context>
    CodeStoragePoc is a working POC with 13-step demo that proves: code generation, plan storage,
    conversation storage, context routing, agent navigation, and advisory locking. All features work
    at demo scale.

    A combined Claude + Gemini review (research/003-consolidated-review.md) found:
    - 1 CRITICAL bug (advisory locks don't protect mutations — session mismatch)
    - 2 HIGH bugs (lock ordering, Cypher injection)
    - 1 HIGH security issue (hardcoded credentials)
    - 2 MEDIUM design issues (no transactions, sequential queries)

    Current test baseline: no automated tests (demo-only verification via `dotnet run`).
    All 13 demo sections pass. Build succeeds with 0 warnings.

    MEMORY.md gotchas relevant to this plan:
    - AGE requires LOAD 'age' + SET search_path per connection
    - Npgsql reader scope: use block form to close before running second command
    - ContextPayload must be record not class

    Files to modify:
    - AgentCoordination/SubtreeLock.cs (lock session + ordering)
    - DbLayer/NodeRepository.cs (accept connection/transaction, invariant checks)
    - GraphLayer/AgeLayer.cs (Cypher injection, edge type whitelist)
    - ContextRouter/ContextAssembler.cs (parallel queries)
    - Program.cs (env var credentials, lock ordering fix, transaction usage)
  </context>

  <thinking>
    The advisory lock session mismatch is the most architecturally significant fix. Two approaches:

    **Option A: SubtreeLock exposes its connection for repository use.**
    SubtreeLock already holds an open NpgsqlConnection for the lock. Repository methods accept an
    optional connection parameter — when provided, they use it instead of opening a new one. This
    keeps the lock and mutations on the same session.
    Pro: Minimal API change, backwards-compatible (connection param is optional).
    Con: Callers must remember to pass the connection.

    **Option B: SubtreeLock wraps mutations internally.**
    SubtreeLock gets a `MutateUnderLock(Func<NpgsqlConnection, Task>)` method that passes its
    connection to the caller's lambda.
    Pro: Impossible to forget — the API enforces the pattern.
    Con: More invasive API change, harder to compose with existing repository methods.

    Choosing **Option A** because: it's a POC, the pattern is clear, and making repository methods
    accept an optional connection also enables transaction support (Option A solves two problems).

    For Cypher injection: AGE doesn't support parameterized Cypher through Npgsql cleanly. The
    pragmatic fix is edge type whitelisting + proper string escaping for values. This blocks
    injection without requiring an AGE driver change.

    For credentials: environment variable with POC fallback. Not a config file — this is a single-
    binary console app, not a web service.
  </thinking>

  <approach>
    <step n="1">Fix advisory lock session mismatch — add optional NpgsqlConnection parameter to
    NodeRepository mutation methods (InsertNode, UpdateNode, SetAttribute). SubtreeLock exposes its
    connection. Program.cs passes lock connection to mutations.</step>

    <step n="2">Fix lock release ordering — move ReleaseSubtree call in Program.cs to after code
    regeneration and validation.</step>

    <step n="3">Add transaction support — NodeRepository.InsertNode wraps node + attribute inserts
    in a transaction when no external connection is provided. Add BeginTransaction helper.</step>

    <step n="4">Fix Cypher injection — add EdgeType whitelist constant to AgeLayer. Validate edge
    types before use. Improve string escaping for node names (escape backslash + single quote).</step>

    <step n="5">Move credentials to environment variable — Program.cs reads CODESTORAGE_CONNSTR
    env var with POC fallback and warning.</step>

    <step n="6">Parallelize context assembly — ContextAssembler.Assemble() uses Task.WhenAll for
    independent queries within each intent case.</step>

    <step n="7">Add node type constants — create Types/NodeTypes.cs with string constants for all
    known node types. Use throughout ContextAssembler and seeders.</step>

    <step n="8">Build, run full demo, verify all 13 sections pass.</step>
  </approach>

  <outputs>
    <file path="AgentCoordination/SubtreeLock.cs" action="modify">Expose lock connection, add MutateUnderLock convenience</file>
    <file path="DbLayer/NodeRepository.cs" action="modify">Optional NpgsqlConnection param on mutation methods, transaction in InsertNode</file>
    <file path="GraphLayer/AgeLayer.cs" action="modify">Edge type whitelist, improved escaping</file>
    <file path="ContextRouter/ContextAssembler.cs" action="modify">Task.WhenAll for parallel queries</file>
    <file path="ContextRouter/NodeTypes.cs" action="create">String constants for node types</file>
    <file path="Program.cs" action="modify">Env var credentials, lock ordering fix, pass lock connection to mutations</file>
  </outputs>

  <testing>
    <verify>dotnet build — 0 errors, 0 warnings</verify>
    <verify>dotnet run — all 13 demo sections pass including agent navigation and mutation</verify>
    <verify>Lock session: mutation demo uses lock connection (verify via console output)</verify>
    <verify>Lock ordering: ReleaseSubtree prints after validation success</verify>
    <verify>Cypher injection: invalid edge type throws ArgumentException</verify>
    <verify>Credentials: set CODESTORAGE_CONNSTR env var, verify it's used</verify>
    <manual>Remove env var, verify POC fallback works with warning</manual>
  </testing>

  <questions>
    <question n="1">
      <ask>Should we also add NpgsqlDataSource (Npgsql 8+ connection pooling) in this pass, or defer?</ask>
      <proposed>Defer — it's a performance improvement, not a correctness fix. Keep this plan focused on bugs and security.</proposed>
    </question>
    <question n="2">
      <ask>Should NodeTypes constants be an enum or static string class?</ask>
      <proposed>Static string class — matches the DB schema (TEXT column) and avoids enum-to-string conversion overhead. Constants can be used directly in SQL parameters.</proposed>
    </question>
  </questions>

  <risks>
    <assumption>AGE Cypher through Npgsql doesn't support true parameterized queries — whitelisting is the practical fix</assumption>
    <assumption>Optional connection parameter pattern is clear enough that callers won't accidentally skip it</assumption>
    <blast_radius>NodeRepository API changes affect all callers (seeders, Program.cs, ContextAssembler). All must compile after change.</blast_radius>
    <rollback>All changes are code-only, no schema migration. Git revert is clean rollback.</rollback>
  </risks>

  <security>
    <threat_model>
      Cypher injection: attacker-controlled edge type or node name could execute arbitrary Cypher.
      Currently only reachable via internal callers, but defense-in-depth requires sanitization.
      Credential exposure: connection string in source code is visible in git history even after fix.
    </threat_model>
    <validation>
      Edge types validated against whitelist before Cypher query construction.
      Node names escaped: single quotes and backslashes.
      Connection string from environment variable, not source code.
    </validation>
  </security>

</plan>
]]>