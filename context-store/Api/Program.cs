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
    ?? "Host=localhost;Port=5433;Database=code_storage;Username=postgres;Password=postgres";

if (Environment.GetEnvironmentVariable("CODESTORAGE_CONNSTR") == null)
    Console.WriteLine("[WARN] CODESTORAGE_CONNSTR not set — using POC default credentials");

// --- Schema migration (idempotent — safe to run on every startup) ---
await CodeStoragePoc.DbLayer.Schema.Initialize(connStr);

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
        policy.WithOrigins("http://localhost:5179")
              .AllowAnyHeader()
              .AllowAnyMethod());
});

var app = builder.Build();

app.UseCors();

// --- Health check ---
app.MapGet("/api/health", () => Results.Ok(new { status = "ok" }));

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
var bus = app.Services.GetRequiredService<SystemMessageBus>();
var dispatcher = new AgentDispatcher(connStr, repo, (level, text) => bus.Publish(new SystemMessage(level, text)));
try
{
    await dispatcher.StartAsync();
}
catch (Exception ex)
{
    Console.WriteLine($"[WARN] Agent dispatcher failed to start: {ex.Message}");
}

app.Lifetime.ApplicationStopping.Register(() =>
{
    dispatcher.DisposeAsync().AsTask().GetAwaiter().GetResult();
});

app.Run($"http://localhost:5102");

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
