// CodeStoragePoc.Api - Plan contract v1 endpoints (opt-in local profile only)
//
// Mapped only when PlanContractGate is Enabled (HEKATE_PLAN_CONTRACT=1 with a verified
// loopback-only configuration). Each request is also checked for a loopback caller as
// defense in depth. Every precondition (operation key, actor, expected revisions, reviewed
// revision/epoch) is REQUIRED — an omitted field is 400 missing_field, never a default.
// Responses use stable codes and named enum values; driver messages never reach clients.
//
// Depends on: PlanContracts/PlanStore, PlanRules
// Used by:    Program

using CodeStoragePoc.PlanContracts;
using Npgsql;

namespace CodeStoragePoc.Api;

public static class PlanContractEndpoints
{
    public const string MissingField = "missing_field";

    public static void MapPlanContractEndpoints(this WebApplication app, PlanStore store)
    {
        var api = app.MapGroup("/api/plan-contract/v1").AddEndpointFilter(async (ctx, next) =>
        {
            var remote = ctx.HttpContext.Connection.RemoteIpAddress;
            if (remote is null || !System.Net.IPAddress.IsLoopback(remote))
                return Results.Json(new { code = "loopback_only", message = "The plan contract API only accepts local callers." }, statusCode: 403);
            try { return await next(ctx); }
            catch (Exception ex) when (FindFence(ex) is null)
            {
                // Stable diagnostic; the exception type is logged locally, details never returned.
                Console.WriteLine($"[PLAN]     Request failed: {ex.GetType().Name}");
                return Error(500, "plan_store_error", "The plan contract store could not complete the request.");
            }
        });

        api.MapPost("/plans", async (CreatePlanRequest req) =>
        {
            if (Missing(("operationKey", req.OperationKey), ("actor", req.Actor), ("expectedStateRevision", req.ExpectedStateRevision),
                    ("rootId", req.RootId), ("projectId", req.ProjectId), ("name", req.Name)) is { } missing) return missing;
            if (!TryGate(req.DefaultGate, out var gate)) return InvalidEnum("defaultGate", req.DefaultGate);
            var r = await store.CreatePlanAsync(req.RootId!.Value, req.ProjectId!.Value, req.Name!, Content(req.Value, req.Attributes),
                gate ?? GatePolicy.Accepted, new OperationContext(req.OperationKey!, req.ExpectedStateRevision!.Value, req.Actor!));
            return await Respond(store, r);
        });

        api.MapGet("/plans/{rootId:guid}", async (Guid rootId) =>
        {
            var snap = await store.LoadAsync(rootId);
            return snap is null ? Error(404, PlanStoreErrorCodes.PlanNotFound, $"Plan {rootId} not found.") : Results.Ok(View(snap, null));
        });

        api.MapGet("/plans/{rootId:guid}/readiness", async (Guid rootId) =>
        {
            var snap = await store.LoadAsync(rootId);
            return snap is null ? Error(404, PlanStoreErrorCodes.PlanNotFound, $"Plan {rootId} not found.") : Results.Ok(ReadinessView(snap));
        });

        api.MapPost("/plans/{rootId:guid}/project", async (Guid rootId) => await Respond(store, await store.ReconcileProjectionAsync(rootId)));

        api.MapPost("/nodes/{parentId:guid}/children", async (Guid parentId, AddChildRequest req) =>
        {
            if (Missing(("operationKey", req.OperationKey), ("actor", req.Actor), ("expectedStateRevision", req.ExpectedStateRevision),
                    ("childId", req.ChildId), ("nodeType", req.NodeType), ("name", req.Name)) is { } missing) return missing;
            return await Respond(store, await store.AddChildAsync(parentId, req.ChildId!.Value, req.NodeType!, req.Name!, req.SiblingOrder ?? 0,
                Content(req.Value, req.Attributes), Ctx(req.OperationKey, req.ExpectedStateRevision, req.Actor)));
        });

        api.MapPost("/nodes/{nodeId:guid}/dependencies", async (Guid nodeId, AddDependencyRequest req) =>
        {
            if (Missing(("operationKey", req.OperationKey), ("actor", req.Actor), ("expectedStateRevision", req.ExpectedStateRevision),
                    ("predecessorId", req.PredecessorId)) is { } missing) return missing;
            if (!TryGate(req.Gate, out var gate)) return InvalidEnum("gate", req.Gate);
            return await Respond(store, await store.AddDependencyAsync(nodeId, req.PredecessorId!.Value, gate, Ctx(req.OperationKey, req.ExpectedStateRevision, req.Actor)));
        });

        api.MapDelete("/nodes/{nodeId:guid}/dependencies/{predecessorId:guid}",
            async (Guid nodeId, Guid predecessorId, string? operationKey, long? expectedStateRevision, string? actor) =>
            {
                if (Missing(("operationKey", operationKey), ("actor", actor), ("expectedStateRevision", expectedStateRevision)) is { } missing) return missing;
                return await Respond(store, await store.RemoveDependencyAsync(nodeId, predecessorId, Ctx(operationKey, expectedStateRevision, actor)));
            });

        api.MapPost("/nodes/{nodeId:guid}/transition", async (Guid nodeId, TransitionRequest req) =>
        {
            if (Missing(("operationKey", req.OperationKey), ("actor", req.Actor), ("expectedStateRevision", req.ExpectedStateRevision),
                    ("to", req.To)) is { } missing) return missing;
            WorkStatus to;
            try { to = PlanStore.ParseWork(req.To!); } catch (FormatException) { return InvalidEnum("to", req.To); }
            return await Respond(store, await store.TransitionAsync(nodeId, to, Ctx(req.OperationKey, req.ExpectedStateRevision, req.Actor),
                req.AttemptId, req.AttemptEpoch, req.ArtifactRef));
        });

        api.MapPost("/nodes/{nodeId:guid}/decide", async (Guid nodeId, DecideRequest req) =>
        {
            if (Missing(("operationKey", req.OperationKey), ("actor", req.Actor), ("expectedStateRevision", req.ExpectedStateRevision),
                    ("decision", req.Decision), ("reviewedContentRevision", req.ReviewedContentRevision),
                    ("reviewedAttemptEpoch", req.ReviewedAttemptEpoch)) is { } missing) return missing;
            AcceptanceDecision decision;
            try { decision = PlanStore.ParseDecision(req.Decision!); } catch (FormatException) { return InvalidEnum("decision", req.Decision); }
            return await Respond(store, await store.DecideAsync(nodeId, decision, req.ReviewedContentRevision!.Value, req.ReviewedArtifactRef,
                req.ReviewedAttemptEpoch!.Value, req.EvidenceRef, Ctx(req.OperationKey, req.ExpectedStateRevision, req.Actor)));
        });

        api.MapPut("/nodes/{nodeId:guid}/content", async (Guid nodeId, ContentRequest req) =>
        {
            if (Missing(("operationKey", req.OperationKey), ("actor", req.Actor), ("expectedStateRevision", req.ExpectedStateRevision),
                    ("expectedContentRevision", req.ExpectedContentRevision)) is { } missing) return missing;
            return await Respond(store, await store.ReviseContentAsync(nodeId, Content(req.Value, req.Attributes), req.ExpectedContentRevision!.Value,
                Ctx(req.OperationKey, req.ExpectedStateRevision, req.Actor)));
        });
    }

    /// <summary>Maps fence violations (SQLSTATE HP409) from ANY endpoint to 409 managed_plan_protected.</summary>
    public static void UseManagedPlanFenceErrors(this WebApplication app) =>
        app.Use(async (ctx, next) =>
        {
            try { await next(ctx); }
            catch (Exception ex) when (FindFence(ex) is { } pe && !ctx.Response.HasStarted)
            {
                ctx.Response.Clear();
                ctx.Response.StatusCode = 409;
                await ctx.Response.WriteAsJsonAsync(new { code = PlanStoreErrorCodes.ManagedPlanProtected, message = pe.MessageText });
            }
        });

    private static PostgresException? FindFence(Exception? ex)
    {
        for (; ex is not null; ex = ex.InnerException)
            if (ex is PostgresException pe && pe.SqlState == PlanStoreSchema.FenceSqlState) return pe;
        return null;
    }

    // -----------------------------------------------------------------------

    private static IResult? Missing(params (string Name, object? Value)[] fields)
    {
        var missing = fields.Where(f => f.Value is null || f.Value is string s && string.IsNullOrWhiteSpace(s)).Select(f => f.Name).ToList();
        return missing.Count == 0 ? null : Error(400, MissingField, $"Required field(s) missing: {string.Join(", ", missing)}.");
    }

    private static OperationContext Ctx(string? key, long? expected, string? actor) => new(key!, expected!.Value, actor!);

    private static PlanContent Content(string? value, Dictionary<string, string>? attributes) => new(value, attributes);

    private static bool TryGate(string? s, out GatePolicy? gate)
    {
        gate = null;
        if (s is null) return true;
        try { gate = PlanStore.ParseGate(s); return true; } catch (FormatException) { return false; }
    }

    private static IResult InvalidEnum(string field, string? value) =>
        Error(400, PlanErrorCodes.InvalidEnum, $"'{value}' is not a valid value for {field}.");

    private static IResult Error(int status, string code, string message, IEnumerable<Blocker>? blockers = null, IEnumerable<PlanError>? errors = null) =>
        Results.Json(new
        {
            code, message,
            errors = errors?.Select(e => new { e.Code, e.Message, e.NodeId, e.RelatedId }),
            blockers = blockers?.Select(BlockerDto),
        }, statusCode: status);

    public static int StatusFor(string code) => code switch
    {
        PlanErrorCodes.InvalidEnum or PlanErrorCodes.InvalidOperationKey or PlanErrorCodes.ActorRequired
            or PlanErrorCodes.AttemptRequired or PlanStoreErrorCodes.InvalidContent or MissingField => 400,
        PlanErrorCodes.NodeNotFound or PlanStoreErrorCodes.PlanNotFound or PlanStoreErrorCodes.ProjectNotFound => 404,
        PlanErrorCodes.StaleRevision or PlanErrorCodes.StaleContent or PlanErrorCodes.OperationKeyReused or PlanErrorCodes.RevisionExhausted
            or PlanStoreErrorCodes.PlanExists or PlanStoreErrorCodes.NodeExists or PlanStoreErrorCodes.ConcurrentModification
            or PlanStoreErrorCodes.ManagedPlanProtected => 409,
        PlanStoreErrorCodes.ProjectionFailed => 503,
        _ => 422,
    };

    private static async Task<IResult> Respond(PlanStore store, PlanStoreResult r)
    {
        if (!r.Ok)
        {
            // An invalid stored graph reports invalid_graph first, followed by the details.
            var primary = r.Errors.FirstOrDefault()?.Code ?? "unknown";
            return Error(StatusFor(primary), primary, r.Errors.FirstOrDefault()?.Message ?? "", r.Blockers, r.Errors);
        }
        var snap = r.RootId is Guid root ? await store.LoadAsync(root) : null;
        return snap is null
            ? Error(500, "plan_store_error", "The plan could not be read back.")
            : Results.Ok(View(snap, r.Outcome));
    }

    private static object BlockerDto(Blocker b) => new
    {
        ownerId = b.DependencyOwnerId, predecessorId = b.PredecessorId, gate = PlanStore.GateName(b.Gate), reason = b.Reason,
    };

    private static string Snake(Enum e) => e switch
    {
        WorkStatus w => PlanStore.WorkName(w),
        _ => System.Text.RegularExpressions.Regex.Replace(e.ToString(), "(?<!^)([A-Z])", "_$1").ToLowerInvariant(),
    };

    public static object ReadinessView(PlanSnapshot snap)
    {
        var g = snap.Graph;
        var view = PlanRules.Evaluate(g);
        return new
        {
            contractVersion = snap.ContractVersion,
            rootId = g.RootId,
            errors = view.Errors.Select(e => new { e.Code, e.Message, e.NodeId, e.RelatedId }),
            leaves = view.Leaves.Select(l => new
            {
                nodeId = l.NodeId, name = snap.Names.GetValueOrDefault(l.NodeId), work = PlanStore.WorkName(l.Work), ready = l.Ready,
                gatesHold = l.GatesHold, upstreamChanged = l.UpstreamChanged,
                // Opaque attempt correlation only; not an identity or auth principal.
                attemptId = g.StateOf(l.NodeId).AttemptId, attemptEpoch = g.StateOf(l.NodeId).AttemptEpoch,
                blockers = l.Blockers.Select(BlockerDto),
            }),
            containers = view.Containers.Select(c => new
            {
                nodeId = c.NodeId, name = snap.Names.GetValueOrDefault(c.NodeId), completion = Snake(c.Completion),
                acceptance = Snake(c.Acceptance), gatesHold = c.GatesHold,
            }),
        };
    }

    public static object View(PlanSnapshot snap, OpOutcome? outcome)
    {
        var g = snap.Graph;
        return new
        {
            contractVersion = snap.ContractVersion,
            outcome = outcome is OpOutcome o ? Snake(o) : null,
            rootId = g.RootId,
            projectId = g.ProjectId,
            defaultGate = PlanStore.GateName(g.DefaultGate),
            nodes = g.Nodes.Values.OrderBy(n => n.Id == g.RootId ? 0 : 1).ThenBy(n => n.SiblingOrder).ThenBy(n => n.Id).Select(n =>
            {
                var s = g.StateOf(n.Id);
                var a = s.Acceptance;
                var content = snap.Contents.GetValueOrDefault(n.Id) ?? PlanContent.Empty;
                return new
                {
                    id = n.Id, parentId = n.ParentId, nodeType = n.NodeType, name = snap.Names.GetValueOrDefault(n.Id),
                    value = content.Value, contentAttributes = content.Attributes ?? new Dictionary<string, string>(),
                    siblingOrder = n.SiblingOrder, contentRevision = n.ContentRevision, stateRevision = s.StateRevision,
                    work = PlanStore.WorkName(s.Work), attemptId = s.AttemptId, attemptEpoch = s.AttemptEpoch, artifactRef = s.ArtifactRef,
                    acceptance = a is null ? null : new
                    {
                        decision = PlanStore.DecisionName(a.Decision), contentRevision = a.ContentRevision, artifactRef = a.ArtifactRef,
                        attemptId = a.AttemptId, attemptEpoch = a.AttemptEpoch, decidedBy = a.DecidedBy, evidenceRef = a.EvidenceRef,
                    },
                    effectiveAcceptance = Snake(PlanRules.EffectiveAcceptanceOf(g, n.Id)),
                };
            }),
            dependencies = g.Dependencies.OrderBy(d => d.SuccessorId).ThenBy(d => d.PredecessorId).Select(d => new
            {
                predecessorId = d.PredecessorId, successorId = d.SuccessorId, gate = d.Gate is GatePolicy gp ? PlanStore.GateName(gp) : null,
            }),
            readiness = ReadinessView(snap),
        };
    }
}

public record CreatePlanRequest(Guid? RootId, Guid? ProjectId, string? Name, string? Value, Dictionary<string, string>? Attributes,
    string? DefaultGate, string? OperationKey, long? ExpectedStateRevision, string? Actor);
public record AddChildRequest(Guid? ChildId, string? NodeType, string? Name, int? SiblingOrder, string? Value, Dictionary<string, string>? Attributes,
    string? OperationKey, long? ExpectedStateRevision, string? Actor);
public record AddDependencyRequest(Guid? PredecessorId, string? Gate, string? OperationKey, long? ExpectedStateRevision, string? Actor);
public record TransitionRequest(string? To, string? AttemptId, long? AttemptEpoch, string? ArtifactRef, string? OperationKey, long? ExpectedStateRevision, string? Actor);
public record DecideRequest(string? Decision, long? ReviewedContentRevision, string? ReviewedArtifactRef, long? ReviewedAttemptEpoch, string? EvidenceRef,
    string? OperationKey, long? ExpectedStateRevision, string? Actor);
public record ContentRequest(string? Value, Dictionary<string, string>? Attributes, long? ExpectedContentRevision, string? OperationKey,
    long? ExpectedStateRevision, string? Actor);
