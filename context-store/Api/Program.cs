// CodeStoragePoc.Api - Minimal API Entry Point
//
// Wires up DI, CORS, and endpoints for the ideation assistant test app.
// Serves as the HTTP layer between the React frontend and the
// existing CodeStoragePoc data/context pipeline.
//
// Depends on: CodeStoragePoc (NodeRepository, ContextAssembler, PromptBuilder, IntentClassifier)
//             ChatService, ExtractionService, SkillLoader, SystemMessageBus
// Used by:    React frontend (port 5179)

using CodeStoragePoc.AgentCoordination;
using CodeStoragePoc.Api.Services;
using CodeStoragePoc.DbLayer;
using CodeStoragePoc.ContextRouter;
using Npgsql;

var builder = WebApplication.CreateBuilder(args);

// --- Configuration ---
var connStr = Environment.GetEnvironmentVariable("CODESTORAGE_CONNSTR")
    ?? "Host=localhost;Port=5433;Database=code_storage;Username=postgres;Password=postgres;Maximum Pool Size=20";

if (Environment.GetEnvironmentVariable("CODESTORAGE_CONNSTR") == null)
    Console.WriteLine("[WARN] CODESTORAGE_CONNSTR not set — using POC default credentials");

// --- Plan contract v1: opt-in local profile only. HEKATE_PLAN_CONTRACT=1 with an unsafe
// (non-loopback / dispatcher-on) configuration throws here, before any managed DDL. ---
CodeStoragePoc.PlanContracts.PlanContractGateResult planContract;
try
{
    planContract = CodeStoragePoc.PlanContracts.PlanContractGate.Evaluate(Environment.GetEnvironmentVariable);
}
catch (CodeStoragePoc.PlanContracts.PlanContractConfigurationException ex)
{
    Console.Error.WriteLine($"[FATAL] {ex.Message}");
    Environment.Exit(78);   // EX_CONFIG
    return;
}
Console.WriteLine($"[INFO] Plan contract: {planContract.Mode} ({planContract.Reason})");

// --- Schema migration (idempotent — safe to run on every startup) ---
await CodeStoragePoc.DbLayer.Schema.Initialize(connStr);
if (planContract.Mode == CodeStoragePoc.PlanContracts.PlanContractMode.Enabled)
{
    try { await CodeStoragePoc.PlanContracts.PlanStoreSchema.Ensure(connStr); }
    catch (CodeStoragePoc.PlanContracts.PlanContractConfigurationException ex)
    {
        Console.Error.WriteLine($"[FATAL] {ex.Message}");
        Environment.Exit(78);
        return;
    }
}

// --- DI Registration ---
var repo = new NodeRepository(connStr);
var embeddingService = new EmbeddingService();
var age = new CodeStoragePoc.GraphLayer.AgeLayer(connStr);
var assembler = new ContextAssembler(repo, connStr, embeddingService, age);
var promptBuilder = new PromptBuilder();
var classifier = new IntentClassifier();
// AppContext.BaseDirectory = Api/bin/Debug/net8.0/ → 5 levels up to project root
var skillsPath = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "..", "tools", "skills", "skills.json"));
if (!File.Exists(skillsPath))
    skillsPath = Path.GetFullPath(Path.Combine(Directory.GetCurrentDirectory(), "..", "tools", "skills", "skills.json"));
Console.WriteLine($"[INFO] Skills path: {skillsPath} (exists: {File.Exists(skillsPath)})");
var skillLoader = new SkillLoader(skillsPath);

builder.Services.AddSingleton(embeddingService);
builder.Services.AddSingleton(repo);
builder.Services.AddSingleton(assembler);
builder.Services.AddSingleton(promptBuilder);
builder.Services.AddSingleton(classifier);
builder.Services.AddSingleton(skillLoader);
var extractionService = new ExtractionService(repo, connStr, embeddingService, age);
builder.Services.AddSingleton(extractionService);
var interpreterService = new InterpreterService(connStr, classifier);
builder.Services.AddSingleton(interpreterService);
var permissionService = new PermissionService(connStr, skillLoader);
var pendingActionService = new PendingActionService(connStr);
builder.Services.AddSingleton(new ChatService(repo, assembler, promptBuilder, classifier, interpreterService, skillLoader, extractionService, connStr, embeddingService, age, permissionService, pendingActionService));
builder.Services.AddSingleton(new PlanService(repo, connStr));
builder.Services.AddSingleton(new NodeService(connStr));
builder.Services.AddSingleton(permissionService);
builder.Services.AddSingleton(pendingActionService);
builder.Services.AddSingleton<SystemMessageBus>();
builder.Services.AddSingleton(age);
builder.Services.AddSingleton(new CodeService(repo, age, connStr));

// --- JSON serialization ---
builder.Services.ConfigureHttpJsonOptions(options =>
{
    options.SerializerOptions.PropertyNamingPolicy = System.Text.Json.JsonNamingPolicy.CamelCase;
});

// --- CORS ---
builder.Services.AddCors(options =>
{
    options.AddDefaultPolicy(policy =>
        policy.WithOrigins("http://localhost:5179", "http://192.168.1.164:5179")
              .AllowAnyHeader()
              .AllowAnyMethod());
});

var app = builder.Build();

app.UseCors();
// Fence violations on managed plan nodes (from any endpoint) become 409 managed_plan_protected.
CodeStoragePoc.Api.PlanContractEndpoints.UseManagedPlanFenceErrors(app);
if (planContract.Mode == CodeStoragePoc.PlanContracts.PlanContractMode.Enabled)
    CodeStoragePoc.Api.PlanContractEndpoints.MapPlanContractEndpoints(app, new CodeStoragePoc.PlanContracts.PlanStore(connStr));

// --- Health check (real: Postgres + AGE + Ollama + outbox) ---
app.MapGet("/api/health", async (CodeStoragePoc.GraphLayer.AgeLayer ageLayer) =>
{
    var health = new Dictionary<string, object> { ["status"] = "ok" };
    try
    {
        await using var hconn = new NpgsqlConnection(connStr);
        await hconn.OpenAsync();
        await using var hcmd = new NpgsqlCommand("SELECT 1", hconn);
        await hcmd.ExecuteScalarAsync();
        health["postgres"] = true;
    }
    catch { health["postgres"] = false; health["status"] = "degraded"; }

    try
    {
        await using var aconn = new NpgsqlConnection(connStr);
        await aconn.OpenAsync();
        await using var acmd = new NpgsqlCommand("SELECT * FROM ag_catalog.ag_graph LIMIT 1", aconn);
        await acmd.ExecuteScalarAsync();
        health["age"] = true;
    }
    catch { health["age"] = false; health["status"] = "degraded"; }

    try
    {
        using var hc = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
        var ollamaUrl = Environment.GetEnvironmentVariable("OLLAMA_URL") ?? "http://localhost:11434";
        var r = await hc.GetAsync(ollamaUrl + "/");
        health["ollama"] = r.IsSuccessStatusCode;
    }
    catch { health["ollama"] = false; }

    try { health["outbox_pending"] = await ageLayer.GetOutboxPendingCount(); }
    catch { health["outbox_pending"] = -1; }

    return Results.Ok(health);
});

// --- Readiness (used by scripts/local/hekate-local.ps1): Postgres + required
// extensions + a real AGE Cypher query + core tables. Optional inference (Ollama)
// is deliberately excluded. 200 when ready, 503 otherwise. Schema.Initialize runs
// before the app serves, so a 200 means it completed, not that it was atomic.
app.MapGet("/api/health/ready", async () =>
{
    var checks = new Dictionary<string, object>
    {
        ["postgres"] = false, ["extensions"] = false, ["age_graph"] = false, ["schema"] = false,
        ["dispatcher_disabled"] = Environment.GetEnvironmentVariable("HEKATE_DISABLE_DISPATCHER") == "1",
    };
    var step = "postgres";
    try
    {
        await using var rconn = new NpgsqlConnection(connStr);
        await rconn.OpenAsync();
        checks["postgres"] = true;

        step = "extensions";
        await using (var ext = new NpgsqlCommand("SELECT count(*) FROM pg_extension WHERE extname IN ('age', 'vector')", rconn))
            checks["extensions"] = Convert.ToInt64(await ext.ExecuteScalarAsync()) == 2;

        step = "age_graph";
        await using (var load = new NpgsqlCommand("LOAD 'age'; SET search_path = ag_catalog, \"$user\", public;", rconn))
            await load.ExecuteNonQueryAsync();
        // agtype has no Npgsql mapping; cast to text so the probe reads a plain value.
        await using (var cypher = new NpgsqlCommand("SELECT v::text FROM cypher('code_graph', $$ MATCH (n) RETURN count(n) $$) as (v agtype);", rconn))
        {
            await cypher.ExecuteScalarAsync();
            checks["age_graph"] = true;
        }

        step = "schema";
        await using (var tables = new NpgsqlCommand(
            "SELECT to_regclass('public.nodes') IS NOT NULL AND to_regclass('public.node_attributes') IS NOT NULL", rconn))
            checks["schema"] = (bool)(await tables.ExecuteScalarAsync())!;
    }
    catch (Exception ex)
    {
        // Stable category only: this route is reachable on the unauthenticated
        // production bind, so raw driver messages stay in the local console.
        var sqlState = (ex as PostgresException)?.SqlState;
        checks["failed_step"] = step;
        checks["failure"] = sqlState == "28P01" ? "auth_failed" : $"{step}_unavailable";
        Console.WriteLine($"[WARN] Readiness failed at {step}: {ex.GetType().Name}{(sqlState is null ? "" : $" (SQLSTATE {sqlState})")}");
    }

    var ready = (bool)checks["postgres"] && (bool)checks["extensions"] && (bool)checks["age_graph"] && (bool)checks["schema"];
    checks["ready"] = ready;
    return ready ? Results.Ok(checks) : Results.Json(checks, statusCode: 503);
});

// --- Graph sync outbox background worker (drains every 3s) ---
var outboxTimer = new System.Threading.Timer(async _ =>
{
    try { await age.DrainOutbox(); }
    catch (Exception ex) { Console.WriteLine($"[GRAPH]    Outbox drain error: {ex.Message}"); }
}, null, TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(3));

// --- Chat endpoint (SSE streaming) ---
app.MapPost("/api/chat", async (HttpContext http, ChatService chat) =>
{
    var request = await http.Request.ReadFromJsonAsync<ChatRequest>();
    if (request == null || string.IsNullOrWhiteSpace(request.Message))
        return Results.BadRequest(new { error = "Message is required" });

    http.Response.ContentType = "text/event-stream";
    http.Response.Headers.CacheControl = "no-cache";
    http.Response.Headers.Connection = "keep-alive";

    await chat.StreamResponse(request.Message, request.ConversationId, http.Response, http.RequestAborted);
    return Results.Empty;
});

// --- Read endpoints ---
app.MapGet("/api/conversations", async (ChatService chat) =>
    Results.Ok(await chat.ListConversations()));

app.MapGet("/api/conversation/{id:guid}", async (Guid id, ChatService chat) =>
    Results.Ok(await chat.GetConversation(id)));

app.MapGet("/api/threads/{conversationId:guid}", async (Guid conversationId, ChatService chat) =>
    Results.Ok(await chat.GetThreads(conversationId)));

app.MapGet("/api/stats/{conversationId:guid}", async (Guid conversationId, ChatService chat) =>
    Results.Ok(await chat.GetStats(conversationId)));

app.MapGet("/api/conversation/{conversationId:guid}/debug", async (Guid conversationId, NodeService nodes) =>
    Results.Ok(await nodes.GetLatestInterpretation(conversationId)));

// --- Plan endpoints ---
app.MapGet("/api/plans", async (PlanService plans) =>
    Results.Ok(await plans.ListPlans()));

app.MapGet("/api/plan/{id:guid}", async (Guid id, PlanService plans) =>
{
    var tree = await plans.GetPlanTree(id);
    if (tree == null) return Results.NotFound(new { error = "Plan not found" });
    return Results.Ok(tree);
});

// --- Node endpoints (workspace view) ---
app.MapGet("/api/nodes/roots", async (NodeService nodes) =>
    Results.Ok(await nodes.GetRootNodes()));

app.MapGet("/api/node/{id:guid}", async (Guid id, NodeService nodes) =>
{
    var detail = await nodes.GetNodeDetail(id);
    if (detail == null) return Results.NotFound(new { error = "Node not found" });
    return Results.Ok(detail);
});

app.MapGet("/api/node/{id:guid}/children", async (Guid id, NodeService nodes) =>
    Results.Ok(await nodes.GetNodeChildren(id)));

app.MapGet("/api/node/{id:guid}/edges", async (Guid id, NodeService nodes) =>
    Results.Ok(await nodes.GetNodeEdges(id)));

// --- Node mutation endpoints ---
app.MapPut("/api/node/{id:guid}", async (Guid id, UpdateNodeRequest req, NodeService nodes) =>
{
    await nodes.UpdateNodeFull(id, req.Name, req.Value);
    var detail = await nodes.GetNodeDetail(id);
    return detail != null ? Results.Ok(detail) : Results.NotFound();
});

app.MapPut("/api/node/{id:guid}/attributes", async (Guid id, UpdateAttrsRequest req, NodeService nodes) =>
{
    await nodes.UpdateAttributes(id, req.Attributes);
    return Results.Ok(req.Attributes);
});

app.MapPost("/api/node/{parentId:guid}/children", async (Guid parentId, CreateNodeRequest req, NodeService nodes, CodeStoragePoc.GraphLayer.AgeLayer ageLayer) =>
{
    var newId = await nodes.CreateChildNode(parentId, req.NodeType, req.Name, req.Value, req.Attributes);
    await ageLayer.SyncVertex(newId, req.NodeType, req.Name);
    var detail = await nodes.GetNodeDetail(newId);
    return detail != null ? Results.Ok(detail) : Results.StatusCode(500);
});

// Root-level node creation (no parent, just project) — used by Hekate indexer
app.MapPost("/api/project/{projectId:guid}/nodes", async (Guid projectId, CreateNodeRequest req, NodeRepository repo, CodeStoragePoc.GraphLayer.AgeLayer ageLayer) =>
{
    var nodeId = Guid.NewGuid();
    await repo.InsertNode(nodeId, projectId, null, req.NodeType, req.Name, req.Value,
        null, 0, req.Attributes?.GetValueOrDefault("modified_by"), req.Attributes);
    await ageLayer.SyncVertex(nodeId, req.NodeType, req.Name);
    return Results.Ok(new { id = nodeId, nodeType = req.NodeType, name = req.Name });
});

// --- Scoped chat (chat about a specific node) ---
app.MapPost("/api/node/{id:guid}/chat", async (Guid id, HttpContext http, ChatService chat, NodeService nodes) =>
{
    var request = await http.Request.ReadFromJsonAsync<ChatRequest>();
    if (request == null || string.IsNullOrWhiteSpace(request.Message))
        return Results.BadRequest(new { error = "Message is required" });

    // Prefix message with node context so the model knows what we're talking about
    var detail = await nodes.GetNodeDetail(id);
    if (detail == null) return Results.NotFound(new { error = "Node not found" });

    http.Response.ContentType = "text/event-stream";
    http.Response.Headers.CacheControl = "no-cache";
    http.Response.Headers.Connection = "keep-alive";

    // Prepend node focus to the message
    var scopedMessage = $"[Focused on node: {id}] {request.Message}";
    await chat.StreamResponse(scopedMessage, request.ConversationId, http.Response, http.RequestAborted);
    return Results.Empty;
});

// --- Preview endpoint (dry-run pipeline) ---
app.MapPost("/api/preview", async (HttpContext http, ChatService chat) =>
{
    var request = await http.Request.ReadFromJsonAsync<ChatRequest>();
    if (request == null || string.IsNullOrWhiteSpace(request.Message))
        return Results.BadRequest(new { error = "Message is required" });

    var result = await chat.PreviewAsync(request.Message, request.ConversationId);
    return Results.Ok(result);
});

// --- Models endpoint ---
app.MapGet("/api/models", async (ChatService chat) =>
    Results.Ok(await chat.GetAvailableModels()));

// --- Command endpoints ---
app.MapPost("/api/command/park", async (CommandRequest req, ChatService chat) =>
{
    await chat.ParkIdea(req.NodeId);
    return Results.Ok(new { status = "parked" });
});

app.MapPost("/api/command/resume", async (CommandRequest req, ChatService chat) =>
{
    await chat.ResumeIdea(req.NodeId);
    return Results.Ok(new { status = "resumed" });
});

// --- Permission endpoints ---
app.MapGet("/api/permissions/{conversationId:guid}", async (Guid conversationId, PermissionService perms) =>
    Results.Ok(await perms.GetPermissionConfig(conversationId)));

app.MapPost("/api/permissions/{conversationId:guid}", async (Guid conversationId, SetPermissionRequest req, PermissionService perms) =>
{
    if (req.Model != null)
        await perms.SetModelPermission(conversationId, req.Model, (PermissionLevel)Math.Clamp(req.Level, 0, 3));
    else
        await perms.SetConversationPermission(conversationId, (PermissionLevel)Math.Clamp(req.Level, 0, 3));
    return Results.Ok(await perms.GetPermissionConfig(conversationId));
});

// --- Pending action endpoints ---
app.MapGet("/api/actions/{conversationId:guid}", async (Guid conversationId, PendingActionService actions) =>
    Results.Ok(await actions.ListPending(conversationId)));

app.MapPost("/api/action/{id:guid}/approve", async (Guid id, PendingActionService actions) =>
{
    var result = await actions.ApproveAction(id);
    if (result == null) return Results.NotFound(new { error = "Action not found or already resolved" });
    return Results.Ok(new { status = "approved", actionId = result.Id, skill = result.Skill, model = result.Model });
});

app.MapPost("/api/action/{id:guid}/deny", async (Guid id, PendingActionService actions) =>
{
    var result = await actions.DenyAction(id);
    if (result == null) return Results.NotFound(new { error = "Action not found or already resolved" });
    return Results.Ok(new { status = "denied", actionId = result.Id, skill = result.Skill, model = result.Model });
});

// --- Code decompose/materialize endpoints ---
app.MapPost("/api/code/decompose", async (DecomposeRequest req, CodeService code) =>
{
    try
    {
        if (!string.IsNullOrWhiteSpace(req.SourceText))
        {
            var result = await code.DecomposeSource(req.ProjectId, req.FilePath, req.SourceText);
            return Results.Ok(result);
        }
        else
        {
            var result = await code.DecomposeFile(req.ProjectId, req.FilePath);
            return Results.Ok(result);
        }
    }
    catch (FileNotFoundException ex)
    {
        return Results.NotFound(new { error = ex.Message });
    }
});

app.MapPost("/api/code/materialize", async (MaterializeRequest req, CodeService code) =>
{
    try
    {
        string source;
        if (req.FileId.HasValue)
            source = await code.MaterializeFile(req.FileId.Value);
        else if (req.RootNodeId.HasValue)
            source = await code.MaterializeNode(req.RootNodeId.Value);
        else
            return Results.BadRequest(new { error = "FileId or RootNodeId is required" });

        return Results.Ok(new { source });
    }
    catch (InvalidOperationException ex)
    {
        return Results.NotFound(new { error = ex.Message });
    }
});

app.MapGet("/api/code/files/{projectId:guid}", async (Guid projectId, CodeService code) =>
    Results.Ok(await code.ListFiles(projectId)));

// --- Project endpoints (used by Hekate indexer) ---
app.MapGet("/api/projects", async (string? name) =>
{
    await using var conn = new NpgsqlConnection(connStr);
    await conn.OpenAsync();
    if (!string.IsNullOrWhiteSpace(name))
    {
        await using var cmd = new NpgsqlCommand("SELECT id, name, root_path FROM projects WHERE name = @name", conn);
        cmd.Parameters.AddWithValue("name", name);
        await using var reader = await cmd.ExecuteReaderAsync();
        if (await reader.ReadAsync())
            return Results.Ok(new { id = reader.GetGuid(0), name = reader.GetString(1), rootPath = reader.IsDBNull(2) ? null : reader.GetString(2) });
        return Results.NotFound(new { error = $"Project '{name}' not found" });
    }
    var projects = new List<object>();
    await using var listCmd = new NpgsqlCommand("SELECT id, name, root_path FROM projects ORDER BY name", conn);
    await using var listReader = await listCmd.ExecuteReaderAsync();
    while (await listReader.ReadAsync())
        projects.Add(new { id = listReader.GetGuid(0), name = listReader.GetString(1), rootPath = listReader.IsDBNull(2) ? null : listReader.GetString(2) });
    return Results.Ok(projects);
});

app.MapPost("/api/projects", async (CreateProjectRequest req) =>
{
    await using var conn = new NpgsqlConnection(connStr);
    await conn.OpenAsync();
    await using var check = new NpgsqlCommand("SELECT id FROM projects WHERE name = @name", conn);
    check.Parameters.AddWithValue("name", req.Name);
    var existing = await check.ExecuteScalarAsync();
    if (existing != null)
        return Results.Ok(new { id = (Guid)existing, name = req.Name, created = false });
    var id = Guid.NewGuid();
    await using var cmd = new NpgsqlCommand("INSERT INTO projects (id, name, root_path) VALUES (@id, @name, @path)", conn);
    cmd.Parameters.AddWithValue("id", id);
    cmd.Parameters.AddWithValue("name", req.Name);
    cmd.Parameters.AddWithValue("path", (object?)req.RootPath ?? DBNull.Value);
    await cmd.ExecuteNonQueryAsync();
    return Results.Ok(new { id, name = req.Name, created = true });
});

// --- Provenance-based node deletion (used by Hekate indexer for idempotent re-indexing) ---
app.MapDelete("/api/project/{projectId:guid}/nodes", async (Guid projectId, string? provenance, NodeRepository repo) =>
{
    if (string.IsNullOrWhiteSpace(provenance))
        return Results.BadRequest(new { error = "provenance query parameter is required" });

    var deleted = await repo.DeleteNodesByProvenance(projectId, provenance);
    return Results.Ok(new { deleted });
});

// --- Edge creation endpoint (used by Hekate indexer) ---
app.MapPost("/api/code/edge", async (CreateEdgeRequest req, CodeStoragePoc.GraphLayer.AgeLayer ageLayer) =>
{
    try
    {
        if (req.Provenance != null)
            await ageLayer.CreateTemporalEdge(req.FromNodeId, req.ToNodeId, req.EdgeType, req.Provenance);
        else
            await ageLayer.CreateEdge(req.FromNodeId, req.ToNodeId, req.EdgeType);
        return Results.Ok(new { success = true });
    }
    catch (ArgumentException ex)
    {
        return Results.BadRequest(new { error = ex.Message });
    }
});

// --- Brain service endpoints (called by orchestration) ---

app.MapPost("/api/brain/resolve", async (HttpContext http, InterpreterService interpreter) =>
{
    var req = await http.Request.ReadFromJsonAsync<BrainResolveRequest>();
    if (req == null || string.IsNullOrWhiteSpace(req.Message))
        return Results.BadRequest(new { error = "Message is required" });

    var resolved = await interpreter.ResolveAsync(req.Message, req.ConversationId);
    return Results.Ok(resolved);
});

app.MapPost("/api/brain/assemble", async (HttpContext http, ContextAssembler assembler) =>
{
    var req = await http.Request.ReadFromJsonAsync<BrainAssembleRequest>();
    if (req == null || req.ResolvedSubject == null)
        return Results.BadRequest(new { error = "ResolvedSubject is required" });

    if (!req.ConversationId.HasValue)
        return Results.BadRequest(new { error = "ConversationId is required" });

    var state = await assembler.AssembleFromSubject(req.ResolvedSubject, req.ConversationId.Value);
    return Results.Ok(state);
});

app.MapPost("/api/brain/extract", async (HttpContext http, ExtractionService extraction) =>
{
    var req = await http.Request.ReadFromJsonAsync<BrainExtractRequest>();
    if (req == null || string.IsNullOrWhiteSpace(req.ResponseText))
        return Results.BadRequest(new { error = "ResponseText is required" });

    if (!req.ConversationId.HasValue || !req.ThreadId.HasValue)
        return Results.BadRequest(new { error = "ConversationId and ThreadId are required" });

    var result = await extraction.ExtractFromResponse(req.ResponseText, req.ConversationId.Value, req.ThreadId.Value);
    return Results.Ok(new { items = result.Items, count = result.Count });
});

app.MapPost("/api/brain/turn", async (HttpContext http, ChatService chat) =>
{
    var req = await http.Request.ReadFromJsonAsync<BrainTurnRequest>();
    if (req == null)
        return Results.BadRequest(new { error = "Request is required" });

    if (!req.ConversationId.HasValue)
        return Results.BadRequest(new { error = "ConversationId is required" });

    var turnId = await chat.StoreTurnPublic(req.ConversationId.Value, req.ThreadId, req.Speaker, req.Content);
    return Results.Ok(new { id = turnId });
});

app.MapPost("/api/brain/conversation", async (ChatService chat) =>
{
    var id = await chat.CreateConversation();
    return Results.Ok(new { id });
});

app.MapGet("/api/brain/conversation/{id:guid}", async (Guid id, ChatService chat) =>
    Results.Ok(await chat.GetConversation(id)));

app.MapGet("/api/brain/permissions/{conversationId:guid}", async (Guid conversationId, PermissionService perms) =>
    Results.Ok(await perms.GetPermissionConfig(conversationId)));

// --- System message endpoints ---
app.MapGet("/api/events", async (HttpContext http, SystemMessageBus bus) =>
{
    http.Response.ContentType = "text/event-stream";
    http.Response.Headers.CacheControl = "no-cache";
    http.Response.Headers.Connection = "keep-alive";

    var (subId, reader) = bus.Subscribe();
    var ct = http.RequestAborted;

    try
    {
        await foreach (var msg in reader.ReadAllAsync(ct))
        {
            var json = System.Text.Json.JsonSerializer.Serialize(new
            {
                level = msg.Level,
                text = msg.Text,
                conversationId = msg.ConversationId
            });
            await http.Response.WriteAsync($"event: system_message\ndata: {json}\n\n", ct);
            await http.Response.Body.FlushAsync(ct);
        }
    }
    catch (OperationCanceledException) { }
    finally
    {
        bus.Unsubscribe(subId);
    }
});

app.MapPost("/api/system-message", async (HttpContext http, SystemMessageBus bus, ChatService chat) =>
{
    var req = await http.Request.ReadFromJsonAsync<SystemMessageRequest>();
    if (req == null || string.IsNullOrWhiteSpace(req.Text))
        return Results.BadRequest(new { error = "Text is required" });

    var level = req.Level ?? "info";
    var msg = new SystemMessage(level, req.Text, req.Persist, req.ConversationId?.ToString());

    // Persist as a turn if requested and conversation exists
    if (req.Persist && req.ConversationId.HasValue)
    {
        await chat.StoreSystemTurn(req.ConversationId.Value, req.Text, level);
    }

    bus.Publish(msg);
    return Results.Ok(new { status = "sent", level, text = req.Text });
});

// --- Agent Dispatcher (event-driven agent spawning) ---
// HEKATE_DISABLE_DISPATCHER=1 (set by the local plan-only profile) keeps node
// edits from spawning claude/gemini CLI runs. Unset: behaviour unchanged.
var bus = app.Services.GetRequiredService<SystemMessageBus>();
AgentDispatcher? dispatcher = null;
if (Environment.GetEnvironmentVariable("HEKATE_DISABLE_DISPATCHER") == "1")
{
    Console.WriteLine("[INFO] Agent dispatcher disabled (HEKATE_DISABLE_DISPATCHER=1)");
}
else
{
    dispatcher = new AgentDispatcher(connStr, repo, (level, text) => bus.Publish(new SystemMessage(level, text)));
    try
    {
        await dispatcher.StartAsync();
    }
    catch (Exception ex)
    {
        Console.WriteLine($"[WARN] Agent dispatcher failed to start: {ex.Message}");
    }
}

app.Lifetime.ApplicationStopping.Register(() =>
{
    outboxTimer.Dispose();
    dispatcher?.DisposeAsync().AsTask().GetAwaiter().GetResult();
});

// --- Local-profile graceful shutdown (scripts/local/hekate-local.ps1) ---
// Mapped only when the launcher sets HEKATE_LOCAL_SHUTDOWN_TOKEN; loopback callers
// with the matching token only. Runs the normal ApplicationStopping path above.
var localShutdownToken = Environment.GetEnvironmentVariable("HEKATE_LOCAL_SHUTDOWN_TOKEN");
if (!string.IsNullOrEmpty(localShutdownToken))
{
    app.MapPost("/api/local/shutdown", (HttpContext http, IHostApplicationLifetime lifetime) =>
    {
        if (http.Connection.RemoteIpAddress is not { } remote || !System.Net.IPAddress.IsLoopback(remote))
            return Results.StatusCode(403);
        var supplied = System.Text.Encoding.UTF8.GetBytes(http.Request.Headers["X-Hekate-Local-Token"].ToString());
        var expected = System.Text.Encoding.UTF8.GetBytes(localShutdownToken);
        if (!System.Security.Cryptography.CryptographicOperations.FixedTimeEquals(supplied, expected))
            return Results.StatusCode(403);
        lifetime.StopApplication();
        return Results.Accepted();
    });
}

// HEKATE_API_URLS lets the local profile bind loopback on its own port. Unset: unchanged.
app.Run(Environment.GetEnvironmentVariable("HEKATE_API_URLS") ?? "http://0.0.0.0:5102");

// --- Request DTOs ---
record ChatRequest(string Message, Guid? ConversationId);
record CommandRequest(Guid NodeId);
record SystemMessageRequest(string Text, string? Level = "info", bool Persist = false, Guid? ConversationId = null);
record UpdateNodeRequest(string? Name, string? Value);
record UpdateAttrsRequest(Dictionary<string, string> Attributes);
record CreateNodeRequest(string NodeType, string? Name, string? Value, Dictionary<string, string>? Attributes);
record SetPermissionRequest(int Level, string? Model = null);
record DecomposeRequest(Guid ProjectId, string FilePath, string? SourceText = null);
record MaterializeRequest(Guid? FileId = null, Guid? RootNodeId = null);
record CreateEdgeRequest(Guid FromNodeId, Guid ToNodeId, string EdgeType, string? Provenance = null);
record CreateProjectRequest(string Name, string? RootPath = null);

// Brain service DTOs (called by orchestration)
record BrainResolveRequest(string Message, Guid? ConversationId = null);
record BrainAssembleRequest(CodeStoragePoc.ContextRouter.ResolvedSubject? ResolvedSubject, Guid? ConversationId = null);
record BrainExtractRequest(string ResponseText, Guid? ConversationId = null, Guid? ThreadId = null, string? Model = null);
record BrainTurnRequest(Guid? ConversationId, Guid? ThreadId, string Speaker, string Content);
