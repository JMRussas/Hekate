# Plan 005: Add Basic Test Suite

Add xUnit test project with unit tests for the pure-logic components. No DB required for the initial suite -- focus on components that can be tested with in-memory TreeNode trees.

```xml
<plan level="L2">

<context>
  Current state: Zero tests. The only validation is the demo loop in Program.cs that
  requires a running Postgres+AGE+pgvector Docker container.

  Key files:
  - CodeStoragePoc.csproj: .NET 8 console app, Npgsql + Roslyn packages
  - CodeStoragePoc.sln: Solution with main project + output/GeneratedCode project
  - ContextRouter/IntentClassifier.cs: Pure pattern matching, no dependencies
  - ContextRouter/PromptBuilder.cs: Builds XML prompts from ContextPayload/AgentContext records
  - ContextRouter/Types.cs: Intent enum, ContextPayload, AgentContext, AssembledPrompt records
  - Generator/CSharpGenerator.cs: Walks TreeNode tree, emits C# source (depends on NodeRepository for GetSubtree)
  - Generator/RoslynValidator.cs: Parses C# with Roslyn, returns diagnostics (pure, no DB)
  - Renderer/PlanRenderer.cs: Walks TreeNode tree, emits plan text (depends on NodeRepository for GetSubtree)
  - Renderer/ConversationRenderer.cs: Walks TreeNode tree, emits transcript (depends on NodeRepository for GetSubtree)
  - DbLayer/NodeRepository.cs: All DB access -- CRUD, subtree, search

  TreeNode is a simple in-memory class: NodeRecord + Dictionary attributes + List children.
  It can be constructed without any DB. The generators/renderers call repo.GetSubtree() to get
  a TreeNode, then walk it purely in-memory. This means we can test the tree-walking logic by
  constructing TreeNodes directly, but we need to refactor Generate/Render to accept a TreeNode
  parameter (or extract the walking logic).

  Baseline test count: 0
</context>

<thinking>
  Framework choice: xUnit is the .NET 8 standard. No reason to deviate.

  What is testable without a DB:
  1. IntentClassifier -- completely pure. Takes string, returns IntentResult. Many test cases.
  2. RoslynValidator -- completely pure. Takes C# string, returns diagnostics.
  3. PromptBuilder -- takes ContextPayload/AgentContext records (constructable in-memory), returns AssembledPrompt.
  4. CSharpGenerator.EmitNode -- currently private, called by Generate() which calls repo.GetSubtree().
     Two options: (a) make EmitNode internal+InternalsVisibleTo, or (b) add a Generate(TreeNode) overload.
     Option (b) is better -- it is a clean public API that also benefits production callers who already
     have a tree (e.g., after a lock-and-fetch). Same applies to PlanRenderer and ConversationRenderer.
  5. PlanRenderer/ConversationRenderer -- same pattern as CSharpGenerator.
  6. TreeNode.Attr() -- trivial but worth a smoke test.
  7. NodeTypes constants -- just verify they exist (catch accidental deletions).

  What needs a DB (deferred to a future integration test plan):
  - NodeRepository CRUD, subtree fetch, semantic search
  - AgeLayer Cypher queries
  - SubtreeLock advisory locking
  - Schema initialization
  - Full end-to-end: seed then generate then validate then build

  Refactoring needed: CSharpGenerator, PlanRenderer, and ConversationRenderer currently only
  expose async methods that call GetSubtree internally. To unit test the rendering logic,
  add a synchronous Render/Generate overload that accepts a TreeNode directly. This is a
  minimal, non-breaking change -- the existing async methods just call the new overload.

  Test project structure: Single xUnit project CodeStoragePoc.Tests in a tests/ folder,
  added to the solution. Test classes mirror the source structure.

  Helper: A small TreeNodeBuilder utility to construct test trees fluently, avoiding boilerplate
  in every test method.
</thinking>

<approach>
  1. Create xUnit test project tests/CodeStoragePoc.Tests/
     - Target net8.0, reference CodeStoragePoc project
     - Add xUnit, xUnit.runner, FluentAssertions packages

  2. Refactor CSharpGenerator to expose Generate(TreeNode root) overload
     - Extract the tree-walking from the async Generate(Guid) method
     - Async method becomes: fetch tree, call Generate(tree)
     - Non-breaking: existing callers unchanged

  3. Refactor PlanRenderer to expose Render(TreeNode root) overload
     - Same pattern as step 2

  4. Refactor ConversationRenderer to expose Render(TreeNode root) overload
     - Same pattern as step 2

  5. Create TreeNodeBuilder test helper
     - Fluent builder for constructing TreeNode trees in tests
     - Handles NodeRecord creation with sensible defaults
     - Methods: WithChild(), WithAttr(), Build()

  6. Write IntentClassifier tests (15-20 test cases)
     - Each intent gets at least 2 pattern hits
     - Case insensitivity
     - Default to Ideation for unknown input
     - Confidence levels
     - Edge cases: empty string, very long input, partial pattern matches

  7. Write RoslynValidator tests (5 test cases)
     - Valid C# produces no errors
     - Invalid C# produces error diagnostics
     - ValidateOrThrow throws on errors, succeeds on valid code
     - Empty string handling

  8. Write PromptBuilder tests (8 test cases)
     - Model selection per intent (voice vs text for ideation)
     - System prompt contains XML tags
     - Context items appear in output
     - Agent context builds correct XML structure
     - Role descriptions present for each intent

  9. Write CSharpGenerator tests (6 test cases)
     - Minimal struct (namespace + struct + field) produces valid C#
     - Using directives emit correctly
     - Constructor with parameters
     - Method with modifier
     - Unknown node type emits comment
     - Generated code passes RoslynValidator

  10. Write PlanRenderer tests (4 test cases)
      - Plan with phases/steps renders with status icons
      - Risks section renders severity + mitigation
      - Questions render with proposed answers
      - Empty plan renders title only

  11. Write ConversationRenderer tests (4 test cases)
      - Conversation with turns renders timeline
      - Topics with ideas render under EXTRACTED KNOWLEDGE
      - Action items render with status
      - Word wrapping works for long turns

  12. Add solution reference and verify dotnet test runs
      - Add test project to CodeStoragePoc.sln
      - Run dotnet test and confirm all pass

  13. Update CLAUDE.md with test commands and structure
</approach>

<outputs>
  | Path | Action | What Changes |
  |------|--------|-------------|
  | tests/CodeStoragePoc.Tests/CodeStoragePoc.Tests.csproj | create | xUnit test project targeting net8.0 |
  | tests/CodeStoragePoc.Tests/Helpers/TreeNodeBuilder.cs | create | Fluent builder for test TreeNode trees |
  | tests/CodeStoragePoc.Tests/ContextRouter/IntentClassifierTests.cs | create | 15-20 test cases for intent classification |
  | tests/CodeStoragePoc.Tests/ContextRouter/PromptBuilderTests.cs | create | 8 test cases for prompt building |
  | tests/CodeStoragePoc.Tests/Generator/RoslynValidatorTests.cs | create | 5 test cases for Roslyn validation |
  | tests/CodeStoragePoc.Tests/Generator/CSharpGeneratorTests.cs | create | 6 test cases for code generation |
  | tests/CodeStoragePoc.Tests/Renderer/PlanRendererTests.cs | create | 4 test cases for plan rendering |
  | tests/CodeStoragePoc.Tests/Renderer/ConversationRendererTests.cs | create | 4 test cases for conversation rendering |
  | Generator/CSharpGenerator.cs | modify | Add Generate(TreeNode) overload, extract tree-walk |
  | Renderer/PlanRenderer.cs | modify | Add Render(TreeNode) overload |
  | Renderer/ConversationRenderer.cs | modify | Add Render(TreeNode) overload |
  | CodeStoragePoc.sln | modify | Add test project reference |
  | CLAUDE.md | modify | Add test commands section |
</outputs>

<testing>
  | Task | Verification | Type |
  |------|-------------|------|
  | Test project builds | dotnet build tests/CodeStoragePoc.Tests/ | build |
  | All tests pass | dotnet test tests/CodeStoragePoc.Tests/ | unit |
  | Main project still builds | dotnet build CodeStoragePoc.csproj | build |
  | Generated code from tests passes Roslyn | CSharpGeneratorTests assert Validate() returns 0 errors | unit |
  | Refactored renderers still work | Existing Generate(Guid)/Render(Guid) methods unchanged in signature | build |
</testing>

<questions>
  1. Should we use FluentAssertions or stick with xUnit built-in Assert?
     Proposed: Use FluentAssertions -- more readable assertions for string-heavy tests
     (checking generated code contains expected fragments).

  2. Should the test project live in tests/ subfolder or at the repo root?
     Proposed: tests/CodeStoragePoc.Tests/ -- keeps the root clean, standard .NET layout.

  3. Should we add a GitHub Actions CI workflow for running tests?
     Proposed: No -- defer to a separate task. Tests run locally via dotnet test for now.
     The project has no CI gate configured.
</questions>

</plan>
```
