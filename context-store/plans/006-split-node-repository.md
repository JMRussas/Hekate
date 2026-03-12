# Plan 006: Split NodeRepository into Focused Classes

NodeRepository.cs (606 lines) mixes CRUD, tree navigation, semantic search, and bulk queries in a single class. This plan splits it into focused classes by concern while preserving the connection injection pattern and avoiding breaking changes.

```xml
<plan level="L2" number="006" status="plan" created="2026-03-08">

<context>
  NodeRepository.cs is 606 lines with 14 public methods spanning 4 distinct concerns:

  CRUD (mutating):
    - InsertNode (with transactional attribute insertion)
    - SetAttribute
    - UpdateNode
    - DeleteSubtree
    - SetEmbedding
    - NextSiblingOrder

  Navigation (read, tree-walking):
    - GetNode
    - GetSubtree (recursive CTE, builds TreeNode tree with attributes)
    - GetParent
    - GetChildren (with optional type filter, batch attribute loading)
    - GetSiblings (batch attribute loading)
    - GetNodeWithAttributes

  Search (pgvector):
    - SemanticSearch

  Bulk/project-level:
    - GetAllNodes (used by AgeLayer.SyncAllVertices)

  Shared infrastructure:
    - GetConnection() — dual-mode connection factory (external or new-per-call)
    - ReadNode() — NpgsqlDataReader to NodeRecord mapper
    - LoadAttributes() — batch attribute loader for node lists
    - InsertAttribute() — upsert helper

  Record types co-located in the file:
    - NodeRecord (record)
    - TreeNode (class with Attributes dict and Children list)

  CONSUMERS (8 distinct callers):
    - Program.cs: new NodeRepository(connStr) for reads, new NodeRepository(lockConn) for mutations
    - Vector2Seeder: InsertNode, SetEmbedding (via _repo field)
    - PlanSeeder, Plan002Seeder, Plan003Seeder: InsertNode only
    - ConversationSeeder: InsertNode only
    - CSharpGenerator: GetSubtree only
    - PlanRenderer: GetSubtree only
    - ConversationRenderer: GetSubtree only
    - ContextAssembler: GetNodeWithAttributes, GetParent, GetSiblings, GetChildren
      (via agent demo in Program.cs), does NOT use repo for its own queries (opens raw connections)
    - AgeLayer.SyncAllVertices: GetAllNodes

  CONNECTION INJECTION PATTERN:
    Constructor overloads: (string connStr) for read-only, (NpgsqlConnection, NpgsqlTransaction?)
    for lock-safe mutations. SubtreeLock.LockConnection flows through to NodeRepository for
    mutations on the same advisory-lock session.

  NO EXISTING TESTS — POC project, verification is the dotnet run demo loop.
</context>

<thinking>
  GROUPING ANALYSIS:

  The natural split is by concern:
  1. NodeCrud — mutations: InsertNode, SetAttribute, UpdateNode, DeleteSubtree, NextSiblingOrder
  2. NodeNavigation — tree reads: GetNode, GetSubtree, GetParent, GetChildren, GetSiblings,
     GetNodeWithAttributes, GetAllNodes
  3. NodeSearch — vector operations: SetEmbedding, SemanticSearch

  GetAllNodes is only used by AgeLayer.SyncAllVertices. It could go in NodeNavigation or stay
  standalone. Putting it in NodeNavigation is simpler — it is a read query, just not tree-shaped.

  SetEmbedding is a mutation but it is pgvector-specific. Grouping it with SemanticSearch keeps
  all pgvector concerns together. The alternative (putting it in NodeCrud) splits the vector
  concern across files. Keeping it in NodeSearch is better for cohesion.

  SHARED INFRASTRUCTURE:

  GetConnection() is needed by all classes. Options:
    a) Base class with protected GetConnection() — simple, direct
    b) Shared static helper — awkward, must pass connStr/externalConn
    c) Duplicate in each class — violates DRY
    d) Extract a NodeConnectionProvider that all classes compose

  Option (a) is simplest for a POC. A base class NodeRepositoryBase holds:
    - _connStr, _externalConn, _externalTx
    - Both constructors
    - GetConnection()

  ReadNode() analysis:
    Used by: GetNode, GetSubtree, GetChildren, GetSiblings, GetNodeWithAttributes — all navigation.
    InsertNode does not read nodes. DeleteSubtree does not use ReadNode.
    Since it could be needed by future classes, put it protected static on the base class.

  LoadAttributes() — used by GetChildren and GetSiblings — both navigation. Stays in NodeNavigation.

  InsertAttribute() — used by InsertNode and SetAttribute — both CRUD. Goes in NodeCrud.

  RECORD TYPES (NodeRecord, TreeNode):
  Consumed everywhere. Should go in their own file: NodeModels.cs.
  Note: ContextRouter/NodeTypes.cs already exists with string constants — use NodeModels.cs to
  avoid name collision.

  INTERFACE EXTRACTION:
  Convention: "extract interfaces for 3+ consumers."
  - Navigation methods consumed by: CSharpGenerator, PlanRenderer, ConversationRenderer,
    Program.cs, ContextAssembler (via Program.cs agent demo) = 5 consumers. Extract INodeNavigation.
  - CRUD methods consumed by: Vector2Seeder, PlanSeeder, Plan002Seeder, Plan003Seeder,
    ConversationSeeder, Program.cs = 6 consumers. Extract INodeCrud.
  - Search methods: Vector2Seeder (SetEmbedding), Program.cs (SemanticSearch) = 2 consumers.
    Skip INodeSearch.

  BREAKING CHANGES:
  Current code does `new NodeRepository(connStr)` then calls any method. After the split, callers
  need to know which class to instantiate. Options:
    a) Keep a facade NodeRepository that delegates to sub-classes — zero breaking changes
    b) Update all call sites — POC with 8 callers, manageable
    c) Keep NodeRepository as a composed class that exposes all sub-objects

  Option (b) is cleanest. The POC has few callers and this is a refactor plan. Each caller only
  uses 1-2 concerns, so they can take the specific class. The facade just hides what was wrong
  in the first place. And the consumer list is small.

  DECISION: Split into focused classes, update call sites, extract INodeCrud and INodeNavigation.
</thinking>

<approach>
  1. Create DbLayer/NodeModels.cs — move NodeRecord and TreeNode out of NodeRepository.cs.

  2. Create DbLayer/NodeRepositoryBase.cs — extract:
     - Fields: _connStr, _externalConn, _externalTx
     - Both constructors (string connStr) and (NpgsqlConnection, NpgsqlTransaction?)
     - GetConnection() method (protected)
     - ReadNode() as protected static

  3. Create DbLayer/INodeCrud.cs — interface with:
     - InsertNode, SetAttribute, UpdateNode, DeleteSubtree, NextSiblingOrder

  4. Create DbLayer/INodeNavigation.cs — interface with:
     - GetNode, GetSubtree, GetParent, GetChildren, GetSiblings, GetNodeWithAttributes, GetAllNodes

  5. Create DbLayer/NodeCrud.cs : NodeRepositoryBase, INodeCrud
     - Move InsertNode, SetAttribute, UpdateNode, DeleteSubtree, NextSiblingOrder
     - Move InsertAttribute (private helper)

  6. Create DbLayer/NodeNavigation.cs : NodeRepositoryBase, INodeNavigation
     - Move GetNode, GetSubtree, GetParent, GetChildren, GetSiblings, GetNodeWithAttributes, GetAllNodes
     - Move LoadAttributes (private helper)

  7. Create DbLayer/NodeSearch.cs : NodeRepositoryBase
     - Move SetEmbedding, SemanticSearch

  8. Delete DbLayer/NodeRepository.cs (all content has been moved).
     Run `dotnet build` — expect errors from consumers (confirms old class removed).

  9. Update consumers — change type references:
     a. Program.cs: NodeRepository -> NodeCrud + NodeNavigation + NodeSearch as needed
     b. Vector2Seeder: NodeRepository -> NodeCrud (InsertNode) + NodeSearch (SetEmbedding)
     c. PlanSeeder, Plan002Seeder, Plan003Seeder, ConversationSeeder: NodeRepository -> INodeCrud
     d. CSharpGenerator: NodeRepository -> INodeNavigation
     e. PlanRenderer: NodeRepository -> INodeNavigation
     f. ConversationRenderer: NodeRepository -> INodeNavigation
     g. AgeLayer.SyncAllVertices: NodeRepository -> INodeNavigation (GetAllNodes)
     h. ContextAssembler: NodeRepository -> INodeNavigation (for repo field used in agent navigation)

  10. Update file dependency headers on all modified files.

  11. Update CLAUDE.md project structure section to reflect new DbLayer files.

  12. Final verification: `dotnet build` compiles cleanly, `dotnet run` with Docker produces
      same demo output as before.
</approach>

<outputs>
  | File | Action | What Changes |
  |------|--------|-------------|
  | DbLayer/NodeModels.cs | CREATE | NodeRecord record + TreeNode class (moved from NodeRepository.cs) |
  | DbLayer/NodeRepositoryBase.cs | CREATE | Shared base: constructors, GetConnection(), ReadNode() |
  | DbLayer/INodeCrud.cs | CREATE | Interface: InsertNode, SetAttribute, UpdateNode, DeleteSubtree, NextSiblingOrder |
  | DbLayer/INodeNavigation.cs | CREATE | Interface: GetNode, GetSubtree, GetParent, GetChildren, GetSiblings, GetNodeWithAttributes, GetAllNodes |
  | DbLayer/NodeCrud.cs | CREATE | CRUD class extending NodeRepositoryBase, implementing INodeCrud |
  | DbLayer/NodeNavigation.cs | CREATE | Navigation class extending NodeRepositoryBase, implementing INodeNavigation |
  | DbLayer/NodeSearch.cs | CREATE | Search class extending NodeRepositoryBase (SetEmbedding, SemanticSearch) |
  | DbLayer/NodeRepository.cs | DELETE | All content moved to new files |
  | Program.cs | MODIFY | Replace NodeRepository with NodeCrud/NodeNavigation/NodeSearch |
  | SeedData/Vector2Seeder.cs | MODIFY | Accept NodeCrud + NodeSearch (two constructor params) |
  | SeedData/PlanSeeder.cs | MODIFY | Accept INodeCrud |
  | SeedData/Plan002Seeder.cs | MODIFY | Accept INodeCrud |
  | SeedData/Plan003Seeder.cs | MODIFY | Accept INodeCrud |
  | SeedData/ConversationSeeder.cs | MODIFY | Accept INodeCrud |
  | Generator/CSharpGenerator.cs | MODIFY | Accept INodeNavigation |
  | Renderer/PlanRenderer.cs | MODIFY | Accept INodeNavigation |
  | Renderer/ConversationRenderer.cs | MODIFY | Accept INodeNavigation |
  | GraphLayer/AgeLayer.cs | MODIFY | SyncAllVertices accepts INodeNavigation |
  | ContextRouter/ContextAssembler.cs | MODIFY | Accept INodeNavigation (for repo field) |
  | CLAUDE.md | MODIFY | Update project structure, file list, dependency info |
</outputs>

<testing>
  | Step | Verification | Type |
  |------|-------------|------|
  | After step 8 | `dotnet build` — expect compile errors from consumers (confirms old class removed) | build |
  | After step 9 | `dotnet build` compiles with zero errors (all consumers updated) | build |
  | After step 12 | `dotnet run` with Docker up — full demo loop passes (same output as before) | integration |
  | Manual | Grep for "class NodeRepository" — should appear nowhere in source | manual |
  | Manual | Each new file has <= 1 public type and filename matches type name | convention |
  | Manual | No method exceeds 50 logic lines (InsertNode is ~35, GetSubtree is ~45 — both ok) | convention |
</testing>

<questions>
  <question id="1">
    Should Vector2Seeder take two constructor parameters (NodeCrud + NodeSearch) or a single
    composed object? Two params is honest about its dependencies. A single composed "seeder
    support" object adds abstraction for one consumer.
    <proposed>Two constructor parameters: NodeCrud for InsertNode, NodeSearch for SetEmbedding.
    Seeder methods already separate seeding (InsertNode) from embedding (SeedEmbeddings), so
    the split maps cleanly.</proposed>
  </question>

  <question id="2">
    Should NodeSearch get an interface (INodeSearch)? Only 2 consumers: Vector2Seeder and
    Program.cs. Convention says 3+ consumers for interface extraction.
    <proposed>Skip INodeSearch for now. Add it when a third consumer appears.</proposed>
  </question>

  <question id="3">
    GetAllNodes is only used by AgeLayer.SyncAllVertices. Should it go in INodeNavigation
    or stay as a concrete-only method on NodeNavigation?
    <proposed>Include it in INodeNavigation. AgeLayer already takes the repo as a parameter,
    and having it on the interface lets AgeLayer depend on the abstraction. It is a read query
    and fits the navigation concern.</proposed>
  </question>
</questions>

</plan>
```
