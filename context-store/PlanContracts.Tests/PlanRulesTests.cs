using Xunit;
using static CodeStoragePoc.PlanContracts.PlanErrorCodes;
using static CodeStoragePoc.PlanContracts.Tests.Ops;
using static CodeStoragePoc.PlanContracts.Tests.PlanBuilder;

namespace CodeStoragePoc.PlanContracts.Tests;

internal static class View
{
    public static LeafReadiness Leaf(PlanGraph g, string name) => PlanRules.Evaluate(g).Leaves.Single(l => l.NodeId == Id(name));
    public static ContainerStatus Container(PlanGraph g, string name) => PlanRules.Evaluate(g).Containers.Single(c => c.NodeId == Id(name));
}

public class GraphValidationTests
{
    [Fact]
    public void Valid_plan_has_no_errors_and_cross_container_dependencies_are_allowed()
    {
        var g = new PlanBuilder()
            .Node("phaseA", type: "plan_phase", order: 1).Node("a1", "phaseA")
            .Node("phaseB", type: "plan_phase", order: 2).Node("b1", "phaseB")
            .Dep("a1", "b1")            // leaf -> leaf across containers
            .Dep("phaseA", "phaseB")    // container -> container
            .Build();
        Assert.Empty(PlanRules.ValidateGraph(g));
    }

    [Fact]
    public void Dependency_on_own_ancestor_or_descendant_is_rejected()
    {
        var g = new PlanBuilder().Node("phase", type: "plan_phase").Node("t", "phase").Dep("phase", "t").Build();
        Assert.Contains(DependencyOnAncestor, Codes(PlanRules.ValidateGraph(g)));
        var g2 = new PlanBuilder().Node("phase", type: "plan_phase").Node("t", "phase").Dep("t", "phase").Build();
        Assert.Contains(DependencyOnAncestor, Codes(PlanRules.ValidateGraph(g2)));
    }

    [Fact]
    public void Direct_and_indirect_dependency_cycles_are_rejected()
    {
        var direct = new PlanBuilder().Node("a").Node("b").Dep("a", "b").Dep("b", "a").Build();
        Assert.Equal([DependencyCycle], Codes(PlanRules.ValidateGraph(direct)).ToArray());
        var indirect = new PlanBuilder().Node("a").Node("b").Node("c").Dep("a", "b").Dep("b", "c").Dep("c", "a").Build();
        Assert.Equal([DependencyCycle], Codes(PlanRules.ValidateGraph(indirect)).ToArray());
    }

    [Fact]
    public void Hierarchy_plus_dependency_deadlock_is_rejected()
    {
        // b waits for container x; x completes only when a completes; a waits for b.
        var g = new PlanBuilder().Node("x", type: "plan_phase").Node("a", "x").Node("b").Dep("x", "b").Dep("b", "a").Build();
        Assert.Equal([HierarchyDependencyDeadlock], Codes(PlanRules.ValidateGraph(g)).ToArray());
    }

    [Fact]
    public void Root_parent_must_be_outside_the_snapshot()
    {
        var a = Id("a");
        var nodes = new[] { new PlanNode(Root, Project, "plan", a, 0, 1), new PlanNode(a, Project, "task", Root, 0, 1) };
        Assert.Contains(InvalidRootParent, Codes(PlanRules.ValidateGraph(PlanGraph.Create(Project, Root, nodes))));
        var outside = new[] { new PlanNode(Root, Project, "plan", Id("project-node"), 0, 1) };
        Assert.Empty(PlanRules.ValidateGraph(PlanGraph.Create(Project, Root, outside)));
    }

    [Fact]
    public void Revisions_enums_and_state_shapes_are_validated()
    {
        var badRevision = new PlanBuilder().Node("a").Build();
        badRevision = badRevision with { Nodes = badRevision.Nodes.SetItem(Id("a"), badRevision.Nodes[Id("a")] with { ContentRevision = 0 }) };
        Assert.Contains(InvalidRevision, Codes(PlanRules.ValidateGraph(badRevision)));

        Assert.Contains(InvalidEnum, Codes(PlanRules.ValidateGraph(new PlanBuilder().Node("a").Build((GatePolicy)42))));
        Assert.Contains(InvalidEnum, Codes(PlanRules.ValidateGraph(new PlanBuilder().Node("a").Node("b").Dep("a", "b", (GatePolicy)42).Build())));
        Assert.Contains(InvalidEnum, Codes(PlanRules.ValidateGraph(new PlanBuilder().Node("a").State("a", NodeState.Initial with { Work = (WorkStatus)9 }).Build())));
        var badDecision = NodeState.Initial with
        {
            Work = WorkStatus.Done, AttemptId = "x", AttemptEpoch = 1,
            Acceptance = new AcceptanceRecord((AcceptanceDecision)7, 1, null, "x", 1, "r", "e"),
        };
        Assert.Contains(InvalidEnum, Codes(PlanRules.ValidateGraph(new PlanBuilder().Node("a").State("a", badDecision).Build())));

        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(new PlanBuilder().Node("a").State("a", NodeState.Initial with { Work = WorkStatus.InProgress }).Build())));
        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(new PlanBuilder().Node("a").State("a", NodeState.Initial with { Work = WorkStatus.Done }).Build())));
        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(new PlanBuilder().Node("a").State("a", NodeState.Initial with { AttemptId = "x" }).Build())));
        // Containers never hold work state.
        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(new PlanBuilder().Node("p", type: "plan_phase").Node("t", "p")
            .State("p", NodeState.Initial with { Work = WorkStatus.Done, AttemptId = "x", AttemptEpoch = 1 }).Build())));
    }

    public static TheoryData<string, NodeState> SmuggledDecisions => new()
    {
        { "accepted without artifact, decider or evidence", Done("x") with { Acceptance = new(AcceptanceDecision.Accepted, 1, null, null, 1, "", null) } },
        { "accepted without evidence", Done("x") with { Acceptance = new(AcceptanceDecision.Accepted, 1, "art", "x", 1, "r", " ") } },
        { "accepted without decider", Done("x") with { Acceptance = new(AcceptanceDecision.Accepted, 1, "art", "x", 1, "", "e") } },
        { "accepted without artifact", Done("x", artifact: null) with { Acceptance = new(AcceptanceDecision.Accepted, 1, null, "x", 1, "r", "e") } },
        { "current-epoch decision naming another attempt", Done("x") with { Acceptance = new(AcceptanceDecision.Accepted, 1, "art", "y", 1, "r", "e") } },
        { "rejected without evidence", Done("x") with { Acceptance = new(AcceptanceDecision.Rejected, 1, "art", "x", 1, "r", null) } },
        { "done without attempt id", Done(null) },
    };

    private static NodeState Done(string? attemptId, string? artifact = "art") =>
        NodeState.Initial with { Work = WorkStatus.Done, AttemptId = attemptId, AttemptEpoch = 1, ArtifactRef = artifact };

    [Theory]
    [MemberData(nameof(SmuggledDecisions))]
    public void Loaded_snapshots_cannot_smuggle_in_decisions_that_Decide_would_refuse(string _, NodeState state)
    {
        var g = new PlanBuilder().Node("a").Node("b").Dep("a", "b").State("a", state).Build();
        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(g)));
        var view = PlanRules.Evaluate(g);
        Assert.Empty(view.Leaves);   // no successor can be reported ready from an invalid snapshot
        AssertRejected(PlanRules.Transition(g, Id("b"), WorkStatus.InProgress, Ctx(g, "b"), "y"), g, InvalidGraph);
    }

    [Fact]
    public void A_valid_loaded_decision_is_honoured()
    {
        var state = Done("x") with { Acceptance = new(AcceptanceDecision.Accepted, 1, "art", "x", 1, "r", "e") };
        var g = new PlanBuilder().Node("a").Node("b").Dep("a", "b").State("a", state).Build();
        Assert.Empty(PlanRules.ValidateGraph(g));
        Assert.True(View.Leaf(g, "b").Ready);
    }

    [Fact]
    public void Errors_are_deterministic_regardless_of_insertion_order()
    {
        var one = new PlanBuilder().Node("a", parent: "ghost1").Node("b", parent: "ghost2").Node("c", type: "risk").Build();
        var two = new PlanBuilder().Node("c", type: "risk").Node("b", parent: "ghost2").Node("a", parent: "ghost1").Build();
        Assert.Equal(PlanRules.ValidateGraph(one).ToArray(), PlanRules.ValidateGraph(two).ToArray());
        Assert.Equal(3, PlanRules.ValidateGraph(one).Length);
    }

    [Fact]
    public void Invalid_graph_reads_return_errors_and_no_derived_data()
    {
        var view = PlanRules.Evaluate(new PlanBuilder().Node("a", parent: "ghost").Build());
        Assert.Contains(MissingParent, Codes(view.Errors));
        Assert.Empty(view.Leaves);
        Assert.Empty(view.Containers);
    }

    [Fact]
    public void Duplicate_node_ids_in_rows_are_refused_at_snapshot_creation()
    {
        var n = new PlanNode(Id("a"), Project, "task", Root, 0, 1);
        Assert.Throws<ArgumentException>(() => PlanGraph.Create(Project, Root, [new PlanNode(Root, Project, "plan", null, 0, 1), n, n]));
    }
}

public class OperationTests
{
    [Fact]
    public void Rejected_operations_return_the_input_graph_by_reference_and_never_mutate_it()
    {
        var g = new PlanBuilder().Node("a").Node("b").Dep("a", "b").Build();
        var nodesBefore = g.Nodes.ToArray();
        var depsBefore = g.Dependencies.ToArray();
        var statesBefore = g.States.ToArray();

        AssertRejected(PlanRules.AddDependency(g, Id("b"), Id("a"), null, Ctx(g, "a")), g, DependencyCycle);

        Assert.Equal(nodesBefore, g.Nodes.ToArray());
        Assert.Equal(depsBefore, g.Dependencies.ToArray());
        Assert.Equal(statesBefore, g.States.ToArray());
    }

    [Fact]
    public void Applied_operations_leave_the_input_untouched()
    {
        var g = new PlanBuilder().Node("a").Node("b").Build();
        var next = Ok(PlanRules.AddDependency(g, Id("a"), Id("b"), null, Ctx(g, "b")));
        Assert.Empty(g.Dependencies);
        Assert.Empty(g.States);
        Assert.Single(next.Dependencies);
        Assert.Equal(1, next.StateOf(Id("b")).StateRevision);
        Assert.Equal(0, next.StateOf(Id("a")).StateRevision); // only the successor's revision moves
    }

    [Fact]
    public void Stale_state_revision_is_rejected()
    {
        var g = Start(new PlanBuilder().Node("a").Build(), "a", "x1");
        AssertRejected(PlanRules.Transition(g, Id("a"), WorkStatus.Cancelled, new OperationContext("k", 0, "tester")), g, StaleRevision);
    }

    [Fact]
    public void Replaying_the_latest_operation_key_is_unchanged_and_reusing_it_differently_is_rejected()
    {
        var g = new PlanBuilder().Node("a").Build();
        var ctx = Ctx(g, "a", key: "start-1");
        var g1 = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.InProgress, ctx, "x1"));

        var replay = PlanRules.Transition(g1, Id("a"), WorkStatus.InProgress, ctx, "x1"); // retry with the old revision
        Assert.Equal(OpOutcome.Unchanged, replay.Outcome);
        Assert.Same(g1, replay.Graph);

        AssertRejected(PlanRules.Transition(g1, Id("a"), WorkStatus.Cancelled, ctx), g1, OperationKeyReused);
    }

    [Fact]
    public void Fingerprints_are_unambiguous_and_cover_actor_and_evidence()
    {
        var g = Start(new PlanBuilder().Node("a").Build(), "a", "x:y");
        var ctx = Ctx(g, "a", key: "finish");
        var done = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Done, ctx, "x:y", 1, "z"));
        // Same concatenated text "x:y:z" split differently must not look like a replay.
        AssertRejected(PlanRules.Transition(done, Id("a"), WorkStatus.Done, ctx, "x", 1, "y:z"), done, OperationKeyReused);
        // A different actor with the same key is a different operation.
        AssertRejected(PlanRules.Transition(done, Id("a"), WorkStatus.Done, ctx with { Actor = "someone-else" }, "x:y", 1, "z"), done, OperationKeyReused);

        var decided = Ok(Decide(done, "a", AcceptanceDecision.Accepted, key: "review"));
        AssertRejected(Decide(decided, "a", AcceptanceDecision.Accepted, key: "review", evidence: "other-evidence"), decided, OperationKeyReused);
    }

    [Fact]
    public void Exhausted_counters_are_rejected_without_changing_the_input()
    {
        var stateMax = new PlanBuilder().Node("a").State("a", NodeState.Initial with { StateRevision = long.MaxValue }).Build();
        AssertRejected(PlanRules.Transition(stateMax, Id("a"), WorkStatus.Cancelled, Ctx(stateMax, "a")), stateMax, RevisionExhausted);

        var epochMax = new PlanBuilder().Node("a").State("a", NodeState.Initial with { AttemptEpoch = long.MaxValue }).Build();
        AssertRejected(PlanRules.Transition(epochMax, Id("a"), WorkStatus.InProgress, Ctx(epochMax, "a"), "x"), epochMax, RevisionExhausted);

        var contentMax = new PlanBuilder().Node("a").Build();
        contentMax = contentMax with { Nodes = contentMax.Nodes.SetItem(Id("a"), contentMax.Nodes[Id("a")] with { ContentRevision = long.MaxValue }) };
        AssertRejected(PlanRules.ReviseContent(contentMax, Id("a"), Ctx(contentMax, "a")), contentMax, RevisionExhausted);

        // One below the limit still works and lands exactly on it.
        var nearMax = new PlanBuilder().Node("a").State("a", NodeState.Initial with { StateRevision = long.MaxValue - 1 }).Build();
        Assert.Equal(long.MaxValue, Ok(PlanRules.Transition(nearMax, Id("a"), WorkStatus.Cancelled, Ctx(nearMax, "a"))).StateOf(Id("a")).StateRevision);

        var epochNear = new PlanBuilder().Node("a").State("a", NodeState.Initial with { AttemptEpoch = long.MaxValue - 1 }).Build();
        Assert.Equal(long.MaxValue, Start(epochNear, "a", "x").StateOf(Id("a")).AttemptEpoch);

        var contentNear = new PlanBuilder().Node("a").Build();
        contentNear = contentNear with { Nodes = contentNear.Nodes.SetItem(Id("a"), contentNear.Nodes[Id("a")] with { ContentRevision = long.MaxValue - 1 }) };
        Assert.Equal(long.MaxValue, Ok(PlanRules.ReviseContent(contentNear, Id("a"), Ctx(contentNear, "a"))).Nodes[Id("a")].ContentRevision);
    }

    [Fact]
    public void Operation_keys_are_scoped_per_node()
    {
        var g = new PlanBuilder().Node("a").Node("b").Build();
        var g1 = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.InProgress, Ctx(g, "a", key: "same"), "x1"));
        var g2 = Ok(PlanRules.Transition(g1, Id("b"), WorkStatus.InProgress, Ctx(g1, "b", key: "same"), "y1"));
        Assert.Equal(WorkStatus.InProgress, g2.StateOf(Id("b")).Work);
    }

    [Fact]
    public void Remove_dependency_unblocks_and_missing_dependency_is_reported()
    {
        var g = new PlanBuilder().Node("a").Node("b").Dep("a", "b").Build();
        Assert.False(View.Leaf(g, "b").Ready);
        var g1 = Ok(PlanRules.RemoveDependency(g, Id("a"), Id("b"), Ctx(g, "b")));
        Assert.True(View.Leaf(g1, "b").Ready);
        AssertRejected(PlanRules.RemoveDependency(g1, Id("a"), Id("b"), Ctx(g1, "b")), g1, DependencyNotFound);
    }

    [Fact]
    public void Unknown_enum_inputs_are_rejected_before_anything_else()
    {
        var g = Finish(Start(new PlanBuilder().Node("a").Node("b").Build(), "a", "x"), "a");
        AssertRejected(PlanRules.Transition(g, Id("b"), (WorkStatus)9, Ctx(g, "b"), "y"), g, InvalidEnum);
        AssertRejected(PlanRules.AddDependency(g, Id("a"), Id("b"), (GatePolicy)9, Ctx(g, "b")), g, InvalidEnum);
        AssertRejected(PlanRules.Decide(g, Id("a"), (AcceptanceDecision)9, 1, "a-artifact", 1, "e", Ctx(g, "a")), g, InvalidEnum);
    }
}

public class TransitionTests
{
    public static TheoryData<WorkStatus, WorkStatus> Disallowed => new()
    {
        { WorkStatus.Todo, WorkStatus.Done },
        { WorkStatus.Todo, WorkStatus.Todo },
        { WorkStatus.Done, WorkStatus.Todo },
        { WorkStatus.Done, WorkStatus.Done },
        { WorkStatus.Cancelled, WorkStatus.InProgress },
        { WorkStatus.Cancelled, WorkStatus.Done },
        { WorkStatus.Cancelled, WorkStatus.Cancelled },
        { WorkStatus.InProgress, WorkStatus.InProgress },
    };

    [Theory]
    [MemberData(nameof(Disallowed))]
    public void Disallowed_transitions_are_rejected(WorkStatus from, WorkStatus to)
    {
        var state = from switch
        {
            WorkStatus.InProgress => NodeState.Initial with { Work = from, AttemptId = "x1", AttemptEpoch = 1 },
            WorkStatus.Done => NodeState.Initial with { Work = from, AttemptId = "x1", AttemptEpoch = 1 },
            _ => NodeState.Initial with { Work = from },
        };
        var g = new PlanBuilder().Node("a").State("a", state).Build();
        AssertRejected(PlanRules.Transition(g, Id("a"), to, Ctx(g, "a"), "x1", 1, "art"), g, InvalidTransition);
    }

    [Fact]
    public void Finishing_requires_the_current_attempt_and_cancel_fences_a_late_worker()
    {
        var g = Start(new PlanBuilder().Node("a").Build(), "a", "x1");
        AssertRejected(PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a"), "other", 1), g, StaleAttempt);
        AssertRejected(PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a"), "x1", 99), g, StaleAttempt);

        var cancelled = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Cancelled, Ctx(g, "a")));
        Assert.Null(cancelled.StateOf(Id("a")).AttemptId);
        AssertRejected(PlanRules.Transition(cancelled, Id("a"), WorkStatus.Done, Ctx(cancelled, "a"), "x1", 1), cancelled, InvalidTransition);
    }

    [Fact]
    public void A_released_attempt_cannot_complete_a_later_run_even_with_the_same_id()
    {
        var g = Start(new PlanBuilder().Node("a").Build(), "a", "x1");                       // epoch 1
        g = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Todo, Ctx(g, "a"), "x1", 1));        // release
        Assert.Null(g.StateOf(Id("a")).AttemptId);
        g = Start(g, "a", "x1");                                                                 // same id, epoch 2
        Assert.Equal(2, g.StateOf(Id("a")).AttemptEpoch);
        AssertRejected(PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a"), "x1", 1, "late"), g, StaleAttempt);
        Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a"), "x1", 2, "fresh"));
    }

    [Fact]
    public void Reopen_issues_a_new_epoch_and_makes_the_old_acceptance_historical()
    {
        var g = Complete(new PlanBuilder().Node("a").Build(), "a", "x1");
        Assert.Equal(EffectiveAcceptance.Accepted, PlanRules.EffectiveAcceptanceOf(g, Id("a")));

        var reopened = Start(g, "a", "x1");
        Assert.NotNull(reopened.StateOf(Id("a")).Acceptance);   // record kept
        Assert.Equal(EffectiveAcceptance.Stale, PlanRules.EffectiveAcceptanceOf(reopened, Id("a")));

        // Same id and same artifact still do not revive the old approval: the epoch moved.
        var redone = Finish(reopened, "a", "a-artifact");
        Assert.Equal(EffectiveAcceptance.Stale, PlanRules.EffectiveAcceptanceOf(redone, Id("a")));
    }

    [Fact]
    public void Cancelled_work_can_be_restored_to_todo()
    {
        var g = new PlanBuilder().Node("a").Build();
        var c = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Cancelled, Ctx(g, "a")));
        var restored = Ok(PlanRules.Transition(c, Id("a"), WorkStatus.Todo, Ctx(c, "a")));
        Assert.Equal(WorkStatus.Todo, restored.StateOf(Id("a")).Work);
    }

    [Fact]
    public void Starting_is_gated_with_deterministic_blockers()
    {
        var g = new PlanBuilder()
            .Node("phase", type: "plan_phase").Node("t", "phase")
            .Node("p1").Node("p2").Node("p3")
            .Dep("p2", "t").Dep("p1", "t").Dep("p3", "phase")
            .Build();
        var r = PlanRules.Transition(g, Id("t"), WorkStatus.InProgress, Ctx(g, "t"), "x1");
        AssertRejected(r, g, NotReady);
        // Own dependencies first (by predecessor id), then the ancestor's.
        var ownFirst = new[] { Id("p1"), Id("p2") }.OrderBy(x => x).ToArray();
        Assert.Equal([.. ownFirst, Id("p3")], r.Blockers.Select(b => b.PredecessorId).ToArray());
        Assert.Equal([Id("t"), Id("t"), Id("phase")], r.Blockers.Select(b => b.DependencyOwnerId).ToArray());
        Assert.All(r.Blockers, b => Assert.Equal(BlockerReasons.PredecessorNotCompleted, b.Reason));
    }

    [Fact]
    public void Containers_have_no_stored_work_status_acceptance_or_revisable_content()
    {
        var g = new PlanBuilder().Node("phase", type: "plan_phase").Node("t", "phase").Build();
        AssertRejected(PlanRules.Transition(g, Id("phase"), WorkStatus.InProgress, Ctx(g, "phase"), "x1"), g, ContainerStateIsDerived);
        AssertRejected(PlanRules.Transition(g, Root, WorkStatus.Cancelled, Ctx(g, "root")), g, ContainerStateIsDerived);
        AssertRejected(PlanRules.Decide(g, Id("phase"), AcceptanceDecision.Accepted, 1, null, 0, "e", Ctx(g, "phase")), g, ContainerStateIsDerived);
        AssertRejected(PlanRules.ReviseContent(g, Id("phase"), Ctx(g, "phase")), g, ContainerRevisionUnsupported);
    }
}

public class GateAndAcceptanceTests
{
    [Fact]
    public void Default_gate_is_accepted_so_done_but_unreviewed_does_not_satisfy_it()
    {
        var g = Finish(Start(new PlanBuilder().Node("a").Node("b").Dep("a", "b").Build(), "a", "x1"), "a");
        Assert.Equal(BlockerReasons.PredecessorNotAccepted, View.Leaf(g, "b").Blockers.Single().Reason);
        Assert.True(View.Leaf(Accept(g, "a"), "b").Ready);
    }

    [Fact]
    public void Completed_gate_is_explicit_per_edge_or_per_plan()
    {
        var perEdge = Finish(Start(new PlanBuilder().Node("a").Node("b").Dep("a", "b", GatePolicy.Completed).Build(), "a", "x1"), "a");
        Assert.True(View.Leaf(perEdge, "b").Ready);
        var perPlan = Finish(Start(new PlanBuilder().Node("a").Node("b").Dep("a", "b").Build(GatePolicy.Completed), "a", "x1"), "a");
        Assert.True(View.Leaf(perPlan, "b").Ready);
    }

    [Fact]
    public void Rejection_blocks_even_a_completed_gate_and_survives_a_content_revision()
    {
        var g = Finish(Start(new PlanBuilder().Node("a").Node("b").Dep("a", "b", GatePolicy.Completed).Build(), "a", "x1"), "a");
        g = Ok(Decide(g, "a", AcceptanceDecision.Rejected));
        Assert.Equal(BlockerReasons.PredecessorRejected, View.Leaf(g, "b").Blockers.Single().Reason);

        // Revising requirements does not un-reject the same work (T7).
        g = Ok(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a")));
        Assert.Equal(EffectiveAcceptance.Stale, PlanRules.EffectiveAcceptanceOf(g, Id("a")));
        Assert.Equal(BlockerReasons.PredecessorRejected, View.Leaf(g, "b").Blockers.Single().Reason);

        // New work (a new attempt) is what lifts it.
        g = Finish(Start(g, "a", "x2"), "a", "a-artifact-2");
        Assert.True(View.Leaf(g, "b").Ready);
    }

    [Fact]
    public void Cancelled_predecessor_never_satisfies_a_gate()
    {
        var g = new PlanBuilder().Node("a").Node("b").Dep("a", "b", GatePolicy.Completed).Build();
        var c = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Cancelled, Ctx(g, "a")));
        Assert.Equal(BlockerReasons.PredecessorCancelled, View.Leaf(c, "b").Blockers.Single().Reason);
    }

    [Fact]
    public void Accepting_stamps_content_artifact_and_epoch_and_does_not_stale_itself()
    {
        var g = Complete(new PlanBuilder().Node("a").Build(), "a", "x1");
        Assert.Equal(new AcceptanceRecord(AcceptanceDecision.Accepted, 1, "a-artifact", "x1", 1, "reviewer", "evidence"), g.StateOf(Id("a")).Acceptance);
        Assert.Equal(1, g.Nodes[Id("a")].ContentRevision);   // state moved, content did not
        Assert.Equal(EffectiveAcceptance.Accepted, PlanRules.EffectiveAcceptanceOf(g, Id("a")));
    }

    [Fact]
    public void Content_revision_makes_acceptance_stale_and_stale_never_satisfies_an_accepted_gate()
    {
        var g = Complete(new PlanBuilder().Node("a").Node("b").Dep("a", "b").Build(), "a");
        var revised = Ok(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a")));
        Assert.Equal(EffectiveAcceptance.Stale, PlanRules.EffectiveAcceptanceOf(revised, Id("a")));
        Assert.Equal(BlockerReasons.PredecessorAcceptanceStale, View.Leaf(revised, "b").Blockers.Single().Reason);
    }

    [Fact]
    public void Adding_a_dependency_stales_a_pinned_acceptance_even_when_the_new_gate_is_green()
    {
        // 3b1 (plan 019 §1b): an added prerequisite is a changed input, so a pinned acceptance is Stale.
        var g = Complete(new PlanBuilder().Node("a").Node("c").Build(), "a");
        g = Complete(g, "c");
        var withDep = Ok(PlanRules.AddDependency(g, Id("c"), Id("a"), null, Ctx(g, "a")));
        Assert.True(View.Leaf(withDep, "a").GatesHold);
        Assert.Equal(EffectiveAcceptance.Stale, PlanRules.EffectiveAcceptanceOf(withDep, Id("a")));
        Assert.Equal(AcceptanceDecision.Accepted, withDep.StateOf(Id("a")).Acceptance!.Decision);   // record not rewritten
    }

    [Fact]
    public void Unrelated_state_operations_do_not_stale_acceptance()
    {
        var g = Complete(new PlanBuilder().Node("a").Node("c").Build(), "a");
        g = Complete(g, "c");                                         // sibling start/finish/accept
        g = Ok(PlanRules.ReviseContent(g, Id("c"), Ctx(g, "c")));     // sibling content
        Assert.Equal(EffectiveAcceptance.Accepted, PlanRules.EffectiveAcceptanceOf(g, Id("a")));
    }

    [Fact]
    public void Decisions_must_name_the_exact_reviewed_content_artifact_and_epoch()
    {
        var g = Finish(Start(new PlanBuilder().Node("a").Build(), "a", "x1"), "a", "sha-1");
        AssertRejected(PlanRules.Decide(g, Id("a"), AcceptanceDecision.Accepted, 0, "sha-1", 1, "e", Ctx(g, "a")), g, StaleContent);
        AssertRejected(PlanRules.Decide(g, Id("a"), AcceptanceDecision.Accepted, 1, "sha-0", 1, "e", Ctx(g, "a")), g, StaleArtifact);
        AssertRejected(PlanRules.Decide(g, Id("a"), AcceptanceDecision.Accepted, 1, "sha-1", 0, "e", Ctx(g, "a")), g, StaleAttempt);
    }

    [Fact]
    public void Decisions_need_evidence_and_acceptance_needs_an_artifact()
    {
        var g = Finish(Start(new PlanBuilder().Node("a").Build(), "a", "x1"), "a");
        AssertRejected(Decide(g, "a", AcceptanceDecision.Rejected, evidence: null), g, EvidenceRequired);

        var noArtifact = Ok(FinishResult(Start(new PlanBuilder().Node("a").Build(), "a", "x1"), "a", artifact: null));
        AssertRejected(Decide(noArtifact, "a", AcceptanceDecision.Accepted), noArtifact, ArtifactRequired);
        Ok(Decide(noArtifact, "a", AcceptanceDecision.Rejected)); // rejecting metadata-only work is possible
    }

    [Fact]
    public void Deciding_before_completion_is_rejected()
    {
        var g = Start(new PlanBuilder().Node("a").Build(), "a", "x1");
        AssertRejected(Decide(g, "a", AcceptanceDecision.Accepted), g, NotCompleted);
    }

    [Fact]
    public void Repeated_identical_decision_is_unchanged_and_a_reversal_applies()
    {
        var g = Complete(new PlanBuilder().Node("a").Build(), "a");
        var again = Decide(g, "a", AcceptanceDecision.Accepted);
        Assert.Equal(OpOutcome.Unchanged, again.Outcome);
        Assert.Same(g, again.Graph);

        var reversed = Ok(Decide(g, "a", AcceptanceDecision.Rejected));
        Assert.Equal(EffectiveAcceptance.Rejected, PlanRules.EffectiveAcceptanceOf(reversed, Id("a")));
        Assert.Equal(g.StateOf(Id("a")).StateRevision + 1, reversed.StateOf(Id("a")).StateRevision);
    }

    [Fact]
    public void Upstream_change_after_start_is_reported_and_rejects_the_finish()
    {
        // B depends on A (Accepted gate). B starts, then A's content is revised.
        var g = Complete(new PlanBuilder().Node("a").Node("b").Dep("a", "b").Build(), "a");
        g = Start(g, "b", "y1");
        g = Ok(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a")));

        var b = View.Leaf(g, "b");
        Assert.Equal(WorkStatus.InProgress, b.Work);   // not auto-cancelled
        Assert.True(b.UpstreamChanged);

        // 3b1 (plan 019 §1b): the finish itself is now rejected; release stays possible.
        AssertRejected(FinishResult(g, "b", "b-artifact"), g, StalePrerequisites);
        var s = g.StateOf(Id("b"));
        Ok(PlanRules.Transition(g, Id("b"), WorkStatus.Todo, Ctx(g, "b"), s.AttemptId, s.AttemptEpoch));
    }

    [Fact]
    public void Upstream_change_after_finish_blocks_acceptance()
    {
        var g = Complete(new PlanBuilder().Node("a").Node("b").Dep("a", "b").Build(), "a");
        g = Finish(Start(g, "b", "y1"), "b");
        g = Ok(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a")));

        AssertRejected(Decide(g, "b", AcceptanceDecision.Accepted), g, GatesNotSatisfied);   // keeps precedence over pins
        Assert.Equal(OpOutcome.Applied, Decide(g, "b", AcceptanceDecision.Rejected).Outcome); // rejecting stays possible
    }

    [Fact]
    public void Staleness_is_transitive_through_leaves()
    {
        // T1: A -> B -> C -> D, all accepted; reopening A must block everything downstream.
        var g = new PlanBuilder().Node("a").Node("b").Node("c").Node("d").Dep("a", "b").Dep("b", "c").Dep("c", "d").Build();
        g = Complete(Complete(Complete(g, "a"), "b"), "c");
        Assert.True(View.Leaf(g, "d").Ready);

        g = Start(g, "a", "x2"); // reopen A
        Assert.Equal(BlockerReasons.PredecessorNotCompleted, View.Leaf(g, "b").Blockers.Single().Reason);
        Assert.Equal(BlockerReasons.PredecessorUpstreamChanged, View.Leaf(g, "c").Blockers.Single().Reason);
        Assert.True(View.Leaf(g, "c").UpstreamChanged);
        Assert.Equal(BlockerReasons.PredecessorUpstreamChanged, View.Leaf(g, "d").Blockers.Single().Reason);
        Assert.False(View.Leaf(g, "d").Ready);
        AssertRejected(Decide(g, "c", AcceptanceDecision.Accepted), g, GatesNotSatisfied);
    }

    [Fact]
    public void Adding_an_unsatisfied_dependency_to_accepted_work_invalidates_downstream()
    {
        // T5
        var g = new PlanBuilder().Node("b").Node("c").Node("x").Dep("b", "c").Build();
        g = Complete(g, "b");
        Assert.True(View.Leaf(g, "c").Ready);
        g = Ok(PlanRules.AddDependency(g, Id("x"), Id("b"), null, Ctx(g, "b")));
        Assert.True(View.Leaf(g, "b").UpstreamChanged);
        Assert.Equal(BlockerReasons.PredecessorUpstreamChanged, View.Leaf(g, "c").Blockers.Single().Reason);
    }
}

public class ContainerTests
{
    [Fact]
    public void Container_derives_completion_and_acceptance_separately()
    {
        var g = new PlanBuilder().Node("phase", type: "plan_phase").Node("t1", "phase", order: 1).Node("t2", "phase", order: 2).Build();
        Assert.Equal(new ContainerStatus(Id("phase"), ContainerCompletion.Incomplete, ContainerAcceptance.Pending, true), View.Container(g, "phase"));

        g = Finish(Start(g, "t1", "a"), "t1");
        g = Finish(Start(g, "t2", "b"), "t2");
        Assert.Equal(new ContainerStatus(Id("phase"), ContainerCompletion.Complete, ContainerAcceptance.Pending, true), View.Container(g, "phase"));

        g = Accept(Accept(g, "t1"), "t2");
        Assert.Equal(new ContainerStatus(Id("phase"), ContainerCompletion.Complete, ContainerAcceptance.Accepted, true), View.Container(g, "phase"));

        g = Ok(Decide(g, "t2", AcceptanceDecision.Rejected));
        Assert.Equal(ContainerAcceptance.Rejected, View.Container(g, "phase").Acceptance);
    }

    [Fact]
    public void Empty_root_is_explicit()
    {
        var g = new PlanBuilder().Build();
        Assert.Equal(new ContainerStatus(Root, ContainerCompletion.Empty, ContainerAcceptance.Empty, true), View.Container(g, "root"));
        Assert.Empty(PlanRules.Evaluate(g).Leaves);
    }

    [Fact]
    public void A_cancelled_child_keeps_its_container_incomplete_no_silent_descope()
    {
        // T3
        var g = new PlanBuilder()
            .Node("x", type: "plan_phase").Node("p", "x", order: 1).Node("q", "x", order: 2)
            .Node("y").Dep("x", "y")
            .Build();
        g = Complete(g, "p");
        g = Ok(PlanRules.Transition(g, Id("q"), WorkStatus.Cancelled, Ctx(g, "q")));
        Assert.Equal(ContainerCompletion.Incomplete, View.Container(g, "x").Completion);
        Assert.Equal(BlockerReasons.PredecessorNotCompleted, View.Leaf(g, "y").Blockers.Single().Reason);
    }

    [Fact]
    public void Nested_containers_roll_up()
    {
        var g = new PlanBuilder()
            .Node("outer", type: "plan_phase").Node("inner", "outer", type: "plan_step", order: 1).Node("leaf", "inner")
            .Node("sibling", "outer", order: 2)
            .Build();
        g = Complete(Complete(g, "leaf"), "sibling");
        Assert.Equal(new ContainerStatus(Id("outer"), ContainerCompletion.Complete, ContainerAcceptance.Accepted, true), View.Container(g, "outer"));
    }

    [Fact]
    public void Container_predecessor_reports_rejected_before_incomplete()
    {
        // T6
        var g = new PlanBuilder()
            .Node("x", type: "plan_phase").Node("p", "x", order: 1).Node("q", "x", order: 2)
            .Node("y").Dep("x", "y", GatePolicy.Completed)
            .Build();
        g = Ok(Decide(Finish(Start(g, "p", "a"), "p"), "p", AcceptanceDecision.Rejected));
        Assert.Equal(BlockerReasons.PredecessorRejected, View.Leaf(g, "y").Blockers.Single().Reason);
    }

    [Fact]
    public void Upstream_change_inside_a_container_blocks_its_dependents()
    {
        // T2: x's children are accepted, then their upstream changes.
        var g = new PlanBuilder()
            .Node("up")
            .Node("x", type: "plan_phase").Node("c1", "x")
            .Node("y")
            .Dep("up", "c1").Dep("x", "y")
            .Build();
        g = Complete(Complete(g, "up"), "c1");
        Assert.True(View.Leaf(g, "y").Ready);

        g = Ok(PlanRules.ReviseContent(g, Id("up"), Ctx(g, "up")));
        var x = View.Container(g, "x");
        Assert.False(x.GatesHold);
        Assert.Equal(ContainerAcceptance.Pending, x.Acceptance);
        Assert.Equal(BlockerReasons.PredecessorUpstreamChanged, View.Leaf(g, "y").Blockers.Single().Reason);
    }

    [Fact]
    public void Dependency_on_a_container_uses_its_derived_acceptance_and_is_inherited_by_descendants()
    {
        var g = new PlanBuilder()
            .Node("phaseA", type: "plan_phase", order: 1).Node("a1", "phaseA")
            .Node("phaseB", type: "plan_phase", order: 2).Node("b1", "phaseB")
            .Dep("phaseA", "phaseB")
            .Build();
        Assert.Equal(Id("phaseB"), View.Leaf(g, "b1").Blockers.Single().DependencyOwnerId);

        g = Finish(Start(g, "a1", "x"), "a1");
        Assert.Equal(BlockerReasons.PredecessorNotAccepted, View.Leaf(g, "b1").Blockers.Single().Reason);
        Assert.True(View.Leaf(Accept(g, "a1"), "b1").Ready);
    }

    [Fact]
    public void Ready_work_is_listed_in_hierarchy_order()
    {
        var g = new PlanBuilder()
            .Node("p2", type: "plan_phase", order: 2).Node("z", "p2", order: 1)
            .Node("p1", type: "plan_phase", order: 1).Node("y", "p1", order: 2).Node("x", "p1", order: 1)
            .Build();
        Assert.Equal([Id("x"), Id("y"), Id("z")], PlanRules.Evaluate(g).ReadyWork.Select(l => l.NodeId).ToArray());
    }
}
