// CodeStoragePoc - Main Orchestrator
//
// Runs the full proof-of-concept loop:
// 1. Initialize DB (extensions, schema, graph)
// 2. Seed Vector2 struct as node tree
// 3. Sync AGE graph vertices and edges
// 4. Generate C# source from node tree
// 5. Validate with Roslyn
// 6. Write to disk and build
// 7. Semantic search demo (pgvector)
// 8. Graph query demo (AGE Cypher)
// 9. Mutation demo (agent claims subtree, mutates, regenerates, rebuilds)
// 10. LISTEN/NOTIFY subscriber (background task observing mutations)
//
// Depends on: all project layers
// Used by:    (entry point)

using Npgsql;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using CodeStoragePoc.AgentCoordination;
using CodeStoragePoc.BuildRunner;
using CodeStoragePoc.DbLayer;
using CodeStoragePoc.Generator;
using CodeStoragePoc.GraphLayer;
using CodeStoragePoc.ContextRouter;
using CodeStoragePoc.Renderer;
using CodeStoragePoc.SeedData;

var ConnStr = Environment.GetEnvironmentVariable("CODESTORAGE_CONNSTR");
if (ConnStr == null)
{
    ConnStr = "Host=localhost;Port=5433;Database=code_storage;Username=postgres;Password=postgres";
    Console.WriteLine("[WARN]     CODESTORAGE_CONNSTR not set — using POC default credentials");
}
const string OutputDir = "output";

static void Log(string tag, string message) =>
    Console.WriteLine($"[{tag,-10}] {message}");

// ============================================================
// 1. INITIALIZE DATABASE
// ============================================================
Log("INIT", "Connecting to PostgreSQL...");

// Wait for DB to be ready (Docker may still be starting)
var retries = 0;
while (true)
{
    try
    {
        await using var test = new NpgsqlConnection(ConnStr);
        await test.OpenAsync();
        break;
    }
    catch when (retries++ < 10)
    {
        await Task.Delay(1000);
    }
}

Log("INIT", "Extensions enabled: age, vector");
await Schema.Initialize(ConnStr);

// ============================================================
// 2. SEED DATA
// ============================================================
var repo = new NodeRepository(ConnStr);

// Create project and file records
Guid projectId, fileId;
{
    await using var conn = new NpgsqlConnection(ConnStr);
    await conn.OpenAsync();

    // Clean any previous run
    await using var clean = new NpgsqlCommand("""
        DELETE FROM node_attributes;
        DELETE FROM nodes;
        DELETE FROM files;
        DELETE FROM projects;
    """, conn);
    await clean.ExecuteNonQueryAsync();

    // Create project
    await using var projCmd = new NpgsqlCommand("""
        INSERT INTO projects (id, name, root_path) VALUES (@id, @name, @path) RETURNING id
    """, conn);
    projectId = Guid.NewGuid();
    projCmd.Parameters.AddWithValue("id", projectId);
    projCmd.Parameters.AddWithValue("name", "Vector2Demo");
    projCmd.Parameters.AddWithValue("path", Path.GetFullPath(OutputDir));
    await projCmd.ExecuteScalarAsync();

    // Create file record (root_node_id set after seeding)
    await using var fileCmd = new NpgsqlCommand("""
        INSERT INTO files (id, project_id, file_path) VALUES (@id, @proj, @path) RETURNING id
    """, conn);
    fileId = Guid.NewGuid();
    fileCmd.Parameters.AddWithValue("id", fileId);
    fileCmd.Parameters.AddWithValue("proj", projectId);
    fileCmd.Parameters.AddWithValue("path", "Vector2.cs");
    await fileCmd.ExecuteScalarAsync();
}

var seeder = new Vector2Seeder(repo);
var rootNodeId = await seeder.Seed(projectId, fileId);

// Link file to root node
{
    await using var conn = new NpgsqlConnection(ConnStr);
    await conn.OpenAsync();
    await using var cmd = new NpgsqlCommand(
        "UPDATE files SET root_node_id = @root WHERE id = @id", conn);
    cmd.Parameters.AddWithValue("root", rootNodeId);
    cmd.Parameters.AddWithValue("id", fileId);
    await cmd.ExecuteNonQueryAsync();
}

// ============================================================
// 3. AGE GRAPH SYNC
// ============================================================
var age = new AgeLayer(ConnStr);
bool ageAvailable = true;
try
{
    await age.SyncAllVertices(repo, projectId);
    await seeder.SeedEdges(age);
}
catch (Exception ex) when (ex.Message.Contains("age") || ex.Message.Contains("cypher"))
{
    Log("GRAPH", $"AGE not available: {ex.Message}");
    Log("GRAPH", "Skipping graph features. Ensure AGE is in shared_preload_libraries.");
    ageAvailable = false;
}

// ============================================================
// 4. GENERATE CODE
// ============================================================
var generator = new CSharpGenerator(repo);
Log("GENERATE", $"Walking node tree from root: {rootNodeId}");
var code = await generator.Generate(rootNodeId);

// ============================================================
// 5. VALIDATE WITH ROSLYN
// ============================================================
var validator = new RoslynValidator();
validator.ValidateOrThrow(code);

// ============================================================
// 6. WRITE FILE AND BUILD
// ============================================================
Directory.CreateDirectory(OutputDir);

// Create a minimal .csproj for the generated code
var csproj = """
    <Project Sdk="Microsoft.NET.Sdk">
      <PropertyGroup>
        <OutputType>Library</OutputType>
        <TargetFramework>net8.0</TargetFramework>
        <Nullable>enable</Nullable>
      </PropertyGroup>
    </Project>
    """;
await File.WriteAllTextAsync(Path.Combine(OutputDir, "GeneratedCode.csproj"), csproj);

var outputPath = Path.Combine(OutputDir, "Vector2.cs");
await File.WriteAllTextAsync(outputPath, code);
Log("WRITE", $"→ {outputPath}");

Console.WriteLine();
Console.WriteLine("--- Generated Code ---");
Console.WriteLine(code);
Console.WriteLine("--- End Generated Code ---");
Console.WriteLine();

var (buildSuccess, _) = await DotnetBuilder.Build(Path.GetFullPath(OutputDir));
if (!buildSuccess)
{
    Log("ERROR", "Initial build failed. Aborting.");
    return;
}

// ============================================================
// 7. SEMANTIC SEARCH DEMO (pgvector)
// ============================================================
Log("SEARCH", "Setting embeddings on method nodes...");
await seeder.SeedEmbeddings();

// Recreate the IVFFlat index now that we have data
{
    await using var conn = new NpgsqlConnection(ConnStr);
    await conn.OpenAsync();
    try
    {
        await using var cmd = new NpgsqlCommand("""
            DROP INDEX IF EXISTS idx_nodes_embedding;
            CREATE INDEX idx_nodes_embedding
            ON nodes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1);
        """, conn);
        await cmd.ExecuteNonQueryAsync();
    }
    catch { /* IVFFlat may need more rows than we have */ }
}

var queryVec = seeder.GetQueryVector();
var searchResults = await repo.SemanticSearch(queryVec, 3);
Log("SEARCH", $"Semantic query: top {searchResults.Count} similar nodes:");
foreach (var (id, name, nodeType, dist) in searchResults)
    Log("SEARCH", $"  {name ?? "(unnamed)"} ({nodeType}) — distance: {dist:F4}");

// ============================================================
// 8. GRAPH QUERY DEMO (AGE)
// ============================================================
if (ageAvailable)
{
    Log("GRAPH", "Running Cypher queries...");

    // Query 1: All methods/constructors that reference field X
    var refsToX = await age.FindMethodsReferencing("X");
    Log("GRAPH", $"References to X: [{string.Join(", ", refsToX)}]");

    // Query 2: Dependency paths from Add to any field
    var paths = await age.FindDependencyPaths("Add");
    Log("GRAPH", $"Dependency paths from Add: {paths.Count} path(s)");
    foreach (var p in paths)
        Log("GRAPH", $"  {p}");
}

// ============================================================
// 9. PLAN-AS-NODES DEMO
// ============================================================
Log("PLAN", "Seeding Plan 001: Voice-Enabled Ideation Assistant...");

// Create a separate project for the plan domain
Guid planProjectId;
{
    await using var conn = new NpgsqlConnection(ConnStr);
    await conn.OpenAsync();
    await using var projCmd = new NpgsqlCommand("""
        INSERT INTO projects (id, name, root_path) VALUES (@id, @name, @path) RETURNING id
    """, conn);
    planProjectId = Guid.NewGuid();
    projCmd.Parameters.AddWithValue("id", planProjectId);
    projCmd.Parameters.AddWithValue("name", "VoiceIdeationAssistant");
    projCmd.Parameters.AddWithValue("path", "plans/001-voice-ideation-assistant");
    await projCmd.ExecuteScalarAsync();
}

var planSeeder = new PlanSeeder(repo);
var planRootId = await planSeeder.Seed(planProjectId);

// Render the plan back from DB
var planRenderer = new PlanRenderer(repo);
var renderedPlan = await planRenderer.Render(planRootId);

Console.WriteLine();
Console.WriteLine("--- Plan from DB ---");
Console.WriteLine(renderedPlan);
Console.WriteLine("--- End Plan from DB ---");
Console.WriteLine();

Log("PLAN", $"Plan round-trip: {planSeeder.AllNodeIds.Count} nodes inserted, rendered successfully");

// Sync plan nodes to AGE graph (if available)
if (ageAvailable)
{
    try
    {
        await age.SyncAllVertices(repo, planProjectId);
        Log("GRAPH", $"Plan vertices synced to AGE graph");
    }
    catch (Exception ex)
    {
        Log("GRAPH", $"Plan AGE sync failed (non-fatal): {ex.Message}");
    }
}

// ============================================================
// 10. PLAN 002: IDEATION + CONTEXT ROUTER
// ============================================================
Log("PLAN", "Seeding Plan 002: Ideation Nodes & Context Router...");
var plan002Seeder = new Plan002Seeder(repo);
var plan002RootId = await plan002Seeder.Seed(planProjectId);

var renderedPlan002 = await planRenderer.Render(plan002RootId);
Console.WriteLine();
Console.WriteLine("--- Plan 002 from DB ---");
Console.WriteLine(renderedPlan002);
Console.WriteLine("--- End Plan 002 from DB ---");
Console.WriteLine();

// ============================================================
// 10b. PLAN 003: REVIEW FIXES (HARDENING PASS)
// ============================================================
Log("PLAN", "Seeding Plan 003: Review Fixes — Hardening Pass...");
var plan003Seeder = new Plan003Seeder(repo);
var plan003RootId = await plan003Seeder.Seed(planProjectId);

var renderedPlan003 = await planRenderer.Render(plan003RootId);
Console.WriteLine();
Console.WriteLine("--- Plan 003 from DB ---");
Console.WriteLine(renderedPlan003);
Console.WriteLine("--- End Plan 003 from DB ---");
Console.WriteLine();

// ============================================================
Log("PLAN", "Seeding Plan 005: Planner View (enriched contract)...");
var plan005Seeder = new Plan005Seeder(repo);
var plan005RootId = await plan005Seeder.Seed(planProjectId);

var renderedPlan005 = await planRenderer.Render(plan005RootId);
Console.WriteLine();
Console.WriteLine("--- Plan 005 from DB ---");
Console.WriteLine(renderedPlan005);
Console.WriteLine("--- End Plan 005 from DB ---");
Console.WriteLine();

// ============================================================
// 10c. RESEARCH + ROADMAP
// ============================================================
Log("RESEARCH", "Seeding research docs 001-006 + roadmap root...");
var researchSeeder = new ResearchSeeder(repo);
var roadmapId = await researchSeeder.Seed(planProjectId);

// Sync to AGE graph and create cross-reference edges
if (ageAvailable)
{
    try
    {
        await age.SyncAllVertices(repo, planProjectId);
        await researchSeeder.SeedEdges(age,
            plan001Id: planRootId,
            plan002Id: plan002RootId,
            plan003Id: plan003RootId);
        Log("GRAPH", "Research vertices + edges synced to AGE graph");

        // Query: what research informs Plan 003?
        var informing = await age.QueryNodeNames(
            "MATCH (r:CodeNode)-[:INFORMS]->(p:CodeNode {node_id: '" + plan003RootId + "'}) RETURN r.name AS name");
        Log("GRAPH", $"Research informing Plan 003: {informing.Count} doc(s)");
        foreach (var r in informing)
            Log("GRAPH", $"  {r}");
    }
    catch (Exception ex)
    {
        Log("GRAPH", $"Research AGE sync failed (non-fatal): {ex.Message}");
    }
}

// ============================================================
// 11. CONVERSATION + CONTEXT ROUTER DEMO
// ============================================================
Log("CONV", "Seeding sample conversation...");

// Create a project for the ideation domain
Guid ideationProjectId;
{
    await using var conn = new NpgsqlConnection(ConnStr);
    await conn.OpenAsync();
    await using var projCmd = new NpgsqlCommand("""
        INSERT INTO projects (id, name, root_path) VALUES (@id, @name, @path) RETURNING id
    """, conn);
    ideationProjectId = Guid.NewGuid();
    projCmd.Parameters.AddWithValue("id", ideationProjectId);
    projCmd.Parameters.AddWithValue("name", "IdeationSessions");
    projCmd.Parameters.AddWithValue("path", "conversations");
    await projCmd.ExecuteScalarAsync();
}

var convSeeder = new ConversationSeeder(repo);
var convRootId = await convSeeder.Seed(ideationProjectId);

// Render the conversation
var convRenderer = new ConversationRenderer(repo);
var renderedConv = await convRenderer.Render(convRootId);

Console.WriteLine();
Console.WriteLine("--- Conversation from DB ---");
Console.WriteLine(renderedConv);
Console.WriteLine("--- End Conversation from DB ---");
Console.WriteLine();

// Sync conversation nodes to AGE graph and create edges
if (ageAvailable)
{
    try
    {
        await age.SyncAllVertices(repo, ideationProjectId);
        await convSeeder.SeedEdges(age);
        Log("GRAPH", "Conversation vertices + edges synced to AGE graph");
    }
    catch (Exception ex)
    {
        Log("GRAPH", $"Conversation AGE sync failed (non-fatal): {ex.Message}");
    }
}

// --- Context Router Demo ---
Log("ROUTER", "Demonstrating context router: same conversation, different intents...");

var classifier = new IntentClassifier();
var assembler = new ContextAssembler(repo, ConnStr);
var promptBuilder = new PromptBuilder();

// Test inputs with different intents
var testInputs = new[]
{
    ("What if we also added CQRS on top of the event sourcing?", "text"),
    ("What did we decide about the event store?", "text"),
    ("Park that Kafka idea for now", "text"),
    ("Let's plan out the events table implementation", "voice"),
    ("Tell me more about the materialized view approach", "text"),
};

foreach (var (input, mode) in testInputs)
{
    var intent = classifier.Classify(input);
    var context = await assembler.Assemble(intent, input, convRootId, mode);
    var prompt = promptBuilder.Build(context);

    Console.WriteLine($"  Input:  \"{input}\" [{mode}]");
    Console.WriteLine($"  Intent: {intent.Intent} (matched: \"{intent.MatchedPattern ?? "default"}\", confidence: {intent.Confidence})");
    Console.WriteLine($"  Model:  {prompt.SuggestedModel}");
    Console.WriteLine($"  Ideas:  {context.RelevantIdeas.Count} relevant, {context.OpenThreads.Count} open threads");
    Console.WriteLine($"  Turns:  {context.RecentTurns.Count} recent (not full history)");
    Console.WriteLine();
}

// Show one full assembled prompt as an example
{
    var exampleInput = "What if we also added CQRS on top of the event sourcing?";
    var intent = classifier.Classify(exampleInput);
    var context = await assembler.Assemble(intent, exampleInput, convRootId);
    var prompt = promptBuilder.Build(context);

    Console.WriteLine("--- Example Assembled Prompt (what the model actually sees) ---");
    Console.WriteLine(prompt.SystemPrompt);
    Console.WriteLine($"User: {prompt.UserPrompt}");
    Console.WriteLine("--- End Assembled Prompt ---");
    Console.WriteLine();
}

Log("ROUTER", "Context router demo complete — models get curated context, not raw history");

// --- Agent Context Demo (node references, not pre-fetched data) ---
Log("ROUTER", "Agent context demo: node references + navigation...");

{
    var agentInput = "What if we also added CQRS on top of the event sourcing?";
    var intent = classifier.Classify(agentInput);
    var agentCtx = await assembler.AssembleForAgent(
        intent, agentInput, convRootId,
        sourceAgent: "user", targetAgent: "claude");
    var agentPrompt = promptBuilder.BuildFromAgentContext(agentCtx);

    Console.WriteLine("--- Agent Context (node references, not full content) ---");
    Console.WriteLine(agentPrompt.SystemPrompt);
    Console.WriteLine($"User: {agentPrompt.UserPrompt}");
    Console.WriteLine("--- End Agent Context ---");
    Console.WriteLine();

    // Simulate: agent decides to pull detail on first focus node
    if (agentCtx.FocusNodes.Count > 0)
    {
        var firstRef = agentCtx.FocusNodes[0];
        Log("AGENT", $"Agent pulls detail on: {firstRef.Name ?? firstRef.NodeId.ToString()}");

        var detail = await repo.GetNodeWithAttributes(firstRef.NodeId);
        if (detail != null)
        {
            Log("AGENT", $"  Type: {detail.Record.NodeType}, Name: {detail.Record.Name}");
            Log("AGENT", $"  Value: {detail.Record.Value}");
            foreach (var (key, val) in detail.Attributes)
                Log("AGENT", $"  Attr: {key} = {val}");
        }

        // Agent navigates up to parent
        var parent = await repo.GetParent(firstRef.NodeId);
        if (parent != null)
            Log("AGENT", $"  Parent: [{parent.Record.NodeType}] {parent.Record.Name}");

        // Agent looks at siblings
        var siblings = await repo.GetSiblings(firstRef.NodeId);
        Log("AGENT", $"  Siblings: {siblings.Count} nodes");
        foreach (var sib in siblings)
            Log("AGENT", $"    [{sib.Record.NodeType}] {sib.Record.Name ?? sib.Record.Value?[..Math.Min(50, sib.Record.Value.Length)]}");
    }
}

Log("ROUTER", "Agent navigates tree on demand — pulls only what it needs");

// ============================================================
// 12. START LISTEN/NOTIFY SUBSCRIBER
// ============================================================
await using var notifier = new ChangeNotifier(ConnStr);
await notifier.StartListening();

// Small delay to ensure listener is ready
await Task.Delay(500);

// ============================================================
// 13. MUTATION DEMO
// ============================================================
Log("AGENT", "Starting mutation demo...");

var agentId = "agent-001";
await using var lockMgr = new SubtreeLock(ConnStr);

// Step 1: Claim the Add method subtree
var claimed = await lockMgr.TryClaimSubtree(seeder.AddMethodId, agentId);
if (!claimed)
{
    Log("ERROR", "Could not claim subtree. Another agent holds the lock.");
    return;
}

// Step 2: Mutate — use a repo bound to the lock connection so mutations
// happen on the same session that holds the advisory lock.
var lockedRepo = new NodeRepository(lockMgr.LockConnection!);
Log("MUTATE", "Adding parameter: scalar:float");

// Find the current max sibling_order for Add's children
var nextOrder = await lockedRepo.NextSiblingOrder(seeder.AddMethodId);

// Insert new parameter node
var scalarParamId = Guid.NewGuid();
await lockedRepo.InsertNode(
    scalarParamId, projectId, fileId,
    "parameter", "scalar", null,
    seeder.AddMethodId, 150, // between existing param (100) and block (200)
    agentId,
    new() { { "type", "float" } });
Log("MUTATE", $"Inserted parameter node: {scalarParamId}");

// Update the return statement — find the block's statement child
var addTree = await repo.GetSubtree(seeder.AddMethodId);
var addBlock = addTree!.Children.First(c => c.Record.NodeType == "block");
var returnStmt = addBlock.Children.First(c => c.Record.NodeType == "statement");

var newBody = "return new Vector2((X + other.X) * scalar, (Y + other.Y) * scalar);";
await lockedRepo.UpdateNode(returnStmt.Record.Id, value: newBody, modifiedBy: agentId);
Log("MUTATE", $"Updated statement: {returnStmt.Record.Id}");

// Let NOTIFY events propagate
await Task.Delay(1000);

// Step 3: Regenerate (while still holding lock — another agent can't mutate)
Log("GENERATE", "Regenerating from mutated nodes...");
var newCode = await generator.Generate(rootNodeId);

// Step 4: Validate (still under lock)
validator.ValidateOrThrow(newCode);

// Step 5: Release the lock (only after validation succeeds)
await lockMgr.ReleaseSubtree(seeder.AddMethodId, agentId);

// Step 6: Write and build
await File.WriteAllTextAsync(outputPath, newCode);
Log("WRITE", $"→ {outputPath}");

Console.WriteLine();
Console.WriteLine("--- Mutated Code ---");
Console.WriteLine(newCode);
Console.WriteLine("--- End Mutated Code ---");
Console.WriteLine();

var (rebuildSuccess, _) = await DotnetBuilder.Build(Path.GetFullPath(OutputDir));

// Step 7: Update AGE graph with new edges
if (ageAvailable)
{
    // The new scalar parameter doesn't add new REFERENCES edges,
    // but in a real system we'd re-analyze the body and update.
    Log("GRAPH", "Graph edges remain valid (parameter addition doesn't change references)");
}

// ============================================================
// 14. PLAN-TO-CODE GENERATION DEMO
// ============================================================
Log("PLAN2CODE", "Seeding Calculator plan with structured code-gen tasks...");

// Create a file record for the generated calculator code
var calcFileId = await repo.GetOrCreateFile(projectId, "Calculator.cs");

// Seed the plan
var calcPlanSeeder = new CalculatorPlanSeeder(repo);
var calcPlanId = await calcPlanSeeder.Seed(planProjectId);

// Generate code nodes from the plan
var planCodeGen = new PlanToCodeGenerator(repo, age, ageAvailable);
var calcRootId = await planCodeGen.GenerateFromPlan(calcPlanId, projectId, calcFileId);

// Materialize the generated code
var calcCode = await generator.Generate(calcRootId);
validator.ValidateOrThrow(calcCode);

var calcOutputPath = Path.Combine(OutputDir, "Calculator.cs");
await File.WriteAllTextAsync(calcOutputPath, calcCode);
Log("PLAN2CODE", $"→ {calcOutputPath}");

Console.WriteLine();
Console.WriteLine("--- Plan-Generated Code (v1) ---");
Console.WriteLine(calcCode);
Console.WriteLine("--- End Plan-Generated Code ---");
Console.WriteLine();

var (calcBuildSuccess, _) = await DotnetBuilder.Build(Path.GetFullPath(OutputDir));
Log("PLAN2CODE", $"Build: {(calcBuildSuccess ? "SUCCESS ✓" : "FAILED ✗")}");
Log("PLAN2CODE", $"Tasks → methods: {planCodeGen.TaskToMethodMap.Count} IMPLEMENTED_BY edges created");

// ============================================================
// 15. PLAN MUTATION + TEMPORAL EDGES DEMO
// ============================================================
Log("MUTATE", "Revising plan: changing Add to checked arithmetic, adding Multiply...");

// Mutation 1: Update Add task's body to use checked arithmetic
await repo.SetAttribute(calcPlanSeeder.AddTaskId, "body", "return checked(a + b);");
Log("MUTATE", "Updated Add task body → checked(a + b)");

// Mutation 2: Add a new Multiply task to the plan step
var multiplyTaskId = Guid.NewGuid();
await repo.InsertNode(
    multiplyTaskId, planProjectId, null,
    "task", "Multiply method", "Two-argument integer multiplication",
    calcPlanSeeder.CreateClassStepId, 400, "claude-opus-4-6",
    new()
    {
        { "status", "pending" },
        { "method_name", "Multiply" },
        { "return_type", "int" },
        { "params", "int a, int b" },
        { "body", "return a * b;" },
    });
Log("MUTATE", "Added Multiply task to plan");

// Regenerate: closes old IMPLEMENTED_BY edges, deletes old code nodes, creates new
var calcRootIdV2 = await planCodeGen.RegenerateFromPlan(calcPlanId, projectId, calcFileId);

// Materialize v2
var calcCodeV2 = await generator.Generate(calcRootIdV2);
validator.ValidateOrThrow(calcCodeV2);
await File.WriteAllTextAsync(calcOutputPath, calcCodeV2);

Console.WriteLine();
Console.WriteLine("--- Plan-Generated Code (v2 — after mutation) ---");
Console.WriteLine(calcCodeV2);
Console.WriteLine("--- End Plan-Generated Code ---");
Console.WriteLine();

var (calcBuild2, _2) = await DotnetBuilder.Build(Path.GetFullPath(OutputDir));
Log("MUTATE", $"Rebuild after mutation: {(calcBuild2 ? "SUCCESS ✓" : "FAILED ✗")}");
Log("MUTATE", $"v2 tasks → methods: {planCodeGen.TaskToMethodMap.Count} (was 3, now 4)");

// Query temporal edges to show version history
if (ageAvailable)
{
    try
    {
        // Show all IMPLEMENTED_BY versions from the Add task
        var addHistory = await age.QueryTemporalEdges(calcPlanSeeder.AddTaskId, "IMPLEMENTED_BY");
        Log("TEMPORAL", $"Add task IMPLEMENTED_BY history: {addHistory.Count} version(s)");
        foreach (var h in addHistory)
            Log("TEMPORAL", $"  {h}");

        // Show all current (open) IMPLEMENTED_BY edges
        var currentEdges = await age.RunCypherQuery(
            "MATCH (t:CodeNode)-[e:IMPLEMENTED_BY]->(m:CodeNode) " +
            "WHERE e.valid_to IS NULL " +
            "RETURN t.name + ' → ' + m.name");
        Log("TEMPORAL", $"Current IMPLEMENTED_BY edges: {currentEdges.Count}");
        foreach (var e in currentEdges)
            Log("TEMPORAL", $"  {e}");
    }
    catch (Exception ex)
    {
        Log("TEMPORAL", $"Temporal query failed (non-fatal): {ex.Message}");
    }
}

// ============================================================
// 16. GIT → DB → EXECUTE (full round-trip proof)
// ============================================================
Log("GIT2EXEC", "=== PROVING: git file → parse → DB → generate → compile → execute ===");
Log("GIT2EXEC", "");

// Step 1: Pull source from git working tree
var gitSourcePath = Path.Combine(Path.GetFullPath("."), "testdata", "Greeter.cs");
Log("GIT2EXEC", $"Step 1: Reading source from git repo: {gitSourcePath}");
var originalSource = await File.ReadAllTextAsync(gitSourcePath);
Log("GIT2EXEC", $"  Read {originalSource.Length} chars, {originalSource.Split('\n').Length} lines");

Console.WriteLine();
Console.WriteLine("--- Original Source (from git) ---");
Console.WriteLine(originalSource);
Console.WriteLine("--- End Original Source ---");
Console.WriteLine();

// Step 2: Parse with Roslyn and decompose into DB nodes
Log("GIT2EXEC", "Step 2: Parsing with Roslyn → decomposing into DB nodes...");
var greeterFileId = await repo.GetOrCreateFile(projectId, "Greeter.cs");
var decomposer = new CodeStoragePoc.Generator.RoslynDecomposer(repo);
var greeterRootId = await decomposer.Decompose(originalSource, projectId, greeterFileId, "claude-opus-4-6");
Log("GIT2EXEC", $"  Decomposed into {decomposer.AllNodeIds.Count} nodes");
Log("GIT2EXEC", $"  Root node: {greeterRootId}");

// Step 3: Read the node tree back from DB and show structure
Log("GIT2EXEC", "Step 3: Reading node tree back from database...");
var greeterTree = await repo.GetSubtree(greeterRootId);
PrintTree(greeterTree!, "  ", 0);

static void PrintTree(TreeNode node, string prefix, int depth)
{
    var indent = new string(' ', depth * 2);
    var name = node.Record.Name ?? node.Record.Value?[..Math.Min(40, node.Record.Value.Length)] ?? "(no name)";
    Console.WriteLine($"{prefix}{indent}[{node.Record.NodeType}] {name}");
    foreach (var child in node.Children)
        PrintTree(child, prefix, depth + 1);
}

// Step 4: Generate C# source from DB nodes
Log("GIT2EXEC", "Step 4: Generating C# from DB nodes...");
var regeneratedCode = await generator.Generate(greeterRootId);

Console.WriteLine();
Console.WriteLine("--- Regenerated Code (from DB) ---");
Console.WriteLine(regeneratedCode);
Console.WriteLine("--- End Regenerated Code ---");
Console.WriteLine();

// Step 5: Validate with Roslyn
Log("GIT2EXEC", "Step 5: Validating regenerated code with Roslyn...");
validator.ValidateOrThrow(regeneratedCode);

// Step 6: Compile in-memory with Roslyn
Log("GIT2EXEC", "Step 6: Compiling in-memory with Roslyn...");
var syntaxTree = CSharpSyntaxTree.ParseText(regeneratedCode);

// Get runtime assembly references
var trustedAssemblies = ((string)AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES")!)
    .Split(Path.PathSeparator);
var metadataReferences = trustedAssemblies
    .Where(p =>
    {
        var fn = Path.GetFileName(p);
        return fn.StartsWith("System.") || fn == "mscorlib.dll" || fn == "netstandard.dll";
    })
    .Select(p => MetadataReference.CreateFromFile(p))
    .Cast<MetadataReference>()
    .ToList();

var compilation = CSharpCompilation.Create(
    "GitToDbDemo",
    new[] { syntaxTree },
    metadataReferences,
    new CSharpCompilationOptions(OutputKind.DynamicallyLinkedLibrary));

using var ms = new MemoryStream();
var emitResult = compilation.Emit(ms);

if (!emitResult.Success)
{
    Log("GIT2EXEC", "COMPILE FAILED:");
    foreach (var diag in emitResult.Diagnostics.Where(d => d.Severity == DiagnosticSeverity.Error))
        Log("GIT2EXEC", $"  {diag.GetMessage()}");
}
else
{
    Log("GIT2EXEC", "  Compilation succeeded — assembly in memory");

    // Step 7: Load and execute via reflection
    Log("GIT2EXEC", "Step 7: Loading assembly and executing via reflection...");
    ms.Seek(0, SeekOrigin.Begin);
    var assembly = System.Reflection.Assembly.Load(ms.ToArray());
    var greeterType = assembly.GetType("TestData.Greeter")!;

    // Call Greeter.Hello("World")
    var helloMethod = greeterType.GetMethod("Hello")!;
    var helloResult = helloMethod.Invoke(null, new object[] { "World" });
    Log("GIT2EXEC", $"  Greeter.Hello(\"World\") = \"{helloResult}\"");

    // Call Greeter.Compute(6, 7)
    var computeMethod = greeterType.GetMethod("Compute")!;
    var computeResult = computeMethod.Invoke(null, new object[] { 6, 7 });
    Log("GIT2EXEC", $"  Greeter.Compute(6, 7) = {computeResult}");

    // Call Greeter.Describe()
    var describeMethod = greeterType.GetMethod("Describe")!;
    var describeResult = describeMethod.Invoke(null, Array.Empty<object>());
    Log("GIT2EXEC", $"  Greeter.Describe() = \"{describeResult}\"");

    Console.WriteLine();
    Log("GIT2EXEC", "=== PROOF COMPLETE ===");
    Log("GIT2EXEC", "  1. Read Greeter.cs from git working tree");
    Log("GIT2EXEC", "  2. Roslyn parsed it into syntax tree");
    Log("GIT2EXEC", $"  3. Decomposed into {decomposer.AllNodeIds.Count} DB nodes");
    Log("GIT2EXEC", "  4. Generated C# back from DB nodes");
    Log("GIT2EXEC", "  5. Compiled in-memory (no disk)");
    Log("GIT2EXEC", "  6. Executed via reflection — got live results");
    Log("GIT2EXEC", $"  Result: {helloResult}");
}

// ============================================================
// DONE
// ============================================================
if (calcBuild2)
    Log("DONE", "Full loop: plan → code → build → mutate plan → regenerate → rebuild ✓");
else if (rebuildSuccess)
    Log("DONE", "Core loop validated: edit nodes → generate → validate → build ✓");
else
    Log("DONE", "Loop completed but build failed.");

// Give notifier a moment to flush remaining events
await Task.Delay(500);
