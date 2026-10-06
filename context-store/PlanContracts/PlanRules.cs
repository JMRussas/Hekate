// CodeStoragePoc - Plan node contracts v1: rules
//
// Pure functions over PlanGraph snapshots: structural validation, derived readiness
// and container status, and state-changing operations. No I/O, no clock, no random
// identities — every key, attempt and actor is an input, so results are replayable.
// Operations never mutate their input; a rejection returns the input graph itself.
// See plans/012-plan-node-contracts-v1.md.
//
// Depends on: PlanContracts/PlanModel, ContextRouter/NodeTypes
// Used by:    PlanContracts.Tests (store integration is a later increment)

using System.Collections.Immutable;
using System.Text;
using CodeStoragePoc.ContextRouter;
using static CodeStoragePoc.PlanContracts.PlanErrorCodes;

namespace CodeStoragePoc.PlanContracts;

/// <summary>Derived view of a valid plan. When Errors is non-empty the other lists are empty.</summary>
public sealed record PlanView(
    ImmutableArray<PlanError> Errors, ImmutableArray<LeafReadiness> Leaves, ImmutableArray<ContainerStatus> Containers)
{
    public IEnumerable<LeafReadiness> ReadyWork => Leaves.Where(l => l.Ready);
}

public static class PlanRules
{
    // -----------------------------------------------------------------------
    // Validation
    // -----------------------------------------------------------------------

    /// <summary>All structural and content problems, in a deterministic order (empty = valid).</summary>
    public static ImmutableArray<PlanError> ValidateGraph(PlanGraph g)
    {
        var errors = ImmutableArray.CreateBuilder<PlanError>();

        if (!Enum.IsDefined(g.DefaultGate))
            errors.Add(new(InvalidEnum, $"Unknown default gate policy {(int)g.DefaultGate}."));

        if (!g.Nodes.TryGetValue(g.RootId, out var root))
            errors.Add(new(MissingRoot, $"Root {g.RootId} is not in the snapshot.", g.RootId));
        else
        {
            if (root.NodeType != NodeTypes.Plan)
                errors.Add(new(InvalidRootType, $"Root must be a '{NodeTypes.Plan}' node, not '{root.NodeType}'.", g.RootId));
            // The root's parent (if any) lives outside the plan; pointing inside would hide a cycle.
            if (root.ParentId is Guid rp && g.Nodes.ContainsKey(rp))
                errors.Add(new(InvalidRootParent, "The root's parent must be outside the plan snapshot.", g.RootId, rp));
        }

        var hasChildren = g.Nodes.Values
            .Where(n => n.Id != g.RootId && n.ParentId is Guid p && g.Nodes.ContainsKey(p))
            .Select(n => n.ParentId!.Value).ToHashSet();

        foreach (var id in g.Nodes.Keys.OrderBy(k => k))
        {
            var n = g.Nodes[id];
            if (n.Id != id) errors.Add(new(NodeKeyMismatch, $"Snapshot key {id} holds node {n.Id}.", id));
            if (n.ProjectId != g.ProjectId) errors.Add(new(CrossProject, $"Node belongs to project {n.ProjectId}, not {g.ProjectId}.", id));
            if (!PlanNodeTypes.Structural.Contains(n.NodeType)) errors.Add(new(InvalidNodeType, $"'{n.NodeType}' is not a plan structure type.", id));
            if (id != g.RootId && (n.ParentId is not Guid p || !g.Nodes.ContainsKey(p)))
                errors.Add(new(MissingParent, $"Parent {n.ParentId?.ToString() ?? "(none)"} is not in the plan.", id, n.ParentId));
            if (n.ContentRevision < 1) errors.Add(new(InvalidRevision, $"Content revision {n.ContentRevision} must be >= 1.", id));
        }

        foreach (var id in g.States.Keys.OrderBy(k => k))
        {
            if (!g.Nodes.ContainsKey(id)) { errors.Add(new(OrphanState, $"State exists for unknown node {id}.", id)); continue; }
            var isContainer = id == g.RootId || hasChildren.Contains(id);
            foreach (var e in ValidateState(id, g.States[id], isContainer)) errors.Add(e);
        }

        foreach (var cycle in FindHierarchyCycles(g))
            errors.Add(new(HierarchyCycle, $"Parent links form a cycle: {string.Join(" -> ", cycle)}.", cycle.Min()));

        var hierarchyOk = errors.Count == 0;
        var seen = new HashSet<(Guid, Guid)>();
        foreach (var d in g.Dependencies.OrderBy(d => d.SuccessorId).ThenBy(d => d.PredecessorId))
        {
            if (d.Gate is GatePolicy gp && !Enum.IsDefined(gp))
            {
                errors.Add(new(InvalidEnum, $"Unknown gate policy {(int)gp}.", d.SuccessorId, d.PredecessorId));
                continue;
            }
            if (!g.Nodes.ContainsKey(d.PredecessorId) || !g.Nodes.ContainsKey(d.SuccessorId))
            {
                errors.Add(new(MissingDependencyEndpoint, $"Dependency {d.PredecessorId} -> {d.SuccessorId} references a node outside the plan.", d.SuccessorId, d.PredecessorId));
                continue;
            }
            if (d.PredecessorId == d.SuccessorId)
            {
                errors.Add(new(SelfDependency, "A node cannot depend on itself.", d.SuccessorId));
                continue;
            }
            if (!seen.Add((d.PredecessorId, d.SuccessorId)))
            {
                errors.Add(new(DuplicateDependency, $"Dependency on {d.PredecessorId} is declared more than once.", d.SuccessorId, d.PredecessorId));
                continue;
            }
            if (hierarchyOk && (IsAncestor(g, d.PredecessorId, d.SuccessorId) || IsAncestor(g, d.SuccessorId, d.PredecessorId)))
                errors.Add(new(DependencyOnAncestor, "A node cannot depend on its own ancestor or descendant (containment already orders them).", d.SuccessorId, d.PredecessorId));
        }
        if (errors.Count > 0) return errors.ToImmutable();

        var ix = new Index(g);
        var depCycle = FindCycle(ix.Ordered, ix.PredecessorsDeclaredBy);
        if (depCycle is not null)
        {
            errors.Add(new(DependencyCycle, $"Dependencies form a cycle: {string.Join(" -> ", depCycle)}.", depCycle[0]));
            return errors.ToImmutable();
        }
        // Completion waits-for graph: a container waits for its children; a leaf waits for
        // every predecessor declared on it or on any ancestor (it cannot start before them).
        var deadlock = FindCycle(ix.Ordered, id => ix.IsContainer(id)
            ? ix.ChildrenOf(id)
            : ix.OwnersOf(id).SelectMany(ix.PredecessorsDeclaredBy).Distinct().OrderBy(x => x));
        if (deadlock is not null)
            errors.Add(new(HierarchyDependencyDeadlock, $"Dependencies combined with containment can never complete: {string.Join(" -> ", deadlock)}.", deadlock[0]));
        return errors.ToImmutable();
    }

    private static IEnumerable<PlanError> ValidateState(Guid id, NodeState s, bool isContainer)
    {
        if (!Enum.IsDefined(s.Work)) { yield return new(InvalidEnum, $"Unknown work status {(int)s.Work}.", id); yield break; }
        if (s.StateRevision < 0 || s.AttemptEpoch < 0) yield return new(InvalidRevision, "State revision and attempt epoch must be >= 0.", id);
        if (isContainer)
        {
            if (s.Work != WorkStatus.Todo || s.AttemptId is not null || s.AttemptEpoch != 0 || s.ArtifactRef is not null || s.Acceptance is not null)
                yield return new(InvalidState, "A container cannot hold work, attempt, artifact or acceptance state (it is derived).", id);
            yield break;
        }
        switch (s.Work)
        {
            case WorkStatus.InProgress when string.IsNullOrWhiteSpace(s.AttemptId) || s.AttemptEpoch < 1:
                yield return new(InvalidState, "In-progress work needs an attempt id and epoch.", id); break;
            case WorkStatus.Done when string.IsNullOrWhiteSpace(s.AttemptId) || s.AttemptEpoch < 1:
                yield return new(InvalidState, "Done work needs the id and epoch of the attempt that finished it.", id); break;
            case WorkStatus.Todo or WorkStatus.Cancelled when s.AttemptId is not null:
                yield return new(InvalidState, $"{s.Work} work cannot hold a current attempt.", id); break;
        }
        // A loaded decision must meet the same contract Decide enforces, or a snapshot could
        // smuggle in an acceptance that satisfies successors without evidence.
        if (s.Acceptance is { } a)
        {
            if (!Enum.IsDefined(a.Decision)) yield return new(InvalidEnum, $"Unknown acceptance decision {(int)a.Decision}.", id);
            if (a.ContentRevision < 1 || a.AttemptEpoch < 1 || a.AttemptEpoch > s.AttemptEpoch)
                yield return new(InvalidState, "Acceptance must reference a real content revision and an issued attempt epoch.", id);
            if (string.IsNullOrWhiteSpace(a.DecidedBy) || string.IsNullOrWhiteSpace(a.EvidenceRef) || string.IsNullOrWhiteSpace(a.AttemptId))
                yield return new(InvalidState, "Acceptance must record its decider, evidence and attempt id.", id);
            if (a.Decision == AcceptanceDecision.Accepted && string.IsNullOrWhiteSpace(a.ArtifactRef))
                yield return new(InvalidState, "An accepted decision must name the reviewed artifact.", id);
            if (a.AttemptEpoch == s.AttemptEpoch && s.AttemptId is not null && a.AttemptId != s.AttemptId)
                yield return new(InvalidState, "Acceptance for the current epoch must name the current attempt id.", id);
        }
    }

    // -----------------------------------------------------------------------
    // Derived state (readiness is never stored)
    // -----------------------------------------------------------------------

    /// <summary>Acceptance as it applies now: Stale if content, artifact or attempt changed, or the node is no longer Done.</summary>
    public static EffectiveAcceptance EffectiveAcceptanceOf(PlanGraph g, Guid nodeId)
    {
        var s = g.StateOf(nodeId);
        var a = s.Acceptance;
        if (a is null) return EffectiveAcceptance.None;
        var n = g.Nodes[nodeId];
        if (s.Work != WorkStatus.Done || a.ContentRevision != n.ContentRevision || a.ArtifactRef != s.ArtifactRef || a.AttemptEpoch != s.AttemptEpoch)
            return EffectiveAcceptance.Stale;
        return a.Decision == AcceptanceDecision.Accepted ? EffectiveAcceptance.Accepted : EffectiveAcceptance.Rejected;
    }

    /// <summary>
    /// A rejection keeps blocking while it applies to the current attempt and artifact,
    /// even after a content revision: revising requirements does not un-reject old work.
    /// </summary>
    private static bool IsCurrentRejection(NodeState s) =>
        s.Work == WorkStatus.Done && s.Acceptance is { Decision: AcceptanceDecision.Rejected } a
        && a.AttemptEpoch == s.AttemptEpoch && a.ArtifactRef == s.ArtifactRef;

    public static PlanView Evaluate(PlanGraph g)
    {
        var errors = ValidateGraph(g);
        if (errors.Length > 0) return new PlanView(errors, [], []);
        var ix = new Index(g);
        var leaves = ImmutableArray.CreateBuilder<LeafReadiness>();
        var containers = ImmutableArray.CreateBuilder<ContainerStatus>();
        foreach (var id in ix.HierarchyOrder())
        {
            if (ix.IsContainer(id)) { containers.Add(ix.Container(id)); continue; }
            var blockers = ix.BlockersFor(id);
            var work = g.StateOf(id).Work;
            leaves.Add(new LeafReadiness(id, work, work == WorkStatus.Todo && blockers.IsEmpty, blockers.IsEmpty, blockers));
        }
        return new PlanView([], leaves.ToImmutable(), containers.ToImmutable());
    }

    // -----------------------------------------------------------------------
    // Operations
    // -----------------------------------------------------------------------

    public static OpResult AddDependency(PlanGraph g, Guid predecessorId, Guid successorId, GatePolicy? gate, OperationContext ctx)
    {
        var fingerprint = Fingerprint("add_dependency", ctx.Actor, predecessorId, gate);
        if (gate is GatePolicy gp && !Enum.IsDefined(gp))
            return Rejected(g, new PlanError(InvalidEnum, $"Unknown gate policy {(int)gp}.", successorId, predecessorId));
        if (Prologue(g, successorId, ctx, fingerprint) is { } early) return early;

        var candidate = g with { Dependencies = g.Dependencies.Add(new Dependency(predecessorId, successorId, gate)) };
        var errors = ValidateGraph(candidate);
        if (errors.Length > 0) return Rejected(g, errors);
        return Applied(Commit(candidate, successorId, ctx, fingerprint, s => s));
    }

    public static OpResult RemoveDependency(PlanGraph g, Guid predecessorId, Guid successorId, OperationContext ctx)
    {
        var fingerprint = Fingerprint("remove_dependency", ctx.Actor, predecessorId);
        if (Prologue(g, successorId, ctx, fingerprint) is { } early) return early;

        var existing = g.Dependencies.FirstOrDefault(d => d.PredecessorId == predecessorId && d.SuccessorId == successorId);
        if (existing is null) return Rejected(g, new PlanError(DependencyNotFound, $"No dependency on {predecessorId}.", successorId, predecessorId));
        var candidate = g with { Dependencies = g.Dependencies.Remove(existing) };
        return Applied(Commit(candidate, successorId, ctx, fingerprint, s => s));
    }

    /// <summary>
    /// Add a new structural child under child.ParentId. The operation belongs to the parent
    /// (ctx is checked against the parent's state). <paramref name="payloadDigest"/> covers
    /// the child's stored content (name, value, attributes) so a reused key with a different
    /// payload is operation_key_reused. Never overwrites: the id must be new and non-empty.
    /// A pristine leaf may become a container; a leaf that holds work state may not (the
    /// candidate graph fails validation with invalid_state).
    /// </summary>
    public static OpResult AddChild(PlanGraph g, PlanNode child, OperationContext ctx, string payloadDigest)
    {
        var parentId = child.ParentId ?? Guid.Empty;
        var fingerprint = Fingerprint("add_child", ctx.Actor, child.Id, child.ProjectId, child.NodeType, child.SiblingOrder, child.ParentId, child.ContentRevision, payloadDigest);
        if (Prologue(g, parentId, ctx, fingerprint) is { } early) return early;

        if (child.Id == Guid.Empty || g.Nodes.ContainsKey(child.Id))
            return Rejected(g, new PlanError(InvalidChild, "A child needs a new, non-empty id.", parentId, child.Id));
        if (child.ProjectId != g.ProjectId)
            return Rejected(g, new PlanError(CrossProject, $"Child belongs to project {child.ProjectId}, not {g.ProjectId}.", parentId, child.Id));
        if (!PlanNodeTypes.Structural.Contains(child.NodeType))
            return Rejected(g, new PlanError(InvalidNodeType, $"'{child.NodeType}' is not a plan structure type.", parentId, child.Id));
        if (child.NodeType == NodeTypes.Plan)
            return Rejected(g, new PlanError(InvalidChild, "A 'plan' node can only be a plan root.", parentId, child.Id));
        if (child.ContentRevision != 1)
            return Rejected(g, new PlanError(InvalidChild, "A new child starts at content revision 1.", parentId, child.Id));

        var candidate = g with { Nodes = g.Nodes.Add(child.Id, child) };
        var errors = ValidateGraph(candidate);
        if (errors.Length > 0) return Rejected(g, errors);
        return Applied(Commit(candidate, parentId, ctx, fingerprint, s => s));
    }

    /// <summary>
    /// Leaf work transitions. Starting (or reopening) needs an attempt id and satisfied
    /// gates, and issues a new attempt epoch. Finishing or releasing must present the
    /// current attempt id AND epoch. Cancelling clears the attempt, fencing late results.
    /// </summary>
    public static OpResult Transition(
        PlanGraph g, Guid nodeId, WorkStatus to, OperationContext ctx,
        string? attemptId = null, long? attemptEpoch = null, string? artifactRef = null)
    {
        var fingerprint = Fingerprint("transition", ctx.Actor, to, attemptId, attemptEpoch, artifactRef);
        if (!Enum.IsDefined(to)) return Rejected(g, new PlanError(InvalidEnum, $"Unknown work status {(int)to}.", nodeId));
        if (Prologue(g, nodeId, ctx, fingerprint) is { } early) return early;

        var ix = new Index(g);
        if (ix.IsContainer(nodeId)) return Rejected(g, new PlanError(ContainerStateIsDerived, "Container status is derived from its children.", nodeId));

        var s = g.StateOf(nodeId);
        if (!AllowedTransitions.Contains((s.Work, to)))
            return Rejected(g, new PlanError(InvalidTransition, $"{s.Work} -> {to} is not allowed.", nodeId));

        Func<NodeState, NodeState> apply;
        switch (s.Work, to)
        {
            case (WorkStatus.Todo or WorkStatus.Done, WorkStatus.InProgress):
                if (string.IsNullOrWhiteSpace(attemptId))
                    return Rejected(g, new PlanError(AttemptRequired, "Starting work requires an attempt id.", nodeId));
                if (s.AttemptEpoch == long.MaxValue)
                    return Rejected(g, new PlanError(RevisionExhausted, "The node's attempt epoch counter is exhausted.", nodeId));
                var blockers = ix.BlockersFor(nodeId);
                if (!blockers.IsEmpty)
                    return new OpResult(OpOutcome.Rejected, g, [new PlanError(NotReady, "Predecessor gates are not satisfied.", nodeId)], blockers);
                apply = st => st with { Work = WorkStatus.InProgress, AttemptId = attemptId, AttemptEpoch = st.AttemptEpoch + 1 };
                break;
            case (WorkStatus.InProgress, WorkStatus.Done or WorkStatus.Todo):
                if (attemptId != s.AttemptId || attemptEpoch != s.AttemptEpoch)
                    return Rejected(g, new PlanError(StaleAttempt, $"Attempt '{attemptId}'/{attemptEpoch} is not the current attempt.", nodeId));
                apply = to == WorkStatus.Done
                    ? st => st with { Work = WorkStatus.Done, ArtifactRef = artifactRef }
                    : st => st with { Work = WorkStatus.Todo, AttemptId = null };
                break;
            case (_, WorkStatus.Cancelled):
                apply = st => st with { Work = WorkStatus.Cancelled, AttemptId = null };
                break;
            default: // Cancelled -> Todo
                apply = st => st with { Work = WorkStatus.Todo };
                break;
        }
        return Applied(Commit(g, nodeId, ctx, fingerprint, apply));
    }

    /// <summary>
    /// Record a review decision on a Done leaf. The caller states the content revision,
    /// artifact and attempt epoch it reviewed; any mismatch is rejected. Every decision
    /// needs an evidence reference; accepting also needs an artifact and re-checks the
    /// node's own gates. Repeating the identical decision is Unchanged.
    /// </summary>
    public static OpResult Decide(
        PlanGraph g, Guid nodeId, AcceptanceDecision decision, long reviewedContentRevision,
        string? reviewedArtifactRef, long reviewedAttemptEpoch, string? evidenceRef, OperationContext ctx)
    {
        var fingerprint = Fingerprint("decide", ctx.Actor, decision, reviewedContentRevision, reviewedArtifactRef, reviewedAttemptEpoch, evidenceRef);
        if (!Enum.IsDefined(decision)) return Rejected(g, new PlanError(InvalidEnum, $"Unknown acceptance decision {(int)decision}.", nodeId));
        if (Prologue(g, nodeId, ctx, fingerprint) is { } early) return early;

        var ix = new Index(g);
        if (ix.IsContainer(nodeId)) return Rejected(g, new PlanError(ContainerStateIsDerived, "Container acceptance is derived from its children.", nodeId));

        var s = g.StateOf(nodeId);
        var n = g.Nodes[nodeId];
        if (s.Work != WorkStatus.Done) return Rejected(g, new PlanError(NotCompleted, "Only completed work can be accepted or rejected.", nodeId));
        if (reviewedContentRevision != n.ContentRevision)
            return Rejected(g, new PlanError(StaleContent, $"Reviewed content revision {reviewedContentRevision}; current is {n.ContentRevision}.", nodeId));
        if (reviewedArtifactRef != s.ArtifactRef)
            return Rejected(g, new PlanError(StaleArtifact, $"Reviewed artifact '{reviewedArtifactRef}'; current is '{s.ArtifactRef}'.", nodeId));
        if (reviewedAttemptEpoch != s.AttemptEpoch)
            return Rejected(g, new PlanError(StaleAttempt, $"Reviewed attempt epoch {reviewedAttemptEpoch}; current is {s.AttemptEpoch}.", nodeId));
        if (string.IsNullOrWhiteSpace(evidenceRef))
            return Rejected(g, new PlanError(EvidenceRequired, "A decision must reference its review evidence.", nodeId));
        if (decision == AcceptanceDecision.Accepted)
        {
            if (string.IsNullOrWhiteSpace(s.ArtifactRef))
                return Rejected(g, new PlanError(ArtifactRequired, "Accepted work must identify the exact artifact that was reviewed.", nodeId));
            var blockers = ix.BlockersFor(nodeId);
            if (!blockers.IsEmpty)
                return new OpResult(OpOutcome.Rejected, g, [new PlanError(GatesNotSatisfied, "Upstream gates no longer hold; cannot accept.", nodeId)], blockers);
        }

        var record = new AcceptanceRecord(decision, n.ContentRevision, s.ArtifactRef, s.AttemptId, s.AttemptEpoch, ctx.Actor, evidenceRef);
        if (s.Acceptance == record) return Unchanged(g);
        return Applied(Commit(g, nodeId, ctx, fingerprint, st => st with { Acceptance = record }));
    }

    /// <summary>
    /// A leaf's specification changed (requirements, acceptance criteria, inputs). Bumps
    /// ContentRevision, which makes any earlier acceptance Stale. Containers are refused
    /// until container-level review semantics exist (their acceptance is derived only).
    /// </summary>
    public static OpResult ReviseContent(PlanGraph g, Guid nodeId, OperationContext ctx) =>
        ReviseContentCore(g, nodeId, ctx, Fingerprint("revise_content", ctx.Actor), expectedContentRevision: null);

    /// <summary>
    /// ReviseContent bound to the actual new content: <paramref name="contentDigest"/> (see
    /// PlanContentDigest) is part of the operation fingerprint, so reusing a key with
    /// different content is operation_key_reused. Replay is recognised before the content
    /// revision check, so an exact retry is Unchanged rather than stale_content.
    /// </summary>
    public static OpResult ReviseContent(PlanGraph g, Guid nodeId, OperationContext ctx, string contentDigest, long expectedContentRevision) =>
        ReviseContentCore(g, nodeId, ctx, Fingerprint("revise_content", ctx.Actor, contentDigest, expectedContentRevision), expectedContentRevision);

    private static OpResult ReviseContentCore(PlanGraph g, Guid nodeId, OperationContext ctx, string fingerprint, long? expectedContentRevision)
    {
        if (Prologue(g, nodeId, ctx, fingerprint) is { } early) return early;
        if (expectedContentRevision is long expected && g.Nodes[nodeId].ContentRevision != expected)
            return Rejected(g, new PlanError(StaleContent, $"Expected content revision {expected}; current is {g.Nodes[nodeId].ContentRevision}.", nodeId));
        if (new Index(g).IsContainer(nodeId))
            return Rejected(g, new PlanError(ContainerRevisionUnsupported, "Revising a container's requirements is not supported in v1.", nodeId));

        var n = g.Nodes[nodeId];
        if (n.ContentRevision == long.MaxValue)
            return Rejected(g, new PlanError(RevisionExhausted, "The node's content revision counter is exhausted.", nodeId));
        var candidate = g with { Nodes = g.Nodes.SetItem(nodeId, n with { ContentRevision = n.ContentRevision + 1 }) };
        return Applied(Commit(candidate, nodeId, ctx, fingerprint, s => s));
    }

    // -----------------------------------------------------------------------
    // Internals
    // -----------------------------------------------------------------------

    private static readonly ImmutableHashSet<(WorkStatus, WorkStatus)> AllowedTransitions =
    [
        (WorkStatus.Todo, WorkStatus.InProgress),
        (WorkStatus.InProgress, WorkStatus.Done),
        (WorkStatus.InProgress, WorkStatus.Todo),
        (WorkStatus.Done, WorkStatus.InProgress),
        (WorkStatus.Todo, WorkStatus.Cancelled),
        (WorkStatus.InProgress, WorkStatus.Cancelled),
        (WorkStatus.Done, WorkStatus.Cancelled),
        (WorkStatus.Cancelled, WorkStatus.Todo),
    ];

    /// <summary>Unambiguous length-prefixed encoding of every semantically relevant field.</summary>
    private static string Fingerprint(params object?[] parts)
    {
        var sb = new StringBuilder();
        foreach (var p in parts)
        {
            if (p is null) { sb.Append("~;"); continue; }
            var s = p is IFormattable f ? f.ToString(null, System.Globalization.CultureInfo.InvariantCulture) : p.ToString()!;
            sb.Append(s.Length).Append(':').Append(s).Append(';');
        }
        return sb.ToString();
    }

    private static OpResult? Prologue(PlanGraph g, Guid nodeId, OperationContext ctx, string fingerprint)
    {
        var graphErrors = ValidateGraph(g);
        if (graphErrors.Length > 0)
            return Rejected(g, graphErrors.Insert(0, new PlanError(InvalidGraph, "The input plan is invalid; no operation applied.")));
        if (string.IsNullOrWhiteSpace(ctx.OperationKey))
            return Rejected(g, new PlanError(InvalidOperationKey, "An operation key is required.", nodeId));
        if (string.IsNullOrWhiteSpace(ctx.Actor))
            return Rejected(g, new PlanError(ActorRequired, "An actor is required.", nodeId));
        if (!g.Nodes.ContainsKey(nodeId))
            return Rejected(g, new PlanError(NodeNotFound, $"Node {nodeId} is not in the plan.", nodeId));

        var s = g.StateOf(nodeId);
        if (s.LastOperationKey == ctx.OperationKey)
        {
            return s.LastOperationFingerprint == fingerprint
                ? Unchanged(g)
                : Rejected(g, new PlanError(OperationKeyReused, "This operation key was already used for a different operation on this node.", nodeId));
        }
        if (ctx.ExpectedStateRevision != s.StateRevision)
            return Rejected(g, new PlanError(StaleRevision, $"Expected state revision {ctx.ExpectedStateRevision}; current is {s.StateRevision}.", nodeId));
        if (s.StateRevision == long.MaxValue)
            return Rejected(g, new PlanError(RevisionExhausted, "The node's state revision counter is exhausted.", nodeId));
        return null;
    }

    private static PlanGraph Commit(PlanGraph g, Guid nodeId, OperationContext ctx, string fingerprint, Func<NodeState, NodeState> change)
    {
        var current = g.StateOf(nodeId);
        var next = change(current) with
        {
            StateRevision = current.StateRevision + 1,
            LastOperationKey = ctx.OperationKey,
            LastOperationFingerprint = fingerprint,
        };
        return g with { States = g.States.SetItem(nodeId, next) };
    }

    private static OpResult Rejected(PlanGraph g, PlanError error) => new(OpOutcome.Rejected, g, [error], []);
    private static OpResult Rejected(PlanGraph g, ImmutableArray<PlanError> errors) => new(OpOutcome.Rejected, g, errors, []);
    private static OpResult Unchanged(PlanGraph g) => new(OpOutcome.Unchanged, g, [], []);
    private static OpResult Applied(PlanGraph g) => new(OpOutcome.Applied, g, [], []);

    private static bool IsAncestor(PlanGraph g, Guid candidateAncestor, Guid nodeId)
    {
        var current = g.Nodes[nodeId];
        while (current.Id != g.RootId && current.ParentId is Guid p)
        {
            if (p == candidateAncestor) return true;
            current = g.Nodes[p];
        }
        return false;
    }

    private static List<List<Guid>> FindHierarchyCycles(PlanGraph g)
    {
        var cycles = new List<List<Guid>>();
        var done = new HashSet<Guid>();
        foreach (var start in g.Nodes.Keys.OrderBy(k => k))
        {
            var path = new List<Guid>();
            var onPath = new HashSet<Guid>();
            var id = start;
            while (!done.Contains(id) && id != g.RootId && g.Nodes.TryGetValue(id, out var n))
            {
                if (!onPath.Add(id))
                {
                    cycles.Add(path.Skip(path.IndexOf(id)).ToList());
                    break;
                }
                path.Add(id);
                if (n.ParentId is not Guid p) break;
                id = p;
            }
            done.UnionWith(path);
        }
        return cycles;
    }

    /// <summary>Deterministic DFS; returns the first cycle as a path (first node repeated at the end).</summary>
    private static List<Guid>? FindCycle(IEnumerable<Guid> nodes, Func<Guid, IEnumerable<Guid>> next)
    {
        var state = new Dictionary<Guid, int>(); // 1 = on stack, 2 = finished
        var stack = new List<Guid>();
        List<Guid>? found = null;

        bool Visit(Guid id)
        {
            state[id] = 1;
            stack.Add(id);
            foreach (var m in next(id))
            {
                if (state.TryGetValue(m, out var st))
                {
                    if (st == 1) { found = [.. stack.Skip(stack.IndexOf(m)), m]; return true; }
                    continue;
                }
                if (Visit(m)) return true;
            }
            stack.RemoveAt(stack.Count - 1);
            state[id] = 2;
            return false;
        }

        foreach (var id in nodes)
        {
            if (!state.ContainsKey(id) && Visit(id)) return found;
        }
        return null;
    }

    /// <summary>
    /// Lookup structures and memoised derivations for a VALID graph (validation rules out
    /// cycles, so the mutual recursion between gates and roll-ups terminates). Built per
    /// call; never cached on the graph.
    /// </summary>
    private sealed class Index
    {
        private readonly PlanGraph _g;
        private readonly Dictionary<Guid, List<Guid>> _children = new();
        private readonly Dictionary<Guid, List<Dependency>> _declared = new();
        private readonly Dictionary<Guid, ContainerStatus> _containers = new();
        private readonly Dictionary<Guid, ImmutableArray<Blocker>> _blockers = new();

        public Index(PlanGraph g)
        {
            _g = g;
            foreach (var n in g.Nodes.Values)
            {
                if (n.Id == g.RootId || n.ParentId is not Guid p) continue;
                if (!_children.TryGetValue(p, out var list)) _children[p] = list = new();
                list.Add(n.Id);
            }
            foreach (var list in _children.Values)
            {
                list.Sort((a, b) =>
                {
                    var c = g.Nodes[a].SiblingOrder.CompareTo(g.Nodes[b].SiblingOrder);
                    return c != 0 ? c : a.CompareTo(b);
                });
            }
            foreach (var d in g.Dependencies)
            {
                if (!_declared.TryGetValue(d.SuccessorId, out var list)) _declared[d.SuccessorId] = list = new();
                list.Add(d);
            }
            foreach (var list in _declared.Values) list.Sort((a, b) => a.PredecessorId.CompareTo(b.PredecessorId));
            Ordered = g.Nodes.Keys.OrderBy(k => k).ToList();
        }

        public List<Guid> Ordered { get; }

        /// <summary>The root is always a container; any node with children is a container.</summary>
        public bool IsContainer(Guid id) => id == _g.RootId || (_children.TryGetValue(id, out var c) && c.Count > 0);

        public IEnumerable<Guid> ChildrenOf(Guid id) => _children.TryGetValue(id, out var c) ? c : [];

        public IEnumerable<Guid> PredecessorsDeclaredBy(Guid id) =>
            _declared.TryGetValue(id, out var deps) ? deps.Select(d => d.PredecessorId) : [];

        /// <summary>The node itself, then its ancestors nearest-first (root last).</summary>
        public IEnumerable<Guid> OwnersOf(Guid id)
        {
            yield return id;
            var current = _g.Nodes[id];
            while (current.Id != _g.RootId && current.ParentId is Guid p)
            {
                yield return p;
                current = _g.Nodes[p];
            }
        }

        public IEnumerable<Guid> HierarchyOrder()
        {
            var stack = new Stack<Guid>();
            stack.Push(_g.RootId);
            while (stack.Count > 0)
            {
                var id = stack.Pop();
                yield return id;
                foreach (var c in ChildrenOf(id).Reverse()) stack.Push(c);
            }
        }

        /// <summary>
        /// Roll-up over ALL children. A cancelled child keeps the container incomplete:
        /// there is no silent descope (an explicit descope operation is future work).
        /// </summary>
        public ContainerStatus Container(Guid id)
        {
            if (_containers.TryGetValue(id, out var cached)) return cached;
            int count = 0;
            bool allComplete = true, allAccepted = true, anyRejected = false, gatesHold = true;
            foreach (var c in ChildrenOf(id))
            {
                count++;
                if (IsContainer(c))
                {
                    var cs = Container(c);
                    allComplete &= cs.Completion == ContainerCompletion.Complete;
                    anyRejected |= cs.Acceptance == ContainerAcceptance.Rejected;
                    allAccepted &= cs.Acceptance == ContainerAcceptance.Accepted;
                    gatesHold &= cs.GatesHold;
                }
                else
                {
                    var s = _g.StateOf(c);
                    var leafGatesHold = BlockersFor(c).IsEmpty;
                    allComplete &= s.Work == WorkStatus.Done;
                    anyRejected |= IsCurrentRejection(s);
                    allAccepted &= EffectiveAcceptanceOf(_g, c) == EffectiveAcceptance.Accepted && leafGatesHold;
                    gatesHold &= leafGatesHold;
                }
            }
            var result = count == 0
                ? new ContainerStatus(id, ContainerCompletion.Empty, ContainerAcceptance.Empty, true)
                : new ContainerStatus(id,
                    allComplete ? ContainerCompletion.Complete : ContainerCompletion.Incomplete,
                    anyRejected ? ContainerAcceptance.Rejected
                        : allComplete && allAccepted ? ContainerAcceptance.Accepted
                        : ContainerAcceptance.Pending,
                    gatesHold);
            _containers[id] = result;
            return result;
        }

        /// <summary>Unsatisfied gates on the node and its ancestors: owner nearest-first, then predecessor id.</summary>
        public ImmutableArray<Blocker> BlockersFor(Guid nodeId)
        {
            if (_blockers.TryGetValue(nodeId, out var cached)) return cached;
            var blockers = ImmutableArray.CreateBuilder<Blocker>();
            foreach (var owner in OwnersOf(nodeId))
            {
                if (!_declared.TryGetValue(owner, out var deps)) continue;
                foreach (var d in deps)
                {
                    var gate = d.Gate ?? _g.DefaultGate;
                    if (GateFailure(d.PredecessorId, gate) is { } reason)
                        blockers.Add(new Blocker(owner, d.PredecessorId, gate, reason));
                }
            }
            var result = blockers.ToImmutable();
            _blockers[nodeId] = result;
            return result;
        }

        /// <summary>First failing reason in BlockerReasons precedence order, or null when satisfied.</summary>
        private string? GateFailure(Guid predecessorId, GatePolicy gate)
        {
            if (IsContainer(predecessorId))
            {
                var cs = Container(predecessorId);
                if (cs.Acceptance == ContainerAcceptance.Rejected) return BlockerReasons.PredecessorRejected;
                if (cs.Completion != ContainerCompletion.Complete) return BlockerReasons.PredecessorNotCompleted;
                if (!cs.GatesHold) return BlockerReasons.PredecessorUpstreamChanged;
                if (gate == GatePolicy.Accepted && cs.Acceptance != ContainerAcceptance.Accepted) return BlockerReasons.PredecessorNotAccepted;
                return null;
            }
            var s = _g.StateOf(predecessorId);
            if (s.Work == WorkStatus.Cancelled) return BlockerReasons.PredecessorCancelled;
            if (IsCurrentRejection(s)) return BlockerReasons.PredecessorRejected;
            if (s.Work != WorkStatus.Done) return BlockerReasons.PredecessorNotCompleted;
            if (!BlockersFor(predecessorId).IsEmpty) return BlockerReasons.PredecessorUpstreamChanged;
            if (gate == GatePolicy.Accepted)
            {
                var eff = EffectiveAcceptanceOf(_g, predecessorId);
                if (eff == EffectiveAcceptance.Stale) return BlockerReasons.PredecessorAcceptanceStale;
                if (eff != EffectiveAcceptance.Accepted) return BlockerReasons.PredecessorNotAccepted;
            }
            return null;
        }
    }
}
