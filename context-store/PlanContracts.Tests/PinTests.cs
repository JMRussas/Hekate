using Xunit;
using static CodeStoragePoc.PlanContracts.PlanErrorCodes;
using static CodeStoragePoc.PlanContracts.Tests.Ops;
using static CodeStoragePoc.PlanContracts.Tests.PlanBuilder;

namespace CodeStoragePoc.PlanContracts.Tests;

/// <summary>Plan 019 §1/§3: attempt pins, strict prerequisite invalidation and pin-aware acceptance.</summary>
public class PinTests
{
    private static string Digest(PlanGraph g, string name) => PlanRules.PrerequisiteSnapshot(g, Id(name)).Digest;

    private static PlanGraph StripPins(PlanGraph g, string name) =>
        g with { States = g.States.SetItem(Id(name), g.StateOf(Id(name)) with { AttemptContentRevision = null, AttemptPrereqDigest = null }) };

    private static OpResult Release(PlanGraph g, string name)
    {
        var s = g.StateOf(Id(name));
        return PlanRules.Transition(g, Id(name), WorkStatus.Todo, Ctx(g, name), s.AttemptId, s.AttemptEpoch);
    }

    // --- pins lifecycle -----------------------------------------------------

    [Fact]
    public void Start_sets_pins_finish_keeps_them_release_and_cancel_clear_them_reopen_renews()
    {
        var g = new PlanBuilder().Node("a").Build();
        var started = Start(g, "a", "x1");
        var s = started.StateOf(Id("a"));
        Assert.Equal(1, s.AttemptContentRevision);
        Assert.Equal(Digest(g, "a"), s.AttemptPrereqDigest);
        Assert.Equal(Digest(started, "a"), s.AttemptPrereqDigest);   // own state is excluded

        var done = Finish(started, "a");
        Assert.Equal((1L, s.AttemptPrereqDigest), (done.StateOf(Id("a")).AttemptContentRevision!.Value, done.StateOf(Id("a")).AttemptPrereqDigest));

        var released = Ok(Release(started, "a")).StateOf(Id("a"));
        Assert.Null(released.AttemptContentRevision);
        Assert.Null(released.AttemptPrereqDigest);

        var cancelled = Ok(PlanRules.Transition(started, Id("a"), WorkStatus.Cancelled, Ctx(started, "a"))).StateOf(Id("a"));
        Assert.Null(cancelled.AttemptContentRevision);
        Assert.Null(cancelled.AttemptPrereqDigest);

        var revised = Ok(PlanRules.ReviseContent(done, Id("a"), Ctx(done, "a")));
        var reopened = Start(revised, "a", "x2").StateOf(Id("a"));
        Assert.Equal(2, reopened.AttemptContentRevision);
    }

    [Fact]
    public void Legacy_unpinned_attempt_fails_closed_on_finish_but_release_and_exact_replay_work()
    {
        var g = new PlanBuilder().Node("a").Build();
        var started = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.InProgress, Ctx(g, "a", "start-key"), "x1"));
        var legacy = StripPins(started, "a");
        Assert.Empty(PlanRules.ValidateGraph(legacy));

        AssertRejected(FinishResult(legacy, "a", "art"), legacy, StaleContent);
        Ok(Release(legacy, "a"));
        var replay = PlanRules.Transition(legacy, Id("a"), WorkStatus.InProgress,
            new OperationContext("start-key", 0, "tester"), "x1");
        Assert.Equal(OpOutcome.Unchanged, replay.Outcome);
    }

    [Fact]
    public void Own_content_revision_during_the_attempt_gives_stale_content()
    {
        var g = Start(new PlanBuilder().Node("a").Build(), "a", "x1");
        g = Ok(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a")));
        AssertRejected(FinishResult(g, "a", "art"), g, StaleContent);
    }

    // --- strict prerequisite invalidation ----------------------------------

    public static TheoryData<string> DriftCases() =>
    [
        "pred_content", "pred_work", "pred_artifact", "pred_acceptance", "edge_added", "edge_removed",
        "ancestor_edge_added", "edge_gate_changed", "default_gate_changed", "transitive", "container_descendant",
        "container_child_added",
    ];

    [Theory]
    [MemberData(nameof(DriftCases))]
    public void Prerequisite_drift_rejects_finish_with_stale_prerequisites(string drift)
    {
        // p (phase) holds t. a -> t, c -> t (Completed gate), z -> a (transitive), box{b1} -> t.
        var g = new PlanBuilder()
            .Node("p", type: "plan_phase").Node("t", "p")
            .Node("z").Node("a").Node("c").Node("x")
            .Node("box", type: "plan_phase").Node("b1", "box")
            .Dep("z", "a").Dep("a", "t").Dep("c", "t", GatePolicy.Completed).Dep("box", "t")
            .Build();
        g = Complete(Complete(Complete(Complete(Complete(g, "z"), "a"), "c"), "b1"), "x");
        g = Start(g, "t", "t1");
        var before = g;

        g = drift switch
        {
            "pred_content" => Ok(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a"))),
            "pred_work" => Start(g, "a", "a2"),
            "pred_artifact" => Finish(Start(g, "c", "c2"), "c", "other-artifact"),
            "pred_acceptance" => Ok(Decide(g, "a", AcceptanceDecision.Rejected)),
            "edge_added" => Ok(PlanRules.AddDependency(g, Id("x"), Id("t"), null, Ctx(g, "t"))),
            "edge_removed" => Ok(PlanRules.RemoveDependency(g, Id("c"), Id("t"), Ctx(g, "t"))),
            "ancestor_edge_added" => Ok(PlanRules.AddDependency(g, Id("x"), Id("p"), null, Ctx(g, "p"))),
            "edge_gate_changed" => g with { Dependencies = g.Dependencies.Replace(
                g.Dependencies.Single(d => d.PredecessorId == Id("c")), new Dependency(Id("c"), Id("t"), GatePolicy.Accepted)) },
            "default_gate_changed" => g with { DefaultGate = GatePolicy.Completed },
            "transitive" => Ok(PlanRules.ReviseContent(g, Id("z"), Ctx(g, "z"))),
            "container_descendant" => Ok(PlanRules.ReviseContent(g, Id("b1"), Ctx(g, "b1"))),
            "container_child_added" => Ok(PlanRules.AddChild(g, new PlanNode(Id("b2"), Project, "task", Id("box"), 1, 1), Ctx(g, "box"), "d")),
            _ => throw new ArgumentException(drift),
        };

        Assert.NotEqual(Digest(before, "t"), Digest(g, "t"));
        var finish = FinishResult(g, "t", "t-artifact");
        Assert.Equal(OpOutcome.Rejected, finish.Outcome);
        Assert.Contains(finish.Errors, e => e.Code == StalePrerequisites);
    }

    [Fact]
    public void Unrelated_work_own_operations_and_bookkeeping_do_not_invalidate()
    {
        var g = new PlanBuilder().Node("a").Node("t").Node("sib").Node("p2", type: "plan_phase", order: 9).Node("q", "p2", order: 3)
            .Dep("a", "t").Build();
        g = Complete(g, "a");
        var digest = Digest(g, "t");

        g = Start(g, "t", "t1");
        g = Complete(g, "sib");                                            // unrelated sibling
        g = Ok(PlanRules.ReviseContent(g, Id("q"), Ctx(g, "q")));          // unrelated subtree
        var a = g.StateOf(Id("a"));                                        // predecessor bookkeeping only
        g = g with { States = g.States.SetItem(Id("a"), a with { StateRevision = a.StateRevision + 7, LastOperationKey = "other", LastOperationFingerprint = "f" }) };
        g = g with { Nodes = g.Nodes.SetItem(Id("t"), g.Nodes[Id("t")] with { SiblingOrder = 42 }) };   // presentation order
        Assert.Equal(digest, Digest(g, "t"));

        g = Finish(g, "t");                                                // own finish
        g = Accept(g, "t");                                                // own decide
        Assert.Equal(digest, Digest(g, "t"));
        Assert.Equal(EffectiveAcceptance.Accepted, PlanRules.EffectiveAcceptanceOf(g, Id("t")));
    }

    [Fact]
    public void Digest_is_deterministic_regardless_of_insertion_order()
    {
        var one = new PlanBuilder().Node("a").Node("b").Node("box", type: "plan_phase").Node("k1", "box", order: 1).Node("k2", "box", order: 0).Node("t")
            .Dep("a", "t").Dep("b", "t").Dep("box", "t").Build();
        var two = new PlanBuilder().Node("t").Node("box", type: "plan_phase").Node("k2", "box", order: 5).Node("k1", "box", order: 2).Node("b").Node("a")
            .Dep("box", "t").Dep("b", "t").Dep("a", "t").Build();
        Assert.Equal(Digest(one, "t"), Digest(two, "t"));
        Assert.Matches("^[0-9a-f]{64}$", Digest(one, "t"));
        var snap = PlanRules.PrerequisiteSnapshot(one, Id("t"));
        Assert.Equal(snap.Nodes.Select(n => n.Id).OrderBy(x => x).ToArray(), snap.Nodes.Select(n => n.Id).ToArray());
        Assert.Equal(new[] { Id("k1"), Id("k2") }.OrderBy(x => x).ToArray(), snap.Nodes.Single(n => n.Id == Id("box")).Children.ToArray());
    }

    [Fact]
    public void Snapshot_records_the_full_raw_acceptance_tuple_not_derived_acceptance()
    {
        var g = Complete(new PlanBuilder().Node("a").Node("t").Node("x0").Dep("a", "t").Build(), "a");
        var recorded = PlanRules.PrerequisiteSnapshot(g, Id("t")).Nodes.Single(n => n.Id == Id("a"));
        Assert.Equal(g.StateOf(Id("a")).Acceptance, recorded.Acceptance);
        Assert.Equal(g.StateOf(Id("a")).AttemptPrereqDigest, recorded.PinnedPrereqDigest);

        // Different evidence or decider is a different prerequisite fact.
        var a = g.StateOf(Id("a"));
        var otherEvidence = g with { States = g.States.SetItem(Id("a"), a with { Acceptance = a.Acceptance! with { EvidenceRef = "other" } }) };
        var otherDecider = g with { States = g.States.SetItem(Id("a"), a with { Acceptance = a.Acceptance! with { DecidedBy = "someone" } }) };
        Assert.NotEqual(Digest(g, "t"), Digest(otherEvidence, "t"));
        Assert.NotEqual(Digest(g, "t"), Digest(otherDecider, "t"));

        // a's own pins drift (its effective acceptance becomes Stale): the snapshot still records the raw record.
        var withX0 = Complete(g, "x0", "w");
        var drifted = Ok(PlanRules.AddDependency(withX0, Id("x0"), Id("a"), null, Ctx(withX0, "a")));
        Assert.Equal(EffectiveAcceptance.Stale, PlanRules.EffectiveAcceptanceOf(drifted, Id("a")));
        Assert.Equal(drifted.StateOf(Id("a")).Acceptance, PlanRules.PrerequisiteSnapshot(drifted, Id("t")).Nodes.Single(n => n.Id == Id("a")).Acceptance);
    }

    // --- acceptance pins (019 §1a) -----------------------------------------

    [Fact]
    public void New_accepted_after_own_content_drift_is_stale_content_and_rejected_is_allowed()
    {
        var g = Finish(Start(new PlanBuilder().Node("a").Build(), "a", "x1"), "a");
        g = Ok(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a")));
        // Decide names the CURRENT revision, so only the pin catches the drift.
        AssertRejected(Decide(g, "a", AcceptanceDecision.Accepted), g, StaleContent);
        Ok(Decide(g, "a", AcceptanceDecision.Rejected));
    }

    [Fact]
    public void New_accepted_after_prerequisite_drift_with_green_gates_is_stale_prerequisites()
    {
        var g = Complete(new PlanBuilder().Node("c").Node("b").Build(), "c");
        g = Finish(Start(g, "b", "y1"), "b");
        g = Ok(PlanRules.AddDependency(g, Id("c"), Id("b"), null, Ctx(g, "b")));
        Assert.True(View.Leaf(g, "b").GatesHold);
        AssertRejected(Decide(g, "b", AcceptanceDecision.Accepted), g, StalePrerequisites);
        Ok(Decide(g, "b", AcceptanceDecision.Rejected));
    }

    [Fact]
    public void Legacy_unpinned_done_keeps_its_acceptance_but_a_new_accept_fails_closed()
    {
        var accepted = StripPins(Complete(new PlanBuilder().Node("a").Build(), "a"), "a");
        Assert.Empty(PlanRules.ValidateGraph(accepted));
        Assert.Equal(EffectiveAcceptance.Accepted, PlanRules.EffectiveAcceptanceOf(accepted, Id("a")));

        var done = StripPins(Finish(Start(new PlanBuilder().Node("a").Build(), "a", "x1"), "a"), "a");
        AssertRejected(Decide(done, "a", AcceptanceDecision.Accepted), done, StaleContent);
        Ok(Decide(done, "a", AcceptanceDecision.Rejected));
    }

    [Fact]
    public void Exact_latest_key_decision_replay_is_unchanged_after_drift()
    {
        var g = Complete(new PlanBuilder().Node("c").Node("b").Dep("c", "b", GatePolicy.Completed).Build(), "c");
        g = Finish(Start(g, "b", "y1"), "b");
        var decideRevision = g.StateOf(Id("b")).StateRevision;
        g = Ok(Decide(g, "b", AcceptanceDecision.Accepted, key: "decide-b"));
        g = Ok(PlanRules.ReviseContent(g, Id("c"), Ctx(g, "c")));   // drift without touching b's state
        Assert.True(View.Leaf(g, "b").GatesHold);
        Assert.Equal(EffectiveAcceptance.Stale, PlanRules.EffectiveAcceptanceOf(g, Id("b")));

        var replay = PlanRules.Decide(g, Id("b"), AcceptanceDecision.Accepted, 1, "b-artifact", 1, "evidence",
            new OperationContext("decide-b", decideRevision, "reviewer"));
        Assert.Equal(OpOutcome.Unchanged, replay.Outcome);
        AssertRejected(Decide(g, "b", AcceptanceDecision.Accepted), g, StalePrerequisites);   // a NEW accept is refused
    }

    // --- state validation --------------------------------------------------

    [Fact]
    public void Pins_require_an_attempt_both_pins_and_a_leaf()
    {
        var noAttempt = new PlanBuilder().Node("a").State("a", NodeState.Initial with { AttemptContentRevision = 1, AttemptPrereqDigest = "d" }).Build();
        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(noAttempt)));

        var started = Start(new PlanBuilder().Node("a").Build(), "a", "x1");
        var half = started with { States = started.States.SetItem(Id("a"), started.StateOf(Id("a")) with { AttemptPrereqDigest = null }) };
        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(half)));

        var container = new PlanBuilder().Node("p", type: "plan_phase").Node("t", "p")
            .State("p", NodeState.Initial with { AttemptContentRevision = 1, AttemptPrereqDigest = "d" }).Build();
        Assert.Contains(InvalidState, Codes(PlanRules.ValidateGraph(container)));
    }
}
