using Xunit;
using static CodeStoragePoc.PlanContracts.PlanErrorCodes;
using static CodeStoragePoc.PlanContracts.Tests.Ops;
using static CodeStoragePoc.PlanContracts.Tests.PlanBuilder;

namespace CodeStoragePoc.PlanContracts.Tests;

public class ExecutorRefTests
{
    private static OpResult Start(PlanGraph g, string name, string attempt, string? executorRef) =>
        PlanRules.Transition(g, Id(name), WorkStatus.InProgress, Ctx(g, name), attempt, executorRef: executorRef);

    [Fact]
    public void Start_binds_finish_keeps_release_and_cancel_clear()
    {
        var g = Ok(Start(new PlanBuilder().Node("a").Build(), "a", "x1", "gods:engine_tasks:1"));
        Assert.Equal("gods:engine_tasks:1", g.StateOf(Id("a")).ExecutorRef);
        var done = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a"), "x1", 1, "sha"));
        Assert.Equal("gods:engine_tasks:1", done.StateOf(Id("a")).ExecutorRef);
        var released = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Todo, Ctx(g, "a"), "x1", 1));
        Assert.Null(released.StateOf(Id("a")).ExecutorRef);
        var cancelled = Ok(PlanRules.Transition(done, Id("a"), WorkStatus.Cancelled, Ctx(done, "a")));
        Assert.Null(cancelled.StateOf(Id("a")).ExecutorRef);
    }

    [Fact]
    public void Reopen_binds_a_new_reference_and_never_inherits()
    {
        var g = Ok(Start(new PlanBuilder().Node("a").Build(), "a", "x1", "run-1"));
        g = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a"), "x1", 1, "sha"));
        var reopened = Ok(Start(g, "a", "x2", null));
        Assert.Null(reopened.StateOf(Id("a")).ExecutorRef);
        var reopened2 = Ok(Start(g, "a", "x2", "run-2"));
        Assert.Equal("run-2", reopened2.StateOf(Id("a")).ExecutorRef);
    }

    [Fact]
    public void Finish_with_a_different_reference_is_a_stale_attempt()
    {
        var g = Ok(Start(new PlanBuilder().Node("a").Build(), "a", "x1", "run-1"));
        AssertRejected(PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a"), "x1", 1, "sha", "run-other"), g, StaleAttempt);
        Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a"), "x1", 1, "sha", "run-1"));
    }

    [Theory]
    [InlineData("")]
    [InlineData("has space")]
    [InlineData("tab\there")]
    [InlineData("é")]
    public void Malformed_references_are_rejected_before_anything_else(string bad)
    {
        var g = new PlanBuilder().Node("a").Build();
        AssertRejected(Start(g, "a", "x1", bad), g, InvalidExecutorRef);
        AssertRejected(Start(g, "a", "x1", new string('a', 257)), g, InvalidExecutorRef);
    }

    [Fact]
    public void Stored_references_are_validated()
    {
        var orphan = new PlanBuilder().Node("a").State("a", NodeState.Initial with { ExecutorRef = "run-1" }).Build();
        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(orphan)));
        var container = new PlanBuilder().Node("p", type: "plan_phase").Node("t", "p").State("p", NodeState.Initial with { ExecutorRef = "run-1" }).Build();
        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(container)));
        var badChars = new PlanBuilder().Node("a")
            .State("a", NodeState.Initial with { Work = WorkStatus.InProgress, AttemptId = "x", AttemptEpoch = 1, ExecutorRef = "a b" }).Build();
        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(badChars)));
    }

    [Fact]
    public void Reference_is_part_of_the_fingerprint()
    {
        var g = new PlanBuilder().Node("a").Build();
        var ctx = Ctx(g, "a", key: "start");
        var g1 = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.InProgress, ctx, "x1", executorRef: "run-1"));
        Assert.Equal(OpOutcome.Unchanged, PlanRules.Transition(g1, Id("a"), WorkStatus.InProgress, ctx, "x1", executorRef: "run-1").Outcome);
        AssertRejected(PlanRules.Transition(g1, Id("a"), WorkStatus.InProgress, ctx, "x1", executorRef: "run-2"), g1, OperationKeyReused);
        AssertRejected(PlanRules.Transition(g1, Id("a"), WorkStatus.InProgress, ctx, "x1"), g1, OperationKeyReused);
    }

    [Fact]
    public void A_2b1_stored_fingerprint_still_replays_as_unchanged_after_the_upgrade()
    {
        // Frozen fingerprint exactly as 2b1 stored it for this start operation.
        const string fp = "10:transition;6:tester;10:InProgress;2:x1;~;~;";
        var state = NodeState.Initial with
        {
            Work = WorkStatus.InProgress, AttemptId = "x1", AttemptEpoch = 1, StateRevision = 1,
            LastOperationKey = "start-2b1", LastOperationFingerprint = fp,
        };
        var g = new PlanBuilder().Node("a").State("a", state).Build();
        var retry = PlanRules.Transition(g, Id("a"), WorkStatus.InProgress, new OperationContext("start-2b1", 0, "tester"), "x1");
        Assert.Equal(OpOutcome.Unchanged, retry.Outcome);
        Assert.Same(g, retry.Graph);
        Assert.Empty(PlanAuditEvents.Derive(retry, g, Id("a"), AuditedOperation.Transition, new OperationContext("start-2b1", 0, "tester")));
    }
}

public class AuditEventDerivationTests
{
    private static AuditEvent Single(OpResult r, PlanGraph before, string name, AuditedOperation op, string? digest = null) =>
        Assert.Single(PlanAuditEvents.Derive(r, before, Id(name), op, new OperationContext("k", 0, "actor"), digest));

    [Fact]
    public void Every_transition_maps_to_one_event_with_the_attempt_as_it_was_during_the_operation()
    {
        var g0 = new PlanBuilder().Node("a").Build();
        var start = PlanRules.Transition(g0, Id("a"), WorkStatus.InProgress, Ctx(g0, "a"), "x1", executorRef: "run-1");
        var e = Single(start, g0, "a", AuditedOperation.Transition);
        Assert.Equal((AuditEventKind.AttemptStarted, WorkStatus.Todo, WorkStatus.InProgress, "x1", 1L, "run-1"),
            (e.Kind, e.WorkFrom, e.WorkTo, e.AttemptId, e.AttemptEpoch, e.ExecutorRef));
        Assert.Equal(start.Graph.StateOf(Id("a")).StateRevision, e.NodeStateRevision);

        var g1 = start.Graph;
        var release = PlanRules.Transition(g1, Id("a"), WorkStatus.Todo, Ctx(g1, "a"), "x1", 1);
        var rel = Single(release, g1, "a", AuditedOperation.Transition);
        Assert.Equal((AuditEventKind.AttemptReleased, "x1", 1L, "run-1"), (rel.Kind, rel.AttemptId, rel.AttemptEpoch, rel.ExecutorRef));   // pre-clear

        var cancel = PlanRules.Transition(g1, Id("a"), WorkStatus.Cancelled, Ctx(g1, "a"));
        var can = Single(cancel, g1, "a", AuditedOperation.Transition);
        Assert.Equal((AuditEventKind.AttemptCancelled, WorkStatus.InProgress, "x1", "run-1"), (can.Kind, can.WorkFrom, can.AttemptId, can.ExecutorRef));

        var restore = PlanRules.Transition(cancel.Graph, Id("a"), WorkStatus.Todo, Ctx(cancel.Graph, "a"));
        Assert.Equal(AuditEventKind.WorkRestored, Single(restore, cancel.Graph, "a", AuditedOperation.Transition).Kind);

        var finish = PlanRules.Transition(g1, Id("a"), WorkStatus.Done, Ctx(g1, "a"), "x1", 1, "sha-1");
        var fin = Single(finish, g1, "a", AuditedOperation.Transition);
        Assert.Equal((AuditEventKind.AttemptFinished, "sha-1", "run-1"), (fin.Kind, fin.ArtifactRef, fin.ExecutorRef));

        var reopen = PlanRules.Transition(finish.Graph, Id("a"), WorkStatus.InProgress, Ctx(finish.Graph, "a"), "x2", executorRef: "run-2");
        var reo = Single(reopen, finish.Graph, "a", AuditedOperation.Transition);
        Assert.Equal((AuditEventKind.AttemptReopened, "x2", 2L, "run-2"), (reo.Kind, reo.AttemptId, reo.AttemptEpoch, reo.ExecutorRef));
    }

    [Fact]
    public void Decisions_and_content_revisions_record_their_evidence()
    {
        var g = Finish(Start(new PlanBuilder().Node("a").Build(), "a", "x1"), "a", "sha-1");
        var decide = Decide(g, "a", AcceptanceDecision.Accepted);
        var d = Single(decide, g, "a", AuditedOperation.Decide);
        Assert.Equal((AuditEventKind.DecisionRecorded, AcceptanceDecision.Accepted, 1L, "evidence", "sha-1"),
            (d.Kind, d.Decision!.Value, d.ReviewedContentRevision!.Value, d.EvidenceRef, d.ArtifactRef));

        var g2 = new PlanBuilder().Node("b").Build();
        var revise = PlanRules.ReviseContent(g2, Id("b"), Ctx(g2, "b"), "DIGEST", 1);
        var c = Single(revise, g2, "b", AuditedOperation.ReviseContent, "DIGEST");
        Assert.Equal((AuditEventKind.ContentRevised, 2L, "DIGEST"), (c.Kind, c.ContentRevision, c.ContentDigest));
    }

    [Fact]
    public void Unchanged_rejected_and_structural_operations_produce_no_events()
    {
        var g = new PlanBuilder().Node("a").Node("b").Build();
        var ctx = Ctx(g, "a", key: "k1");
        var start = PlanRules.Transition(g, Id("a"), WorkStatus.InProgress, ctx, "x1");
        var replay = PlanRules.Transition(start.Graph, Id("a"), WorkStatus.InProgress, ctx, "x1");
        Assert.Empty(PlanAuditEvents.Derive(replay, start.Graph, Id("a"), AuditedOperation.Transition, ctx));
        var rejected = PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a"));
        Assert.Empty(PlanAuditEvents.Derive(rejected, g, Id("a"), AuditedOperation.Transition, ctx));
        var dep = PlanRules.AddDependency(g, Id("a"), Id("b"), null, Ctx(g, "b"));
        Assert.Equal(OpOutcome.Applied, dep.Outcome);
        Assert.Empty(PlanAuditEvents.Derive(dep, g, Id("b"), AuditedOperation.Other, ctx));
    }
}
