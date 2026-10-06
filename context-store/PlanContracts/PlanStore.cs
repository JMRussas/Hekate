// CodeStoragePoc - Plan node contracts v1: PostgreSQL store
//
// Applies PlanRules to managed plans persisted in PostgreSQL. Every mutation is ONE
// transaction: SET LOCAL hekate.plan_contract (opens the write fence for this tx only),
// per-project advisory lock (serialises all managed writes of a project), whole-plan
// snapshot load, the pure PlanRules operation (whole-graph validation inside it), CAS
// writes of changed rows, and a convergent AGE reconcile of the plan's tagged DEPENDS_ON
// edges in the same transaction. Any failure, including AGE, rolls everything back.
//
// AGE is a projection, never authority. Availability coupling: while AGE is broken,
// managed-plan writes fail with projection_failed instead of drifting. The projection
// carries identity only (CodeNode.node_id, node_type, DEPENDS_ON.plan_root); names and
// content are not projected, so no user text is ever interpolated into Cypher.
//
// Depends on: PlanRules, PlanModel, PlanContentDigest, PlanStoreSchema, Npgsql
// Used by:    Api/PlanContractEndpoints, PlanStore.LiveTests

using System.Collections.Immutable;
using CodeStoragePoc.ContextRouter;
using Npgsql;

namespace CodeStoragePoc.PlanContracts;

/// <summary>Store-level error codes (in addition to PlanErrorCodes).</summary>
public static class PlanStoreErrorCodes
{
    public const string PlanNotFound = "plan_not_found";
    public const string ProjectNotFound = "project_not_found";
    public const string PlanExists = "plan_exists";
    public const string NodeExists = "node_exists";
    public const string InvalidContent = "invalid_content";
    public const string ConcurrentModification = "concurrent_modification";
    public const string ProjectionFailed = "projection_failed";
    public const string ManagedPlanProtected = "managed_plan_protected";
    public const string UnsupportedContractVersion = "unsupported_contract_version";
}

/// <summary>Contract content of a node: value plus content attributes (no null attribute values).</summary>
public sealed record PlanContent(string? Value, IReadOnlyDictionary<string, string>? Attributes)
{
    public static PlanContent Empty { get; } = new(null, null);

    public string Digest(params string?[] fields) =>
        PlanContentDigest.Compute(Value, Attributes?.ToDictionary(kv => kv.Key, kv => (string?)kv.Value), fields);
}

public sealed record PlanStoreResult(
    OpOutcome Outcome, Guid? RootId, PlanGraph? Graph, ImmutableArray<PlanError> Errors, ImmutableArray<Blocker> Blockers)
{
    public bool Ok => Outcome != OpOutcome.Rejected;

    internal static PlanStoreResult Fail(string code, string message, Guid? nodeId = null) =>
        new(OpOutcome.Rejected, null, null, [new PlanError(code, message, nodeId)], []);
}

/// <summary>
/// A loaded plan, read atomically: the rules snapshot plus each node's name and its saved
/// contract content (value + content attributes) for map/workflow consumers.
/// </summary>
public sealed record PlanSnapshot(
    PlanGraph Graph,
    ImmutableDictionary<Guid, string?> Names,
    ImmutableDictionary<Guid, PlanContent> Contents,
    string ContractVersion);

public sealed class PlanStore(string connectionString, string graphName = "code_graph")
{
    private static readonly string[] StructuralTypes = [.. PlanNodeTypes.Structural];

    // -----------------------------------------------------------------------
    // Reads
    // -----------------------------------------------------------------------

    public async Task<PlanSnapshot?> LoadAsync(Guid rootId)
    {
        await using var conn = new NpgsqlConnection(connectionString);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync(System.Data.IsolationLevel.RepeatableRead);
        var snap = await LoadSnapshot(conn, tx, rootId);
        await tx.RollbackAsync();
        return snap;
    }

    // -----------------------------------------------------------------------
    // Mutations
    // -----------------------------------------------------------------------

    /// <summary>
    /// Create a NEW managed plan root. Idempotent on (rootId, operation key, payload): an exact
    /// retry returns Unchanged; a different key or payload for an existing root is plan_exists.
    /// </summary>
    public async Task<PlanStoreResult> CreatePlanAsync(
        Guid rootId, Guid projectId, string name, PlanContent content, GatePolicy defaultGate, OperationContext ctx)
    {
        if (rootId == Guid.Empty) return PlanStoreResult.Fail(PlanErrorCodes.InvalidChild, "rootId must be a new, non-empty id.");
        if (string.IsNullOrWhiteSpace(ctx.OperationKey)) return PlanStoreResult.Fail(PlanErrorCodes.InvalidOperationKey, "An operation key is required.");
        if (string.IsNullOrWhiteSpace(ctx.Actor)) return PlanStoreResult.Fail(PlanErrorCodes.ActorRequired, "An actor is required.");
        if (!Enum.IsDefined(defaultGate)) return PlanStoreResult.Fail(PlanErrorCodes.InvalidEnum, $"Unknown gate policy {(int)defaultGate}.");
        if (ValidateContent(content) is { } contentError) return contentError;

        var fingerprint = content.Digest("create_plan", ctx.Actor, rootId.ToString(), projectId.ToString(), name, defaultGate.ToString());

        await using var conn = new NpgsqlConnection(connectionString);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync();
        await OpenFence(conn, tx);

        if (!await Exists(conn, tx, "SELECT 1 FROM public.projects WHERE id = @id", projectId))
            return await Abort(tx, PlanStoreResult.Fail(PlanStoreErrorCodes.ProjectNotFound, $"Project {projectId} does not exist."));
        await LockProject(conn, tx, projectId);

        if (await Exists(conn, tx, "SELECT 1 FROM public.managed_plans WHERE root_node_id = @id", rootId))
        {
            await using var cmd = new NpgsqlCommand(
                "SELECT last_op_key, last_op_fingerprint, state_revision FROM public.plan_node_state WHERE node_id = @id", conn, tx);
            cmd.Parameters.AddWithValue("id", rootId);
            await using var r = await cmd.ExecuteReaderAsync();
            await r.ReadAsync();
            var sameOp = !r.IsDBNull(0) && r.GetString(0) == ctx.OperationKey && !r.IsDBNull(1) && r.GetString(1) == fingerprint && r.GetInt64(2) == 1;
            await r.CloseAsync();
            if (!sameOp)
                return await Abort(tx, PlanStoreResult.Fail(PlanStoreErrorCodes.PlanExists, $"Plan {rootId} already exists (different operation or payload).", rootId));
            var existing = await LoadSnapshot(conn, tx, rootId);
            await tx.RollbackAsync();
            return new PlanStoreResult(OpOutcome.Unchanged, rootId, existing!.Graph, [], []);
        }
        if (await Exists(conn, tx, "SELECT 1 FROM public.nodes WHERE id = @id", rootId))
            return await Abort(tx, PlanStoreResult.Fail(PlanStoreErrorCodes.NodeExists, $"Node {rootId} already exists and is not a managed plan.", rootId));
        if (ctx.ExpectedStateRevision != 0)
            return await Abort(tx, PlanStoreResult.Fail(PlanErrorCodes.StaleRevision, "A new plan is created with expected state revision 0.", rootId));

        try
        {
            await InsertNode(conn, tx, rootId, projectId, NodeTypes.Plan, name, null, 0, content, ctx.Actor);
            await Execute(conn, tx,
                "INSERT INTO public.managed_plans (root_node_id, project_id, default_gate, contract_version, created_by) VALUES (@r, @p, @g, @v, @a)",
                ("r", rootId), ("p", projectId), ("g", GateName(defaultGate)), ("v", PlanContract.Version), ("a", ctx.Actor));
            var rootState = NodeState.Initial with { StateRevision = 1, LastOperationKey = ctx.OperationKey, LastOperationFingerprint = fingerprint };
            await InsertState(conn, tx, rootId, rootId, 1, rootState, ctx.Actor);
        }
        catch (PostgresException ex) when (ex.SqlState == PostgresErrorCodes.UniqueViolation)
        {
            // Same root id created concurrently under a different project lock.
            return await Abort(tx, PlanStoreResult.Fail(PlanStoreErrorCodes.NodeExists, $"Node {rootId} was created concurrently.", rootId));
        }

        var created = await LoadSnapshot(conn, tx, rootId);
        var errors = PlanRules.ValidateGraph(created!.Graph);
        if (errors.Length > 0) return await Abort(tx, new PlanStoreResult(OpOutcome.Rejected, null, null, errors, []));
        if (await TryProject(conn, tx, rootId, created.Graph) is { } projectionError) return await Abort(tx, projectionError);
        await tx.CommitAsync();
        return new PlanStoreResult(OpOutcome.Applied, rootId, created.Graph, [], []);
    }

    public Task<PlanStoreResult> AddChildAsync(
        Guid parentId, Guid childId, string nodeType, string name, int siblingOrder, PlanContent content, OperationContext ctx)
    {
        if (ValidateContent(content) is { } contentError) return Task.FromResult(contentError);
        var payload = content.Digest(name);
        return MutateAsync(parentId,
            g => PlanRules.AddChild(g, new PlanNode(childId, g.ProjectId, nodeType, parentId, siblingOrder, 1), ctx, payload),
            ctx.Actor,
            (conn, tx, before, after) => InsertNode(conn, tx, childId, after.ProjectId, nodeType, name, parentId, siblingOrder, content, ctx.Actor),
            // The rules only see this plan; ids are global. An id used anywhere else is a conflict.
            async (conn, tx, before) =>
                !before.Nodes.ContainsKey(childId) && await Exists(conn, tx, "SELECT 1 FROM public.nodes WHERE id = @id", childId)
                    ? PlanStoreResult.Fail(PlanStoreErrorCodes.NodeExists, $"Node id {childId} is already in use.", childId)
                    : null);
    }

    public Task<PlanStoreResult> AddDependencyAsync(Guid successorId, Guid predecessorId, GatePolicy? gate, OperationContext ctx) =>
        MutateAsync(successorId, g => PlanRules.AddDependency(g, predecessorId, successorId, gate, ctx), ctx.Actor);

    public Task<PlanStoreResult> RemoveDependencyAsync(Guid successorId, Guid predecessorId, OperationContext ctx) =>
        MutateAsync(successorId, g => PlanRules.RemoveDependency(g, predecessorId, successorId, ctx), ctx.Actor);

    public Task<PlanStoreResult> TransitionAsync(Guid nodeId, WorkStatus to, OperationContext ctx, string? attemptId, long? attemptEpoch,
        string? artifactRef, string? executorRef = null) =>
        MutateAsync(nodeId, g => PlanRules.Transition(g, nodeId, to, ctx, attemptId, attemptEpoch, artifactRef, executorRef), ctx.Actor,
            audit: (r, before) => PlanAuditEvents.Derive(r, before, nodeId, AuditedOperation.Transition, ctx));

    public Task<PlanStoreResult> DecideAsync(
        Guid nodeId, AcceptanceDecision decision, long reviewedContentRevision, string? reviewedArtifactRef,
        long reviewedAttemptEpoch, string? evidenceRef, OperationContext ctx) =>
        MutateAsync(nodeId, g => PlanRules.Decide(g, nodeId, decision, reviewedContentRevision, reviewedArtifactRef, reviewedAttemptEpoch, evidenceRef, ctx), ctx.Actor,
            audit: (r, before) => PlanAuditEvents.Derive(r, before, nodeId, AuditedOperation.Decide, ctx));

    /// <summary>Replace a leaf's contract content (value + content attributes) and bump its content revision.</summary>
    public Task<PlanStoreResult> ReviseContentAsync(Guid nodeId, PlanContent content, long expectedContentRevision, OperationContext ctx)
    {
        if (ValidateContent(content) is { } contentError) return Task.FromResult(contentError);
        var digest = content.Digest();
        return MutateAsync(nodeId,
            g => PlanRules.ReviseContent(g, nodeId, ctx, digest, expectedContentRevision),
            ctx.Actor,
            (conn, tx, before, after) => WriteContent(conn, tx, nodeId, content, ctx.Actor),
            audit: (r, before) => PlanAuditEvents.Derive(r, before, nodeId, AuditedOperation.ReviseContent, ctx, digest));
    }

    /// <summary>Re-run the AGE projection for a plan (idempotent repair).</summary>
    public async Task<PlanStoreResult> ReconcileProjectionAsync(Guid rootId)
    {
        await using var conn = new NpgsqlConnection(connectionString);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync();
        await OpenFence(conn, tx);
        var project = await ProjectOfRoot(conn, tx, rootId);
        if (project is null) return await Abort(tx, PlanStoreResult.Fail(PlanStoreErrorCodes.PlanNotFound, $"Plan {rootId} not found.", rootId));
        await LockProject(conn, tx, project.Value);
        var snap = await LoadSnapshot(conn, tx, rootId);
        if (snap!.ContractVersion != PlanContract.Version)
            return await Abort(tx, PlanStoreResult.Fail(PlanStoreErrorCodes.UnsupportedContractVersion,
                $"Plan uses '{snap.ContractVersion}'; this store implements '{PlanContract.Version}'.", rootId));
        // Never project a corrupt stored graph.
        var invalid = PlanRules.ValidateGraph(snap.Graph);
        if (invalid.Length > 0)
            return await Abort(tx, new PlanStoreResult(OpOutcome.Rejected, rootId, null,
                invalid.Insert(0, new PlanError(PlanErrorCodes.InvalidGraph, "The stored plan is invalid; nothing was projected.")), []));
        if (await TryProject(conn, tx, rootId, snap.Graph) is { } projectionError) return await Abort(tx, projectionError);
        await tx.CommitAsync();
        return new PlanStoreResult(OpOutcome.Applied, rootId, snap.Graph, [], []);
    }

    // -----------------------------------------------------------------------
    // Core transaction
    // -----------------------------------------------------------------------

    private async Task<PlanStoreResult> MutateAsync(
        Guid nodeId, Func<PlanGraph, OpResult> op, string actor,
        Func<NpgsqlConnection, NpgsqlTransaction, PlanGraph, PlanGraph, Task>? writeNodeRows = null,
        Func<NpgsqlConnection, NpgsqlTransaction, PlanGraph, Task<PlanStoreResult?>>? preCheck = null,
        Func<OpResult, PlanGraph, IReadOnlyList<AuditEvent>>? audit = null)
    {
        await using var conn = new NpgsqlConnection(connectionString);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync();
        await OpenFence(conn, tx);

        // Root and project of a managed node never change, so this read is safe before the lock.
        Guid? root = null, project = null;
        await using (var cmd = new NpgsqlCommand(
            "SELECT s.root_node_id, m.project_id FROM public.plan_node_state s JOIN public.managed_plans m ON m.root_node_id = s.root_node_id WHERE s.node_id = @id", conn, tx))
        {
            cmd.Parameters.AddWithValue("id", nodeId);
            await using var r = await cmd.ExecuteReaderAsync();
            if (await r.ReadAsync()) { root = r.GetGuid(0); project = r.GetGuid(1); }
        }
        if (root is null)
            return await Abort(tx, PlanStoreResult.Fail(PlanErrorCodes.NodeNotFound, $"Node {nodeId} is not part of a managed plan.", nodeId));
        await LockProject(conn, tx, project!.Value);

        var snapshot = (await LoadSnapshot(conn, tx, root.Value))!;
        if (snapshot.ContractVersion != PlanContract.Version)
            return await Abort(tx, PlanStoreResult.Fail(PlanStoreErrorCodes.UnsupportedContractVersion,
                $"Plan uses '{snapshot.ContractVersion}'; this store implements '{PlanContract.Version}'.", root));
        var before = snapshot.Graph;
        var result = op(before);
        if (result.Outcome != OpOutcome.Applied)
        {
            await tx.RollbackAsync();
            return new PlanStoreResult(result.Outcome, root, result.Outcome == OpOutcome.Unchanged ? before : null, result.Errors, result.Blockers);
        }
        if (preCheck is not null && await preCheck(conn, tx, before) is { } conflict) return await Abort(tx, conflict);

        try
        {
            if (writeNodeRows is not null) await writeNodeRows(conn, tx, before, result.Graph);
            await PersistStates(conn, tx, root.Value, before, result.Graph, actor);
            await PersistDependencies(conn, tx, root.Value, before, result.Graph);
        }
        catch (ConcurrencyConflictException ex)
        {
            return await Abort(tx, PlanStoreResult.Fail(PlanStoreErrorCodes.ConcurrentModification, ex.Message, nodeId));
        }
        catch (PostgresException ex) when (ex.SqlState == PostgresErrorCodes.UniqueViolation)
        {
            // A concurrent writer in another project used the same id between check and insert.
            return await Abort(tx, PlanStoreResult.Fail(PlanStoreErrorCodes.NodeExists, "A node id in this operation is already in use.", nodeId));
        }

        // Plan 016: append-only provenance events for this applied operation, same transaction.
        var events = audit?.Invoke(result, before) ?? [];
        if (events.Count > 0 && await AppendEvents(conn, tx, root.Value, events) is { } overflow) return await Abort(tx, overflow);
        if (await TryProject(conn, tx, root.Value, result.Graph) is { } projectionError) return await Abort(tx, projectionError);
        await tx.CommitAsync();
        return new PlanStoreResult(OpOutcome.Applied, root, result.Graph, [], []);
    }

    private sealed class ConcurrencyConflictException(string message) : Exception(message);

    /// <summary>
    /// Advance the plan's event sequence and insert the events (insert-only; there is no update or
    /// delete path). Under the project lock, so sequences are contiguous for store-written events.
    /// Runs after the state/dependency writes of the same transaction; if the sequence would
    /// overflow it returns revision_exhausted and the caller rolls the whole transaction back, so
    /// no change of the operation is committed.
    /// </summary>
    private static async Task<PlanStoreResult?> AppendEvents(NpgsqlConnection conn, NpgsqlTransaction tx, Guid rootId, IReadOnlyList<AuditEvent> events)
    {
        long current;
        await using (var cmd = new NpgsqlCommand("SELECT event_seq FROM public.managed_plans WHERE root_node_id = @r", conn, tx))
        {
            cmd.Parameters.AddWithValue("r", rootId);
            current = (long)(await cmd.ExecuteScalarAsync())!;
        }
        if (current > long.MaxValue - events.Count)
            return PlanStoreResult.Fail(PlanErrorCodes.RevisionExhausted, "The plan's audit event sequence is exhausted.", rootId);

        await Execute(conn, tx, "UPDATE public.managed_plans SET event_seq = @s WHERE root_node_id = @r", ("s", current + events.Count), ("r", rootId));
        var seq = current;
        foreach (var e in events)
        {
            seq++;
            object N(object? v) => v ?? DBNull.Value;
            await Execute(conn, tx, """
                INSERT INTO public.plan_attempt_events (root_node_id, seq, node_id, node_state_revision, kind, work_from, work_to,
                    content_revision, attempt_id, attempt_epoch, executor_ref, artifact_ref, decision, reviewed_content_revision,
                    evidence_ref, content_digest, actor, operation_key)
                VALUES (@r, @seq, @n, @nsr, @k, @wf, @wt, @cr, @aid, @ae, @eref, @art, @d, @rcr, @ev, @dig, @a, @ok)
                """,
                ("r", rootId), ("seq", seq), ("n", e.NodeId), ("nsr", e.NodeStateRevision), ("k", EventKindName(e.Kind)),
                ("wf", WorkName(e.WorkFrom)), ("wt", WorkName(e.WorkTo)), ("cr", e.ContentRevision), ("aid", N(e.AttemptId)),
                ("ae", e.AttemptEpoch), ("eref", N(e.ExecutorRef)), ("art", N(e.ArtifactRef)),
                ("d", e.Decision is AcceptanceDecision d ? DecisionName(d) : DBNull.Value), ("rcr", N(e.ReviewedContentRevision)),
                ("ev", N(e.EvidenceRef)), ("dig", N(e.ContentDigest)), ("a", e.Actor), ("ok", e.OperationKey));
        }
        return null;
    }

    public static string EventKindName(AuditEventKind k) => k switch
    {
        AuditEventKind.AttemptStarted => "attempt_started", AuditEventKind.AttemptReopened => "attempt_reopened",
        AuditEventKind.AttemptFinished => "attempt_finished", AuditEventKind.AttemptReleased => "attempt_released",
        AuditEventKind.AttemptCancelled => "attempt_cancelled", AuditEventKind.WorkRestored => "work_restored",
        AuditEventKind.DecisionRecorded => "decision_recorded", AuditEventKind.ContentRevised => "content_revised",
        _ => throw new ArgumentOutOfRangeException(nameof(k)),
    };

    // -----------------------------------------------------------------------
    // Event reads (plan 016)
    // -----------------------------------------------------------------------

    /// <summary>A stored audit event as read back (seq + recorded_at are store-assigned).</summary>
    public sealed record StoredEvent(long Seq, AuditEvent Event, DateTime RecordedAt);

    /// <summary>Page of events; HistoryStartsAtSeq is only the first RECORDED event, never proof of earliest work.</summary>
    public sealed record EventPage(IReadOnlyList<StoredEvent> Events, long? NextAfterSeq, long? HistoryStartsAtSeq);

    /// <summary>Events of a plan (or one of its nodes) after a seq cursor. Null when the plan/node is not managed.</summary>
    public async Task<(EventPage? Page, string? NotFoundCode)> ReadEventsAsync(Guid? rootId, Guid? nodeId, long afterSeq, int limit)
    {
        // Exactly one selector, so a future caller can never issue an unscoped read.
        if ((rootId is null) == (nodeId is null)) throw new ArgumentException("Specify exactly one of rootId or nodeId.");
        ArgumentOutOfRangeException.ThrowIfNegative(afterSeq);
        ArgumentOutOfRangeException.ThrowIfLessThan(limit, 1);
        ArgumentOutOfRangeException.ThrowIfGreaterThan(limit, 500);
        await using var conn = new NpgsqlConnection(connectionString);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync(System.Data.IsolationLevel.RepeatableRead);
        Guid root;
        if (nodeId is Guid n)
        {
            await using var cmd = new NpgsqlCommand("SELECT root_node_id FROM public.plan_node_state WHERE node_id = @n", conn, tx);
            cmd.Parameters.AddWithValue("n", n);
            if (await cmd.ExecuteScalarAsync() is not Guid r) return (null, PlanErrorCodes.NodeNotFound);
            root = r;
        }
        else
        {
            root = rootId!.Value;
            if (await ProjectOfRoot(conn, tx, root) is null) return (null, PlanStoreErrorCodes.PlanNotFound);
        }

        long? first;
        await using (var cmd = new NpgsqlCommand(
            "SELECT min(seq) FROM public.plan_attempt_events WHERE root_node_id = @r AND (@n::uuid IS NULL OR node_id = @n)", conn, tx))
        {
            cmd.Parameters.AddWithValue("r", root);
            cmd.Parameters.Add(new NpgsqlParameter("n", NpgsqlTypes.NpgsqlDbType.Uuid) { Value = (object?)nodeId ?? DBNull.Value });
            first = await cmd.ExecuteScalarAsync() is long f ? f : null;
        }

        var rows = new List<StoredEvent>();
        await using (var cmd = new NpgsqlCommand("""
            SELECT seq, node_id, node_state_revision, kind, work_from, work_to, content_revision, attempt_id, attempt_epoch,
                   executor_ref, artifact_ref, decision, reviewed_content_revision, evidence_ref, content_digest, actor,
                   operation_key, recorded_at
            FROM public.plan_attempt_events
            WHERE root_node_id = @r AND seq > @after AND (@n::uuid IS NULL OR node_id = @n)
            ORDER BY seq
            LIMIT @lim
            """, conn, tx))
        {
            cmd.Parameters.AddWithValue("r", root);
            cmd.Parameters.AddWithValue("after", afterSeq);
            cmd.Parameters.Add(new NpgsqlParameter("n", NpgsqlTypes.NpgsqlDbType.Uuid) { Value = (object?)nodeId ?? DBNull.Value });
            cmd.Parameters.AddWithValue("lim", limit + 1);
            await using var r = await cmd.ExecuteReaderAsync();
            while (await r.ReadAsync())
            {
                var e = new AuditEvent(
                    r.GetGuid(1), r.GetInt64(2), ParseEventKind(r.GetString(3)), ParseWork(r.GetString(4)), ParseWork(r.GetString(5)),
                    r.GetInt64(6), NullableString(r, 7), r.GetInt64(8), NullableString(r, 9), NullableString(r, 10),
                    r.IsDBNull(11) ? null : ParseDecision(r.GetString(11)), r.IsDBNull(12) ? null : r.GetInt64(12),
                    NullableString(r, 13), NullableString(r, 14), r.GetString(15), r.GetString(16));
                rows.Add(new StoredEvent(r.GetInt64(0), e, r.GetDateTime(17)));
            }
        }
        await tx.RollbackAsync();
        var more = rows.Count > limit;
        var page = more ? rows.Take(limit).ToList() : rows;
        return (new EventPage(page, more ? page[^1].Seq : null, first), null);
    }

    private static readonly Dictionary<string, AuditEventKind> EventKindsByName =
        Enum.GetValues<AuditEventKind>().ToDictionary(EventKindName, k => k, StringComparer.Ordinal);

    public static AuditEventKind ParseEventKind(string s) =>
        EventKindsByName.TryGetValue(s, out var k) ? k : throw new FormatException($"Unknown event kind '{s}'.");

    private static async Task<PlanStoreResult> Abort(NpgsqlTransaction tx, PlanStoreResult result)
    {
        await tx.RollbackAsync();
        return result;
    }

    private static Task OpenFence(NpgsqlConnection conn, NpgsqlTransaction tx) =>
        Execute(conn, tx, "SET LOCAL hekate.plan_contract = 'on'");

    private static Task LockProject(NpgsqlConnection conn, NpgsqlTransaction tx, Guid projectId) =>
        Execute(conn, tx, "SELECT pg_advisory_xact_lock(hashtext('hekate-plan-project:' || @p::text))", ("p", projectId));

    private static async Task<Guid?> ProjectOfRoot(NpgsqlConnection conn, NpgsqlTransaction tx, Guid rootId)
    {
        await using var cmd = new NpgsqlCommand("SELECT project_id FROM public.managed_plans WHERE root_node_id = @r", conn, tx);
        cmd.Parameters.AddWithValue("r", rootId);
        return await cmd.ExecuteScalarAsync() is Guid g ? g : null;
    }

    // -----------------------------------------------------------------------
    // Snapshot load
    // -----------------------------------------------------------------------

    private static async Task<PlanSnapshot?> LoadSnapshot(NpgsqlConnection conn, NpgsqlTransaction tx, Guid rootId)
    {
        Guid projectId; GatePolicy defaultGate; string version;
        await using (var cmd = new NpgsqlCommand("SELECT project_id, default_gate, contract_version FROM public.managed_plans WHERE root_node_id = @r", conn, tx))
        {
            cmd.Parameters.AddWithValue("r", rootId);
            await using var r = await cmd.ExecuteReaderAsync();
            if (!await r.ReadAsync()) return null;
            projectId = r.GetGuid(0);
            defaultGate = ParseGate(r.GetString(1));
            version = r.GetString(2);
        }

        var nodes = new List<PlanNode>();
        var states = new List<KeyValuePair<Guid, NodeState>>();
        var names = ImmutableDictionary.CreateBuilder<Guid, string?>();
        var values = new Dictionary<Guid, string?>();
        await using (var cmd = new NpgsqlCommand("""
            SELECT n.id, n.project_id, n.node_type, n.parent_id, n.sibling_order, n.name, n.value,
                   s.content_revision, s.state_revision, s.work_status, s.attempt_id, s.attempt_epoch, s.artifact_ref,
                   s.acc_decision, s.acc_content_revision, s.acc_artifact_ref, s.acc_attempt_id, s.acc_attempt_epoch,
                   s.acc_decided_by, s.acc_evidence_ref, s.last_op_key, s.last_op_fingerprint, s.executor_ref
            FROM public.plan_node_state s JOIN public.nodes n ON n.id = s.node_id
            WHERE s.root_node_id = @r
            """, conn, tx))
        {
            cmd.Parameters.AddWithValue("r", rootId);
            await using var r = await cmd.ExecuteReaderAsync();
            while (await r.ReadAsync())
            {
                // Columns: 0 id, 1 project, 2 type, 3 parent, 4 order, 5 name, 6 value, 7 content_rev, 8 state_rev,
                // 9 work, 10 attempt_id, 11 epoch, 12 artifact, 13 acc_decision, 14 acc_content_rev, 15 acc_artifact,
                // 16 acc_attempt_id, 17 acc_epoch, 18 acc_decided_by, 19 acc_evidence, 20 last_key, 21 last_fp, 22 executor_ref.
                // The stored parent is kept as-is (no normalisation), so a corrupt root parent fails validation.
                var id = r.GetGuid(0);
                Guid? parent = r.IsDBNull(3) ? null : r.GetGuid(3);
                nodes.Add(new PlanNode(id, r.GetGuid(1), r.GetString(2), parent, r.GetInt32(4), r.GetInt64(7)));
                names[id] = NullableString(r, 5);
                values[id] = NullableString(r, 6);
                // Incomplete stored decisions load with sentinel values so validation rejects them
                // (invalid_state) instead of the reader throwing.
                AcceptanceRecord? acceptance = r.IsDBNull(13) ? null : new AcceptanceRecord(
                    ParseDecision(r.GetString(13)), r.IsDBNull(14) ? 0 : r.GetInt64(14), NullableString(r, 15), NullableString(r, 16),
                    r.IsDBNull(17) ? 0 : r.GetInt64(17), NullableString(r, 18) ?? "", NullableString(r, 19));
                states.Add(new(id, new NodeState(
                    ParseWork(r.GetString(9)), r.GetInt64(8), NullableString(r, 10), r.GetInt64(11), NullableString(r, 12),
                    acceptance, NullableString(r, 20), NullableString(r, 21), NullableString(r, 22))));
            }
        }

        var deps = new List<Dependency>();
        await using (var cmd = new NpgsqlCommand("SELECT predecessor_id, successor_id, gate FROM public.plan_dependencies WHERE root_node_id = @r", conn, tx))
        {
            cmd.Parameters.AddWithValue("r", rootId);
            await using var r = await cmd.ExecuteReaderAsync();
            while (await r.ReadAsync())
                deps.Add(new Dependency(r.GetGuid(0), r.GetGuid(1), r.IsDBNull(2) ? null : ParseGate(r.GetString(2))));
        }

        var attrs = values.Keys.ToDictionary(k => k, _ => new Dictionary<string, string>());
        await using (var cmd = new NpgsqlCommand("""
            SELECT a.node_id, a.key, a.value FROM public.node_attributes a
            JOIN public.plan_node_state s ON s.node_id = a.node_id
            WHERE s.root_node_id = @r AND a.key = ANY(@keys)
            """, conn, tx))
        {
            cmd.Parameters.AddWithValue("r", rootId);
            cmd.Parameters.AddWithValue("keys", PlanContentDigest.ContentAttributeKeys.ToArray());
            await using var r = await cmd.ExecuteReaderAsync();
            while (await r.ReadAsync()) attrs[r.GetGuid(0)][r.GetString(1)] = r.GetString(2);
        }
        var contents = values.ToImmutableDictionary(kv => kv.Key,
            kv => new PlanContent(kv.Value, attrs[kv.Key].Count == 0 ? null : attrs[kv.Key]));

        return new PlanSnapshot(PlanGraph.Create(projectId, rootId, nodes, deps, states, defaultGate), names.ToImmutable(), contents, version);
    }

    private static string? NullableString(NpgsqlDataReader r, int i) => r.IsDBNull(i) ? null : r.GetString(i);

    // -----------------------------------------------------------------------
    // Persistence
    // -----------------------------------------------------------------------

    private static async Task PersistStates(NpgsqlConnection conn, NpgsqlTransaction tx, Guid rootId, PlanGraph before, PlanGraph after, string actor)
    {
        foreach (var (id, node) in after.Nodes.OrderBy(kv => kv.Key))
        {
            var next = after.StateOf(id);
            if (!before.Nodes.TryGetValue(id, out var prevNode))
            {
                await InsertState(conn, tx, id, rootId, node.ContentRevision, next, actor);
                continue;
            }
            var prev = before.StateOf(id);
            if (prev == next && prevNode.ContentRevision == node.ContentRevision) continue;

            await using var cmd = new NpgsqlCommand("""
                UPDATE public.plan_node_state SET
                    content_revision = @cr, state_revision = @sr, work_status = @ws, attempt_id = @aid, attempt_epoch = @ae,
                    artifact_ref = @art, acc_decision = @ad, acc_content_revision = @acr, acc_artifact_ref = @aart,
                    acc_attempt_id = @aaid, acc_attempt_epoch = @aae, acc_decided_by = @adb, acc_evidence_ref = @aev,
                    last_op_key = @lk, last_op_fingerprint = @lf, executor_ref = @eref, updated_at = now(), updated_by = @by
                WHERE node_id = @id AND state_revision = @expected
                """, conn, tx);
            AddStateParameters(cmd, node.ContentRevision, next, actor);
            cmd.Parameters.AddWithValue("id", id);
            cmd.Parameters.AddWithValue("expected", prev.StateRevision);
            if (await cmd.ExecuteNonQueryAsync() != 1)
                throw new ConcurrencyConflictException($"Node {id} changed underneath the plan-contract transaction.");
        }
    }

    private static async Task InsertState(NpgsqlConnection conn, NpgsqlTransaction tx, Guid id, Guid rootId, long contentRevision, NodeState s, string actor)
    {
        await using var cmd = new NpgsqlCommand("""
            INSERT INTO public.plan_node_state (node_id, root_node_id, content_revision, state_revision, work_status, attempt_id,
                attempt_epoch, artifact_ref, acc_decision, acc_content_revision, acc_artifact_ref, acc_attempt_id, acc_attempt_epoch,
                acc_decided_by, acc_evidence_ref, last_op_key, last_op_fingerprint, executor_ref, updated_by)
            VALUES (@id, @root, @cr, @sr, @ws, @aid, @ae, @art, @ad, @acr, @aart, @aaid, @aae, @adb, @aev, @lk, @lf, @eref, @by)
            """, conn, tx);
        AddStateParameters(cmd, contentRevision, s, actor);
        cmd.Parameters.AddWithValue("id", id);
        cmd.Parameters.AddWithValue("root", rootId);
        await cmd.ExecuteNonQueryAsync();
    }

    private static void AddStateParameters(NpgsqlCommand cmd, long contentRevision, NodeState s, string actor)
    {
        object N(object? v) => v ?? DBNull.Value;
        var a = s.Acceptance;
        cmd.Parameters.AddWithValue("cr", contentRevision);
        cmd.Parameters.AddWithValue("sr", s.StateRevision);
        cmd.Parameters.AddWithValue("ws", WorkName(s.Work));
        cmd.Parameters.AddWithValue("aid", N(s.AttemptId));
        cmd.Parameters.AddWithValue("ae", s.AttemptEpoch);
        cmd.Parameters.AddWithValue("art", N(s.ArtifactRef));
        cmd.Parameters.AddWithValue("ad", N(a is null ? null : DecisionName(a.Decision)));
        cmd.Parameters.AddWithValue("acr", N(a?.ContentRevision));
        cmd.Parameters.AddWithValue("aart", N(a?.ArtifactRef));
        cmd.Parameters.AddWithValue("aaid", N(a?.AttemptId));
        cmd.Parameters.AddWithValue("aae", N(a?.AttemptEpoch));
        cmd.Parameters.AddWithValue("adb", N(a?.DecidedBy));
        cmd.Parameters.AddWithValue("aev", N(a?.EvidenceRef));
        cmd.Parameters.AddWithValue("lk", N(s.LastOperationKey));
        cmd.Parameters.AddWithValue("lf", N(s.LastOperationFingerprint));
        cmd.Parameters.AddWithValue("eref", N(s.ExecutorRef));
        cmd.Parameters.AddWithValue("by", actor);
    }

    private static async Task PersistDependencies(NpgsqlConnection conn, NpgsqlTransaction tx, Guid rootId, PlanGraph before, PlanGraph after)
    {
        var old = before.Dependencies.ToDictionary(d => (d.PredecessorId, d.SuccessorId));
        var neu = after.Dependencies.ToDictionary(d => (d.PredecessorId, d.SuccessorId));
        foreach (var key in old.Keys.Where(k => !neu.ContainsKey(k)).OrderBy(k => k))
            await Execute(conn, tx, "DELETE FROM public.plan_dependencies WHERE predecessor_id = @p AND successor_id = @s",
                ("p", key.PredecessorId), ("s", key.SuccessorId));
        foreach (var d in neu.Values.Where(d => !old.ContainsKey((d.PredecessorId, d.SuccessorId))).OrderBy(d => (d.SuccessorId, d.PredecessorId)))
            await Execute(conn, tx, "INSERT INTO public.plan_dependencies (root_node_id, predecessor_id, successor_id, gate) VALUES (@r, @p, @s, @g)",
                ("r", rootId), ("p", d.PredecessorId), ("s", d.SuccessorId), ("g", d.Gate is GatePolicy gp ? GateName(gp) : DBNull.Value));
    }

    private static async Task InsertNode(NpgsqlConnection conn, NpgsqlTransaction tx, Guid id, Guid projectId, string nodeType,
        string name, Guid? parentId, int siblingOrder, PlanContent content, string actor)
    {
        await Execute(conn, tx, """
            INSERT INTO public.nodes (id, project_id, node_type, name, value, parent_id, sibling_order, modified_by)
            VALUES (@id, @p, @t, @n, @v, @parent, @o, @by)
            """,
            ("id", id), ("p", projectId), ("t", nodeType), ("n", name), ("v", (object?)content.Value ?? DBNull.Value),
            ("parent", (object?)parentId ?? DBNull.Value), ("o", siblingOrder), ("by", actor));
        foreach (var (key, value) in (content.Attributes ?? new Dictionary<string, string>()).OrderBy(kv => kv.Key, StringComparer.Ordinal))
            await Execute(conn, tx, "INSERT INTO public.node_attributes (node_id, key, value) VALUES (@id, @k, @v)", ("id", id), ("k", key), ("v", value));
    }

    private static async Task WriteContent(NpgsqlConnection conn, NpgsqlTransaction tx, Guid id, PlanContent content, string actor)
    {
        await Execute(conn, tx, "UPDATE public.nodes SET value = @v, modified_at = now(), modified_by = @by WHERE id = @id",
            ("v", (object?)content.Value ?? DBNull.Value), ("by", actor), ("id", id));
        var attrs = content.Attributes ?? new Dictionary<string, string>();
        await using (var del = new NpgsqlCommand("DELETE FROM public.node_attributes WHERE node_id = @id AND key = ANY(@keys)", conn, tx))
        {
            del.Parameters.AddWithValue("id", id);
            del.Parameters.AddWithValue("keys", PlanContentDigest.ContentAttributeKeys.ToArray());
            await del.ExecuteNonQueryAsync();
        }
        foreach (var (key, value) in attrs.OrderBy(kv => kv.Key, StringComparer.Ordinal))
            await Execute(conn, tx, "INSERT INTO public.node_attributes (node_id, key, value) VALUES (@id, @k, @v)", ("id", id), ("k", key), ("v", value));
    }

    private static PlanStoreResult? ValidateContent(PlanContent content)
    {
        foreach (var key in (content.Attributes ?? new Dictionary<string, string>()).Keys)
        {
            if (!PlanContentDigest.ContentAttributeKeys.Contains(key))
                return PlanStoreResult.Fail(PlanStoreErrorCodes.InvalidContent,
                    $"'{key}' is not a content attribute. Allowed: {string.Join(", ", PlanContentDigest.ContentAttributeKeys.OrderBy(k => k))}.");
        }
        if (content.Attributes?.Values.Any(v => v is null) == true)
            return PlanStoreResult.Fail(PlanStoreErrorCodes.InvalidContent, "Content attribute values cannot be null; omit the key instead.");
        return null;
    }

    // -----------------------------------------------------------------------
    // AGE projection (same transaction, convergent)
    // -----------------------------------------------------------------------

    private async Task<PlanStoreResult?> TryProject(NpgsqlConnection conn, NpgsqlTransaction tx, Guid rootId, PlanGraph g)
    {
        try
        {
            await Project(conn, tx, rootId, g);
            return null;
        }
        catch (Exception ex) when (ex is PostgresException or ProjectionMismatchException)
        {
            var code = ex is PostgresException pe ? pe.SqlState : "mismatch";
            Console.WriteLine($"[PLAN]     AGE projection failed for plan {rootId}: {ex.GetType().Name} ({code})");
            return PlanStoreResult.Fail(PlanStoreErrorCodes.ProjectionFailed, "The plan graph projection failed; nothing was written.", rootId);
        }
    }

    private sealed class ProjectionMismatchException(string message) : Exception(message);

    private async Task Project(NpgsqlConnection conn, NpgsqlTransaction tx, Guid rootId, PlanGraph g)
    {
        await Execute(conn, tx, "LOAD 'age'");
        await Execute(conn, tx, "SET LOCAL search_path = ag_catalog, \"$user\", public");

        foreach (var node in g.Nodes.Values.OrderBy(n => n.Id))
            await Cypher(conn, tx, $"MERGE (n:CodeNode {{node_id: '{node.Id}'}}) SET n.node_type = '{Token(node.NodeType)}' RETURN 1");

        var desired = g.Dependencies.Select(d => (Succ: d.SuccessorId, Pred: d.PredecessorId)).ToHashSet();
        var existing = await TaggedEdges(conn, tx, rootId);

        foreach (var pair in existing.Keys.OrderBy(k => k))
        {
            if (!desired.Contains(pair) || existing[pair] > 1)
                await Cypher(conn, tx,
                    $"MATCH (a:CodeNode {{node_id: '{pair.Succ}'}})-[e:DEPENDS_ON {{plan_root: '{rootId}'}}]->(b:CodeNode {{node_id: '{pair.Pred}'}}) DELETE e RETURN 1");
        }
        foreach (var pair in desired.OrderBy(k => k))
        {
            if (existing.TryGetValue(pair, out var n) && n == 1) continue;
            await Cypher(conn, tx,
                $"MATCH (a:CodeNode {{node_id: '{pair.Succ}'}}), (b:CodeNode {{node_id: '{pair.Pred}'}}) CREATE (a)-[:DEPENDS_ON {{plan_root: '{rootId}'}}]->(b) RETURN 1");
        }

        var after = await TaggedEdges(conn, tx, rootId);
        if (after.Count != desired.Count || after.Any(kv => kv.Value != 1 || !desired.Contains(kv.Key)))
            throw new ProjectionMismatchException($"Plan {rootId} projection did not converge.");
    }

    private async Task<Dictionary<(Guid Succ, Guid Pred), int>> TaggedEdges(NpgsqlConnection conn, NpgsqlTransaction tx, Guid rootId)
    {
        var result = new Dictionary<(Guid, Guid), int>();
        var sql = $"SELECT a::text, b::text FROM cypher('{Token(graphName)}', $$ MATCH (x:CodeNode)-[e:DEPENDS_ON {{plan_root: '{rootId}'}}]->(y:CodeNode) RETURN x.node_id, y.node_id $$) AS (a agtype, b agtype)";
        await using var cmd = new NpgsqlCommand(sql, conn, tx);
        await using var r = await cmd.ExecuteReaderAsync();
        while (await r.ReadAsync())
        {
            var key = (Guid.Parse(r.GetString(0).Trim('"')), Guid.Parse(r.GetString(1).Trim('"')));
            result[key] = result.TryGetValue(key, out var c) ? c + 1 : 1;
        }
        return result;
    }

    private Task Cypher(NpgsqlConnection conn, NpgsqlTransaction tx, string cypher) =>
        Execute(conn, tx, $"SELECT * FROM cypher('{Token(graphName)}', $$ {cypher} $$) AS (r agtype)");

    /// <summary>Identifiers interpolated into Cypher are restricted to [a-z0-9_]; ids are Guids.</summary>
    private static string Token(string s) =>
        s.All(c => char.IsAsciiLetterLower(c) || char.IsAsciiDigit(c) || c == '_') ? s : throw new ArgumentException($"Unsafe token '{s}'.");

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    private static async Task Execute(NpgsqlConnection conn, NpgsqlTransaction tx, string sql, params (string Name, object Value)[] parameters)
    {
        await using var cmd = new NpgsqlCommand(sql, conn, tx);
        foreach (var (name, value) in parameters) cmd.Parameters.AddWithValue(name, value);
        await cmd.ExecuteNonQueryAsync();
    }

    private static async Task<bool> Exists(NpgsqlConnection conn, NpgsqlTransaction tx, string sql, Guid id)
    {
        await using var cmd = new NpgsqlCommand(sql, conn, tx);
        cmd.Parameters.AddWithValue("id", id);
        return await cmd.ExecuteScalarAsync() is not null;
    }

    public static string WorkName(WorkStatus w) => w switch
    {
        WorkStatus.Todo => "todo", WorkStatus.InProgress => "in_progress", WorkStatus.Done => "done", WorkStatus.Cancelled => "cancelled",
        _ => throw new ArgumentOutOfRangeException(nameof(w)),
    };

    public static WorkStatus ParseWork(string s) => s switch
    {
        "todo" => WorkStatus.Todo, "in_progress" => WorkStatus.InProgress, "done" => WorkStatus.Done, "cancelled" => WorkStatus.Cancelled,
        _ => throw new FormatException($"Unknown work status '{s}'."),
    };

    public static string GateName(GatePolicy g) => g switch
    {
        GatePolicy.Completed => "completed", GatePolicy.Accepted => "accepted", _ => throw new ArgumentOutOfRangeException(nameof(g)),
    };

    public static GatePolicy ParseGate(string s) => s switch
    {
        "completed" => GatePolicy.Completed, "accepted" => GatePolicy.Accepted, _ => throw new FormatException($"Unknown gate '{s}'."),
    };

    public static string DecisionName(AcceptanceDecision d) => d switch
    {
        AcceptanceDecision.Accepted => "accepted", AcceptanceDecision.Rejected => "rejected", _ => throw new ArgumentOutOfRangeException(nameof(d)),
    };

    public static AcceptanceDecision ParseDecision(string s) => s switch
    {
        "accepted" => AcceptanceDecision.Accepted, "rejected" => AcceptanceDecision.Rejected, _ => throw new FormatException($"Unknown decision '{s}'."),
    };
}
