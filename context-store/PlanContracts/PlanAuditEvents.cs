// CodeStoragePoc - Plan node contracts v1: attempt provenance audit (plan 016)
//
// Pure derivation of the append-only audit record for one APPLIED operation from the
// before/after snapshots. Unchanged and Rejected results never produce events; dependency
// and structural operations produce none in this increment. The record is derived output,
// never read back by the rules.
//
// Depends on: PlanModel
// Used by:    PlanStore, PlanContracts.Tests

namespace CodeStoragePoc.PlanContracts;

public enum AuditedOperation { Transition, Decide, ReviseContent, Other }

public static class PlanAuditEvents
{
    /// <summary>0 or 1 events for an applied operation on <paramref name="nodeId"/>.</summary>
    public static IReadOnlyList<AuditEvent> Derive(
        OpResult result, PlanGraph before, Guid nodeId, AuditedOperation op, OperationContext ctx, string? contentDigest = null)
    {
        if (result.Outcome != OpOutcome.Applied || op == AuditedOperation.Other) return [];
        var after = result.Graph;
        var b = before.StateOf(nodeId);
        var a = after.StateOf(nodeId);
        var contentRevision = after.Nodes[nodeId].ContentRevision;

        AuditEvent Make(AuditEventKind kind, NodeState attempt, string? artifact = null, AcceptanceRecord? decision = null, string? digest = null) =>
            new(nodeId, a.StateRevision, kind, b.Work, a.Work, contentRevision,
                attempt.AttemptId, attempt.AttemptEpoch, attempt.ExecutorRef, artifact,
                decision?.Decision, decision?.ContentRevision, decision?.EvidenceRef, digest, ctx.Actor, ctx.OperationKey,
                attempt.AttemptContentRevision, attempt.AttemptPrereqDigest);

        switch (op)
        {
            case AuditedOperation.Decide:
                return [Make(AuditEventKind.DecisionRecorded, a, a.Acceptance?.ArtifactRef, a.Acceptance)];
            case AuditedOperation.ReviseContent:
                return [Make(AuditEventKind.ContentRevised, a, digest: contentDigest)];
        }

        // Transition: classify by (from, to). Release/cancel record the attempt (and its pins) BEFORE it was cleared.
        return (b.Work, a.Work) switch
        {
            (WorkStatus.Todo, WorkStatus.InProgress) => [Make(AuditEventKind.AttemptStarted, a)],
            (WorkStatus.Done, WorkStatus.InProgress) => [Make(AuditEventKind.AttemptReopened, a)],
            (WorkStatus.InProgress, WorkStatus.Done) => [Make(AuditEventKind.AttemptFinished, a, a.ArtifactRef)],
            (WorkStatus.InProgress, WorkStatus.Todo) => [Make(AuditEventKind.AttemptReleased, b)],
            (_, WorkStatus.Cancelled) => [Make(AuditEventKind.AttemptCancelled, b)],
            (WorkStatus.Cancelled, WorkStatus.Todo) => [Make(AuditEventKind.WorkRestored, a)],
            _ => [],
        };
    }
}
