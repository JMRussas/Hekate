# Hekate Code Analysis MCP

Roslyn-powered code analysis server with multi-language support. Provides structural analysis, pattern detection, contract verification, allocation checking, and AI-assisted planning for .NET, Python, TypeScript, and C++ projects.

**Port:** 5110
**Transport:** HTTP JSON-RPC (MCP over HTTP)
**Service name:** `HekateServer`
**Workers:** HekatePythonWorker (9200), HekateTypeScriptWorker (9202), HekateCppWorker (9201)

---

## Connection

### Claude Code Configuration

```json
{
  "mcpServers": {
    "hekate": {
      "url": "http://localhost:5110/mcp"
    }
  }
}
```

---

## Tools

### Navigation and Discovery

#### where

Find the best starting point for a code change. Ranks candidates by ownership: types that define a concept (fields, properties) rank higher than types that consume it. Searches the entire project -- no file path needed.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `intent` | string | Yes | What you are trying to do (e.g., "add gradient support to UI fills") |
| `project` | string | Yes | Path to project file or directory |

---

#### find_implementations

Find all types that implement an interface or extend a base class. Returns type name, file path, line number, and relationship type.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `type_name` | string | Yes | Exact interface or base class name (e.g., `IRenderer`) |

---

#### find_usages

Find all references to a symbol across the project. Returns locations grouped by file with containing type/member and code snippets. Useful for change impact analysis.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `symbol_name` | string | Yes | Symbol name (e.g., `FillElement`, `Render`) |
| `file_path` | string | No | File where the symbol is defined (speeds up search) |
| `max_results` | int | No | Max results (default: 50) |

---

#### find_patterns

Find files with similar patterns to a target file. Useful for discovering conventions and related implementations.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `file_path` | string | Yes | Path to the file to compare against |

---

### Analysis

#### decide

Analyze a codebase and return a verdict: patterns to follow, constraints that apply, things to avoid, and existing files to match. Scoped by role (runtime, editor, platform, general).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `intent` | string | Yes | What you are trying to do |
| `project` | string | Yes | Path to project file or directory |
| `file_path` | string | Yes | Path to the file you are working on |
| `rigor` | string | No | Analysis depth: `quick`, `standard`, `thorough`, `deep-review`, `consensus` |

---

#### analyze_file

Get raw analysis of a file: types, methods, detected patterns, allocation count. Hot-path detection uses role-specific entry points.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `file_path` | string | Yes | Path to the file to analyze |

---

#### project_graph

Build the project dependency graph. Returns all projects with dependencies, reverse dependencies, topological build order, and cycle detection.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |

---

### Verification and Quality

#### review

Validate files against their role's constraints and guidelines. Returns violations with severity (error for constraint violations, warning for guideline matches). Supports diff-scoped review.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `file_paths` | string | Yes | Comma-separated file paths to review |
| `diff_base` | string | No | Git ref to diff against (e.g., `HEAD~1`, `main`) -- reviews only changed lines |
| `summarize` | bool | No | Summarize results via LLM (default: false) |

---

#### verify

Compile-check a file without running `dotnet build`. Uses Roslyn's in-memory compilation for sub-second verification. Optionally verify modified text before writing to disk.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `file_path` | string | Yes | Path to the file to verify |
| `modified_text` | string | No | Modified content to verify in-memory (checks file as-is if omitted) |

---

#### check_contracts

Verify a type satisfies its contracts: all interface members implemented and constructor parameters available in DI. Returns missing members with signatures and DI registration warnings.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `type_name` | string | Yes | Name of the type to check (e.g., `MyService`) |
| `file_path` | string | No | File where the type is defined |

---

#### check_allocations

Detect heap allocations in a file or specific method. Useful for hot-path optimization.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `file_path` | string | Yes | Path to the file to check |
| `method_name` | string | No | Specific method to check (entire file if omitted) |

---

#### test_impact

Find test methods affected by changes to a symbol. Walks the call graph into test projects and finds attributed methods (`[Fact]`, `[Test]`, `[TestMethod]`). Returns a `dotnet test --filter` command.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `symbol_name` | string | Yes | Symbol that changed |
| `file_path` | string | No | File where the symbol is defined |

---

### Indexing

#### build_index

Build a local index for a project. Extracts types, methods, patterns, allocations, call graph edges, and dependencies into `.hekate/index/`. Atomic writes via temp directory swap.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file |
| `incremental` | bool | No | Only re-analyze changed files (default: false) |

---

#### index_status

Check the status of a project's local index. Reports existence, build time, schema version, and stale file count.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file |

---

### Planning

#### plan

Orchestrate a full change plan in a single call. Combines `where` + `decide` + `find_usages` + `project_graph` into one cross-referenced result. Saves 4+ round trips.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `intent` | string | Yes | What you are trying to do |
| `project` | string | Yes | Path to project file or directory |
| `depth` | string | No | `quick` (where+decide) or `full` (all sub-tools, default) |
| `summarize` | bool | No | Summarize via LLM (default: false) |

---

#### list_plans

List all saved plans for a project. Returns summaries with plan ID, intent, status, revision, step count, and gap count. Plans stored in `.hekate/plans/`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |

---

#### get_plan

Retrieve a saved plan by ID with all steps, gaps, reuse directives, and lifecycle status.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `plan_id` | string | Yes | Plan ID to retrieve |

---

#### review_plan

Critique a saved plan by running gap detection against the project index. Detects missing symbol coverage, cross-role impacts, reuse conflicts, and missing test coverage. Requires a built index.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `plan_id` | string | Yes | Plan ID to review |

---

#### deepen_plan

Re-analyze focused areas of a saved plan at higher depth. Finds semantic overlap with existing code to attach reuse directives.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `plan_id` | string | Yes | Plan ID to deepen |
| `focus` | string | No | Step index or symbol name to focus on (omit to deepen all) |

---

#### finalize_plan

Validate a plan is ready for execution and lock it. Checks for unresolved error-severity gaps, adds missing gates, and sets status to `finalized`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `plan_id` | string | Yes | Plan ID to finalize |

---

### Code Editing

#### iterate

Review files and suggest concrete fixes for each violation. Combines `review` with targeted fix suggestions as executable edit operations.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `file_paths` | string | Yes | Comma-separated file paths |

---

#### execute

Execute a surgical code edit using Roslyn. Generates syntactically correct C# and returns a diff. Never writes to disk.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `project` | string | Yes | Path to project file or directory |
| `operation` | string | Yes | `AddMember`, `ImplementInterface`, `AddConstructorParam`, `AddUsing`, `RenameSymbol`, `ExtractInterface`, `CreateFile` |
| `file_path` | string | Yes | Target file path |
| `target_type` | string | No | Class/struct to modify |
| `member_kind` | string | No | `method`, `field`, `property` |
| `member_name` | string | No | Member name |
| `return_type` | string | No | Return/field type |
| `parameters` | string | No | Parameter list as JSON array |
| `body` | string | No | Method body |
| `access_modifier` | string | No | `public`, `private`, `protected`, `internal` |
| `interface_name` | string | No | For `ImplementInterface` |
| `param_name` | string | No | For `AddConstructorParam` |
| `param_type` | string | No | For `AddConstructorParam` |
| `namespace` | string | No | For `AddUsing` |
| `old_name` | string | No | For `RenameSymbol` |
| `new_name` | string | No | For `RenameSymbol` |
| `source_type` | string | No | For `ExtractInterface` |
| `type_name` | string | No | For `CreateFile` |
| `type_kind` | string | No | For `CreateFile`: `class`, `struct`, `record`, `interface` |
| `base_types` | string | No | For `CreateFile`: JSON array of base types |
| `usings` | string | No | For `CreateFile`: JSON array of using directives |

---

### Utility

#### get_log

Retrieve raw results from a previous tool call by its call ID. Use when a summarized result needs deeper inspection.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `call_id` | string | Yes | Call ID from a previous result's `callId` field |

---

#### self_update

Build, publish, and restart the Hekate MCP server with latest code changes. Only available in HTTP mode. The MCP connection drops briefly during restart.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `restart` | bool | No | Restart after publishing (default: true) |
