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

        // Plan 021: read-only discovery of managed plans (metadata only; graphs are not loaded).
        api.MapGet("/plans", async (string? projectId, string? afterRootId, string? limit) =>
        {
            Guid? project = null, after = null;
            if (projectId is not null)
            {
                if (!Guid.TryParse(projectId, out var p)) return Error(400, InvalidQuery, "projectId must be a uuid.");
                project = p;
            }
            if (afterRootId is not null)
            {
                if (!Guid.TryParse(afterRootId, out var a)) return Error(400, InvalidQuery, "afterRootId must be a uuid.");
                after = a;
            }
            var lim = 100;
            if (limit is not null && (!int.TryParse(limit, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out lim) || lim < 1 || lim > 500))
                return Error(400, InvalidQuery, "limit must be an integer between 1 and 500.");
            var page = await store.ListPlansAsync(project, after, lim);
            return Results.Ok(new
            {
                contractVersion = PlanContract.Version,
                plans = page.Plans.Select(p => new
                {
                    rootId = p.RootId, projectId = p.ProjectId, name = p.Name, defaultGate = p.DefaultGate,
                    contractVersion = p.ContractVersion, supported = p.Supported,
                    createdAt = p.CreatedAt, createdBy = p.CreatedBy, eventSeq = p.EventSeq,
                }),
                nextAfterRootId = page.NextAfterRootId,
            });
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
                req.AttemptId, req.AttemptEpoch, req.ArtifactRef, req.ExecutorRef));
        });

        // Read-only provenance history (plan 016). There is no public history-write endpoint.
        api.MapGet("/plans/{rootId:guid}/events", async (Guid rootId, string? afterSeq, string? limit) =>
            await Events(store, rootId, null, afterSeq, limit));
        api.MapGet("/nodes/{nodeId:guid}/events", async (Guid nodeId, string? afterSeq, string? limit) =>
            await Events(store, null, nodeId, afterSeq, limit));

        // Plan 047: one attempt's retained worker trace, read-only. Files are confined under
        // HEKATE_TRACE_ROOT, read on each request; the root is read per request, never cached.
        api.MapGet("/nodes/{nodeId:guid}/attempts/{attemptId}/trace", async (Guid nodeId, string attemptId, string? afterSeq, string? limit) =>
            await Trace(store, Environment.GetEnvironmentVariable(AttemptTrace.RootVariable), nodeId, attemptId, afterSeq, limit));

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

        // Plan 019: durable claim receipts. A receipt is historical; stillCurrent is factual
        // correlation only and grants no authority.
        api.MapPost("/plans/{rootId:guid}/claims", async (Guid rootId, ClaimRequest req) =>
        {
            if (Missing(("claimKey", req.ClaimKey), ("attemptId", req.AttemptId), ("actor", req.Actor)) is { } missing) return missing;
            return ClaimResponse(await store.ClaimAsync(rootId, req.ClaimKey!, req.AttemptId!, req.ExecutorRef, req.Actor!));
        });

        api.MapGet("/plans/{rootId:guid}/claims/{claimKey}", async (Guid rootId, string claimKey) =>
            ClaimResponse(await store.ReadClaimAsync(rootId, claimKey)));

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

    public const string InvalidQuery = "invalid_query";

    private static async Task<IResult> Events(PlanStore store, Guid? rootId, Guid? nodeId, string? afterSeqText, string? limitText)
    {
        long afterSeq = 0;
        if (afterSeqText is not null && (!long.TryParse(afterSeqText, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out afterSeq)))
            return Error(400, InvalidQuery, "afterSeq must be a non-negative integer.");
        var limit = 100;
        if (limitText is not null && (!int.TryParse(limitText, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out limit) || limit < 1 || limit > 500))
            return Error(400, InvalidQuery, "limit must be an integer between 1 and 500.");

        var (page, notFound) = await store.ReadEventsAsync(rootId, nodeId, afterSeq, limit);
        if (page is null) return Error(404, notFound!, nodeId is Guid n ? $"Node {n} is not part of a managed plan." : $"Plan {rootId} not found.");
        return Results.Ok(new
        {
            contractVersion = PlanContract.Version,
            events = page.Events.Select(s => new
            {
                seq = s.Seq, nodeId = s.Event.NodeId, nodeStateRevision = s.Event.NodeStateRevision, kind = PlanStore.EventKindName(s.Event.Kind),
                workFrom = PlanStore.WorkName(s.Event.WorkFrom), workTo = PlanStore.WorkName(s.Event.WorkTo), contentRevision = s.Event.ContentRevision,
                // Opaque correlation only; not identity or claim authority.
                attemptId = s.Event.AttemptId, attemptEpoch = s.Event.AttemptEpoch, executorRef = s.Event.ExecutorRef,
                artifactRef = s.Event.ArtifactRef,
                decision = s.Event.Decision is AcceptanceDecision d ? PlanStore.DecisionName(d) : null,
                reviewedContentRevision = s.Event.ReviewedContentRevision, evidenceRef = s.Event.EvidenceRef,
                contentDigest = s.Event.ContentDigest, actor = s.Event.Actor, operationKey = s.Event.OperationKey, recordedAt = s.RecordedAt,
                // Plan 019: the attempt's pins (NULL before 3b1, never backfilled) and the claim key for claim starts.
                attemptContentRevision = s.Event.AttemptContentRevision, attemptPrereqDigest = s.Event.AttemptPrereqDigest,
                claimKey = s.Event.ClaimKey,
            }),
            nextAfterSeq = page.NextAfterSeq,
            // First RECORDED event only; earlier work history (before plan 016) is unknown, never backfilled.
            historyStartsAtSeq = page.HistoryStartsAtSeq,
            historyBackfilled = false,
        });
    }

    /// <summary>
    /// Compose one trace page (trace contract v0). Status says what is known about the trace, never
    /// whether a process is alive: running = no exited record and the node's current attempt is
    /// this one in progress; unfinished = no exited record otherwise. Integrity is verified only
    /// when exited finalized a complete, write-error-free trace whose retained bytes match; a
    /// complete trace that does not match is refused. Every key is present, null when not
    /// applicable, and the serialized response stays within the UTF-8 budget.
    /// </summary>
    public static async Task<IResult> Trace(PlanStore store, string? traceRoot, Guid nodeId, string attemptId, string? afterSeqText, string? limitText)
    {
        long? afterSeq = null;
        if (afterSeqText is not null)
        {
            if (!long.TryParse(afterSeqText, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out var a))
                return Error(400, InvalidQuery, "afterSeq must be a non-negative integer.");
            afterSeq = a;
        }
        var limit = 200;
        if (limitText is not null && (!int.TryParse(limitText, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out limit) || limit < 1 || limit > 500))
            return Error(400, InvalidQuery, "limit must be an integer between 1 and 500.");

        var found = await store.ReadAttemptTraceSourceAsync(nodeId, attemptId);
        if (found.Source is not { } s) return Error(StatusFor(found.ErrorCode!), found.ErrorCode!, found.ErrorMessage ?? "");

        IResult Page(string status, string? reason, string integrity, string? kind, TraceExit? exit, (string Text, long Bytes)? prompt,
            IReadOnlyList<TraceRecord> records, long? next, bool capped) =>
            TraceJson(new
            {
                contractVersion = PlanContract.Version, nodeId = s.NodeId, attemptId = s.AttemptId, attemptEpoch = s.AttemptEpoch, claimKey = s.ClaimKey,
                status, reason, integrity, executionKind = kind,
                exit = exit is null ? null : new { code = exit.Code, killReason = exit.KillReason },
                prompt = prompt is { } p ? new { text = p.Text, bytes = p.Bytes } : null,
                records, nextAfterSeq = next, capped,
            });
        var empty = Array.Empty<TraceRecord>();

        if (s.NotCapturedReason is { } why) return Page("not_captured", why, "none", null, null, null, empty, null, false);

        var (traceRef, refError) = AttemptTrace.ParseRef(s.LaunchDataJson!);
        if (refError is not null) return Error(StatusFor(AttemptTraceCodes.Invalid), AttemptTraceCodes.Invalid, refError);
        if (traceRef is null) return Page("not_captured", AttemptTraceSources.NoTraceBlock, "none", null, null, null, empty, null, false);

        TraceFinal? final = null;
        TraceExit? exit = null;
        if (s.ExitedDataJson is { } exitedJson)
        {
            var (f, x, exitError) = AttemptTrace.ParseExited(exitedJson);
            if (exitError is not null) return Error(StatusFor(AttemptTraceCodes.Invalid), AttemptTraceCodes.Invalid, exitError);
            (final, exit) = (f, x);
        }
        // An exited record from before trace capture carries no trace block: the attempt's own files are then unverifiable.
        var status = final is not null ? "exited" : s.CurrentInProgress ? "running" : "unfinished";

        if (string.IsNullOrWhiteSpace(traceRoot))
            return Error(StatusFor(AttemptTraceCodes.RootNotConfigured), AttemptTraceCodes.RootNotConfigured, $"{AttemptTrace.RootVariable} is not configured.");
        var promptPath = AttemptTrace.Confine(traceRoot, traceRef.RunDir, traceRef.PromptName);
        var tracePath = AttemptTrace.Confine(traceRoot, traceRef.RunDir, traceRef.TraceName);
        foreach (var refused in new[] { promptPath, tracePath }.Where(p => p.Kind == TracePathKind.Refused))
            return Error(StatusFor(AttemptTraceCodes.PathRefused), AttemptTraceCodes.PathRefused, refused.Reason!);
        if (promptPath.Kind == TracePathKind.Missing || tracePath.Kind == TracePathKind.Missing)
            return Page("missing", null, "none", traceRef.ExecutionKind, null, null, empty, null, false);

        var promptBytes = AttemptTrace.ReadBounded(promptPath.FullPath!);
        var traceBytes = AttemptTrace.ReadBounded(tracePath.FullPath!);
        if (promptBytes is null || traceBytes is null)
            return Error(StatusFor(AttemptTraceCodes.FileTooLarge), AttemptTraceCodes.FileTooLarge, "A retained trace file is larger than this endpoint reads.");

        var integrity = "unverified";
        if (final is { Complete: true, WriteError: false })
        {
            if (!AttemptTrace.Matches(final, promptBytes, traceBytes))
                return Error(StatusFor(AttemptTraceCodes.IntegrityMismatch), AttemptTraceCodes.IntegrityMismatch, "The retained trace does not match its recorded size and hash.");
            integrity = "verified";
        }
        var exitView = status == "exited" ? exit : null;
        var capped = final?.Capped ?? false;
        (string, long)? promptView = afterSeq is null ? (new System.Text.UTF8Encoding(false, false).GetString(promptBytes), promptBytes.LongLength) : null;

        // The budget covers the whole serialized response: measure the header with no records first.
        var header = System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(new
        {
            contractVersion = PlanContract.Version, nodeId = s.NodeId, attemptId = s.AttemptId, attemptEpoch = s.AttemptEpoch, claimKey = s.ClaimKey,
            status, reason = (string?)null, integrity, executionKind = traceRef.ExecutionKind,
            exit = exitView is null ? null : new { code = exitView.Code, killReason = exitView.KillReason },
            prompt = promptView is { } pv ? new { text = pv.Item1, bytes = pv.Item2 } : null,
            records = empty, nextAfterSeq = long.MaxValue, capped,
        }, AttemptTrace.Json).Length;
        if (header > AttemptTrace.ResponseBudgetBytes)
            return Error(StatusFor(AttemptTraceCodes.RecordTooLarge), AttemptTraceCodes.RecordTooLarge, "The prompt alone is larger than the response budget.");
        var page = AttemptTrace.ReadPage(traceBytes, afterSeq, limit, AttemptTrace.ResponseBudgetBytes - header);
        if (page.ErrorCode is { } pageError) return Error(StatusFor(pageError), pageError, page.ErrorMessage ?? "");
        return Page(status, null, integrity, traceRef.ExecutionKind, exitView, promptView, page.Records, page.NextAfterSeq, capped);
    }

    /// <summary>Serialize with the shared settings and return the exact bytes measured against the budget.</summary>
    private static IResult TraceJson(object body) =>
        Results.Bytes(System.Text.Json.JsonSerializer.SerializeToUtf8Bytes(body, AttemptTrace.Json), "application/json; charset=utf-8");

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
            or PlanErrorCodes.AttemptRequired or PlanStoreErrorCodes.InvalidContent or MissingField
            or PlanErrorCodes.InvalidExecutorRef or InvalidQuery or PlanStoreErrorCodes.InvalidInput => 400,
        PlanErrorCodes.NodeNotFound or PlanStoreErrorCodes.PlanNotFound or PlanStoreErrorCodes.ProjectNotFound
            or PlanStoreErrorCodes.ClaimNotFound => 404,
        PlanErrorCodes.StaleRevision or PlanErrorCodes.StaleContent or PlanErrorCodes.StalePrerequisites
            or PlanErrorCodes.OperationKeyReused or PlanErrorCodes.RevisionExhausted
            or PlanStoreErrorCodes.PlanExists or PlanStoreErrorCodes.NodeExists or PlanStoreErrorCodes.ConcurrentModification
            or PlanStoreErrorCodes.ManagedPlanProtected => 409,
        PlanStoreErrorCodes.ProjectionFailed => 503,
        // Plan 047 attempt traces.
        AttemptTraceCodes.AttemptNotFound => 404,
        AttemptTraceCodes.IdentityConflict or AttemptTraceCodes.PathRefused or AttemptTraceCodes.IntegrityMismatch
            or AttemptTraceCodes.RecordTooLarge or AttemptTraceCodes.FileTooLarge or AttemptTraceCodes.Invalid => 409,
        AttemptTraceCodes.RootNotConfigured or AttemptTraceCodes.JournalUnavailable => 503,
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

    private static IResult ClaimResponse(ClaimResult r)
    {
        if (!r.Ok)
        {
            var primary = r.Errors[0];
            return Error(StatusFor(primary.Code), primary.Code, primary.Message, null, r.Errors);
        }
        var rc = r.Receipt!;
        static System.Text.Json.JsonElement? Json(string? s)
        {
            if (s is null) return null;
            using var doc = System.Text.Json.JsonDocument.Parse(s);
            return doc.RootElement.Clone();
        }
        return Results.Ok(new
        {
            contractVersion = PlanContract.Version,
            replayed = r.Replayed,
            stillCurrent = r.StillCurrent,
            current = r.Current is { } c ? new { work = PlanStore.WorkName(c.Work), attemptId = c.AttemptId, attemptEpoch = c.AttemptEpoch } : null,
            receipt = new
            {
                rootId = rc.RootId, claimKey = rc.ClaimKey, outcome = rc.Outcome, nodeId = rc.NodeId,
                attemptId = rc.AttemptId, attemptEpoch = rc.AttemptEpoch, executorRef = rc.ExecutorRef,
                contentRevision = rc.ContentRevision, contentDigest = rc.ContentDigest, contentSnapshot = Json(rc.ContentSnapshotJson),
                prereqDigest = rc.PrereqDigest, prereqSnapshot = Json(rc.PrereqSnapshotJson),
                eventSeq = rc.EventSeq, actor = rc.Actor, createdAt = rc.CreatedAt,
            },
        });
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
                    executorRef = s.ExecutorRef,
                    attemptContentRevision = s.AttemptContentRevision, attemptPrereqDigest = s.AttemptPrereqDigest,
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
public record TransitionRequest(string? To, string? AttemptId, long? AttemptEpoch, string? ArtifactRef, string? OperationKey, long? ExpectedStateRevision,
    string? Actor, string? ExecutorRef = null);
public record DecideRequest(string? Decision, long? ReviewedContentRevision, string? ReviewedArtifactRef, long? ReviewedAttemptEpoch, string? EvidenceRef,
    string? OperationKey, long? ExpectedStateRevision, string? Actor);
public record ClaimRequest(string? ClaimKey, string? AttemptId, string? ExecutorRef, string? Actor);
public record ContentRequest(string? Value, Dictionary<string, string>? Attributes, long? ExpectedContentRevision, string? OperationKey,
    long? ExpectedStateRevision, string? Actor);
