// CodeStoragePoc - Plan node contracts v1: model
//
// Immutable, storage-agnostic snapshot of one plan: structural nodes (hierarchy),
// predecessor dependencies (separate from hierarchy), and per-leaf execution state.
// PlanRules validates and transforms snapshots; it never touches a database.
// See plans/012-plan-node-contracts-v1.md.
//
// Depends on: ContextRouter/NodeTypes
// Used by:    PlanRules, PlanContracts.Tests

using System.Collections.Immutable;
using CodeStoragePoc.ContextRouter;

namespace CodeStoragePoc.PlanContracts;

public static class PlanContract
{
    /// <summary>Contract version shared with ChatAgent and other consumers.</summary>
    public const string Version = "plan-contract/v1";
}

/// <summary>Work status, stored for leaf nodes only. Containers derive theirs.</summary>
public enum WorkStatus { Todo, InProgress, Done, Cancelled }

public enum AcceptanceDecision { Accepted, Rejected }

/// <summary>What a predecessor must reach before its successor may start.</summary>
public enum GatePolicy
{
    /// <summary>Predecessor work is done, no current rejection, and its own gates still hold.</summary>
    Completed,
    /// <summary>Predecessor work is done, effectively Accepted (exact basis), and its own gates still hold.</summary>
    Accepted,
}

/// <summary>Acceptance as it applies to the node's CURRENT content, artifact and attempt.</summary>
public enum EffectiveAcceptance { None, Accepted, Rejected, Stale }

public enum ContainerCompletion { Empty, Incomplete, Complete }

public enum ContainerAcceptance { Empty, Pending, Accepted, Rejected }

public enum OpOutcome { Applied, Unchanged, Rejected }

public static class PlanNodeTypes
{
    /// <summary>Existing node types that form plan structure. No new types are introduced.</summary>
    public static readonly ImmutableHashSet<string> Structural = ImmutableHashSet.Create(
        StringComparer.Ordinal,
        NodeTypes.Plan, NodeTypes.PlanPhase, NodeTypes.PlanStep, NodeTypes.Task, NodeTypes.Milestone);
}

/// <summary>
/// A structural plan node. ContentRevision (>= 1) is a counter the store increments when
/// the node's specification changes (requirements, acceptance criteria, inputs); it is
/// what an acceptance decision is bound to. Presentational edits do not change it.
/// </summary>
public sealed record PlanNode(Guid Id, Guid ProjectId, string NodeType, Guid? ParentId, int SiblingOrder, long ContentRevision);

/// <summary>
/// A reviewer decision bound to the exact content revision, artifact and attempt epoch
/// that was reviewed. If any of these later differs from the node's current values, or
/// the node is no longer Done, the decision is historical and not effective (Stale).
/// </summary>
public sealed record AcceptanceRecord(
    AcceptanceDecision Decision, long ContentRevision, string? ArtifactRef, string? AttemptId, long AttemptEpoch,
    string DecidedBy, string? EvidenceRef);

/// <summary>
/// State of a node, versioned by StateRevision (the compare-and-set token). Every applied
/// operation on the node increments StateRevision; only content revisions change
/// ContentRevision, so recording an acceptance never stales itself.
/// AttemptEpoch is a monotonic fencing token: each start issues epoch+1 and it is never
/// reset, so a released or cancelled attempt can never match a later one, even if the
/// caller reuses an attempt id.
/// </summary>
public sealed record NodeState(
    WorkStatus Work,
    long StateRevision,
    string? AttemptId,
    long AttemptEpoch,
    string? ArtifactRef,
    AcceptanceRecord? Acceptance,
    string? LastOperationKey,
    string? LastOperationFingerprint)
{
    public static NodeState Initial { get; } = new(WorkStatus.Todo, 0, null, 0, null, null, null, null);
}

/// <summary>Successor may not start until Predecessor satisfies the gate. Null gate = plan default.</summary>
public sealed record Dependency(Guid PredecessorId, Guid SuccessorId, GatePolicy? Gate = null);

public sealed record PlanGraph(
    Guid ProjectId,
    Guid RootId,
    GatePolicy DefaultGate,
    ImmutableDictionary<Guid, PlanNode> Nodes,
    ImmutableList<Dependency> Dependencies,
    ImmutableDictionary<Guid, NodeState> States)
{
    /// <summary>Build a snapshot from rows. Nodes without a state row start as NodeState.Initial.</summary>
    public static PlanGraph Create(
        Guid projectId, Guid rootId, IEnumerable<PlanNode> nodes, IEnumerable<Dependency>? dependencies = null,
        IEnumerable<KeyValuePair<Guid, NodeState>>? states = null, GatePolicy defaultGate = GatePolicy.Accepted)
    {
        // Duplicate node ids are a caller error, surfaced instead of silently overwritten.
        var nodeMap = ImmutableDictionary.CreateBuilder<Guid, PlanNode>();
        foreach (var n in nodes)
        {
            if (!nodeMap.TryAdd(n.Id, n)) throw new ArgumentException($"Duplicate plan node id {n.Id}.", nameof(nodes));
        }
        return new PlanGraph(
            projectId, rootId, defaultGate, nodeMap.ToImmutable(),
            (dependencies ?? []).ToImmutableList(),
            (states ?? []).ToImmutableDictionary());
    }

    public NodeState StateOf(Guid id) => States.TryGetValue(id, out var s) ? s : NodeState.Initial;
}

/// <summary>
/// Identifies one intended mutation. Keys are scoped per node and only the most recent
/// key per node is remembered (bounded memory): replaying the latest operation with the
/// same payload is a no-op; an older key is just a new operation subject to the revision
/// check. All identities (keys, attempts, actors) are inputs; PlanRules never generates them.
/// </summary>
public sealed record OperationContext(string OperationKey, long ExpectedStateRevision, string Actor);

public sealed record PlanError(string Code, string Message, Guid? NodeId = null, Guid? RelatedId = null);

/// <summary>Why a dependency is unsatisfied. Owner is the leaf or the ancestor that declared the dependency.</summary>
public sealed record Blocker(Guid DependencyOwnerId, Guid PredecessorId, GatePolicy Gate, string Reason);

/// <summary>
/// Result of a mutation. On Rejected, Graph is the unchanged input (no partial application);
/// on Unchanged (idempotent replay or identical decision) Graph is also the input.
/// </summary>
public sealed record OpResult(OpOutcome Outcome, PlanGraph Graph, ImmutableArray<PlanError> Errors, ImmutableArray<Blocker> Blockers)
{
    public bool Ok => Outcome != OpOutcome.Rejected;
}

/// <summary>
/// Readiness is derived, never stored. Gates are enforced when work starts and when it
/// is accepted; afterwards GatesHold keeps reporting them, so a started or finished node
/// whose upstream changed shows UpstreamChanged instead of being silently trusted.
/// </summary>
public sealed record LeafReadiness(Guid NodeId, WorkStatus Work, bool Ready, bool GatesHold, ImmutableArray<Blocker> Blockers)
{
    public bool UpstreamChanged => !GatesHold && Work is WorkStatus.InProgress or WorkStatus.Done;
}

/// <summary>Derived container status. GatesHold is false when any descendant leaf's gates no longer hold.</summary>
public sealed record ContainerStatus(Guid NodeId, ContainerCompletion Completion, ContainerAcceptance Acceptance, bool GatesHold);

/// <summary>Stable error codes: part of the contract surface. Documented in plans/012.</summary>
public static class PlanErrorCodes
{
    // Graph shape and contents
    public const string MissingRoot = "missing_root";
    public const string InvalidRootType = "invalid_root_type";
    public const string InvalidRootParent = "invalid_root_parent";
    public const string NodeKeyMismatch = "node_key_mismatch";
    public const string CrossProject = "cross_project";
    public const string InvalidNodeType = "invalid_node_type";
    public const string MissingParent = "missing_parent";
    public const string HierarchyCycle = "hierarchy_cycle";
    public const string OrphanState = "orphan_state";
    public const string InvalidRevision = "invalid_revision";
    public const string InvalidEnum = "invalid_enum";
    public const string InvalidState = "invalid_state";
    public const string MissingDependencyEndpoint = "missing_dependency_endpoint";
    public const string SelfDependency = "self_dependency";
    public const string DuplicateDependency = "duplicate_dependency";
    public const string DependencyOnAncestor = "dependency_on_ancestor";
    public const string DependencyCycle = "dependency_cycle";
    public const string HierarchyDependencyDeadlock = "hierarchy_dependency_deadlock";
    public const string InvalidGraph = "invalid_graph";

    // Operations
    public const string NodeNotFound = "node_not_found";
    public const string InvalidOperationKey = "invalid_operation_key";
    public const string ActorRequired = "actor_required";
    public const string OperationKeyReused = "operation_key_reused";
    public const string StaleRevision = "stale_revision";
    public const string RevisionExhausted = "revision_exhausted";
    public const string DependencyNotFound = "dependency_not_found";
    public const string ContainerStateIsDerived = "container_state_is_derived";
    public const string ContainerRevisionUnsupported = "container_revision_unsupported";
    public const string InvalidTransition = "invalid_transition";
    public const string AttemptRequired = "attempt_required";
    public const string StaleAttempt = "stale_attempt";
    public const string NotReady = "not_ready";
    public const string NotCompleted = "not_completed";
    public const string StaleContent = "stale_content";
    public const string StaleArtifact = "stale_artifact";
    public const string ArtifactRequired = "artifact_required";
    public const string EvidenceRequired = "evidence_required";
    public const string GatesNotSatisfied = "gates_not_satisfied";
    public const string InvalidChild = "invalid_child";
}

/// <summary>Stable blocker reasons, in the precedence order they are reported.</summary>
public static class BlockerReasons
{
    public const string PredecessorCancelled = "predecessor_cancelled";
    public const string PredecessorRejected = "predecessor_rejected";
    public const string PredecessorNotCompleted = "predecessor_not_completed";
    public const string PredecessorUpstreamChanged = "predecessor_upstream_changed";
    public const string PredecessorAcceptanceStale = "predecessor_acceptance_stale";
    public const string PredecessorNotAccepted = "predecessor_not_accepted";
}
