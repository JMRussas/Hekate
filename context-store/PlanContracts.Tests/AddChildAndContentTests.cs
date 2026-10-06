using Xunit;
using static CodeStoragePoc.PlanContracts.PlanErrorCodes;
using static CodeStoragePoc.PlanContracts.Tests.Ops;
using static CodeStoragePoc.PlanContracts.Tests.PlanBuilder;

namespace CodeStoragePoc.PlanContracts.Tests;

public class AddChildTests
{
    private static PlanNode Child(string name, string parent = "root", string type = "task", Guid? project = null, long contentRevision = 1) =>
        new(Id(name), project ?? Project, type, Id(parent), 0, contentRevision);

    [Fact]
    public void Adds_a_child_and_bumps_only_the_parent_revision()
    {
        var g = new PlanBuilder().Build();
        var r = PlanRules.AddChild(g, Child("a"), Ctx(g, "root"), "digest-1");
        var next = Ok(r);
        Assert.True(next.Nodes.ContainsKey(Id("a")));
        Assert.Equal(1, next.StateOf(Root).StateRevision);
        Assert.Equal(0, next.StateOf(Id("a")).StateRevision);
        Assert.DoesNotContain(Id("a"), g.Nodes.Keys);   // input untouched
    }

    [Fact]
    public void Never_overwrites_and_rejects_empty_ids()
    {
        var g = new PlanBuilder().Node("a").Build();
        AssertRejected(PlanRules.AddChild(g, Child("a"), Ctx(g, "root"), "d"), g, InvalidChild);
        AssertRejected(PlanRules.AddChild(g, new PlanNode(Guid.Empty, Project, "task", Root, 0, 1), Ctx(g, "root"), "d"), g, InvalidChild);
    }

    [Fact]
    public void Rejects_wrong_project_type_plan_type_revision_and_missing_parent()
    {
        var g = new PlanBuilder().Build();
        AssertRejected(PlanRules.AddChild(g, Child("a", project: Id("other")), Ctx(g, "root"), "d"), g, CrossProject);
        AssertRejected(PlanRules.AddChild(g, Child("a", type: "risk"), Ctx(g, "root"), "d"), g, InvalidNodeType);
        AssertRejected(PlanRules.AddChild(g, Child("a", type: "plan"), Ctx(g, "root"), "d"), g, InvalidChild);
        AssertRejected(PlanRules.AddChild(g, Child("a", contentRevision: 3), Ctx(g, "root"), "d"), g, InvalidChild);
        AssertRejected(PlanRules.AddChild(g, Child("a", parent: "ghost"), new OperationContext("k", 0, "t"), "d"), g, NodeNotFound);
    }

    [Fact]
    public void A_pristine_leaf_can_become_a_container_even_after_bookkeeping_moved_its_revision()
    {
        var g = new PlanBuilder().Node("a").Node("x").Build();
        g = Ok(PlanRules.AddDependency(g, Id("x"), Id("a"), null, Ctx(g, "a")));   // bookkeeping only
        Assert.Equal(1, g.StateOf(Id("a")).StateRevision);
        var next = Ok(PlanRules.AddChild(g, Child("a1", parent: "a"), Ctx(g, "a"), "d"));
        Assert.True(PlanRules.Evaluate(next).Containers.Any(c => c.NodeId == Id("a")));
    }

    [Fact]
    public void A_leaf_with_work_state_cannot_become_a_container()
    {
        var g = Start(new PlanBuilder().Node("a").Build(), "a", "x1");
        AssertRejected(PlanRules.AddChild(g, Child("a1", parent: "a"), Ctx(g, "a"), "d"), g, InvalidState);
        // Released work still carries an issued epoch, so it is not pristine either.
        var released = Ok(PlanRules.Transition(g, Id("a"), WorkStatus.Todo, Ctx(g, "a"), "x1", 1));
        AssertRejected(PlanRules.AddChild(released, Child("a1", parent: "a"), Ctx(released, "a"), "d"), released, InvalidState);
    }

    [Fact]
    public void Replay_is_unchanged_and_a_different_payload_with_the_same_key_is_rejected()
    {
        var g = new PlanBuilder().Build();
        var ctx = Ctx(g, "root", key: "add-a");
        var g1 = Ok(PlanRules.AddChild(g, Child("a"), ctx, "digest-1"));
        var replay = PlanRules.AddChild(g1, Child("a"), ctx, "digest-1");
        Assert.Equal(OpOutcome.Unchanged, replay.Outcome);
        Assert.Same(g1, replay.Graph);
        AssertRejected(PlanRules.AddChild(g1, Child("a"), ctx, "digest-2"), g1, OperationKeyReused);
        AssertRejected(PlanRules.AddChild(g1, Child("b"), ctx, "digest-1"), g1, OperationKeyReused);
        // Same key and payload but a different project must not replay as Unchanged.
        AssertRejected(PlanRules.AddChild(g1, Child("a", project: Id("other")), ctx, "digest-1"), g1, OperationKeyReused);
    }
}

public class ContentRevisionTests
{
    [Fact]
    public void Payload_bound_revision_detects_reused_key_with_different_content()
    {
        var g = new PlanBuilder().Node("a").Build();
        var ctx = Ctx(g, "a", key: "edit-1");
        var d1 = PlanContentDigest.Compute("v1", null);
        var g1 = Ok(PlanRules.ReviseContent(g, Id("a"), ctx, d1, expectedContentRevision: 1));
        Assert.Equal(2, g1.Nodes[Id("a")].ContentRevision);

        // Exact retry with the OLD expected revisions is Unchanged, not stale_content.
        var replay = PlanRules.ReviseContent(g1, Id("a"), ctx, d1, expectedContentRevision: 1);
        Assert.Equal(OpOutcome.Unchanged, replay.Outcome);
        Assert.Same(g1, replay.Graph);

        var d2 = PlanContentDigest.Compute("v2", null);
        AssertRejected(PlanRules.ReviseContent(g1, Id("a"), ctx, d2, expectedContentRevision: 1), g1, OperationKeyReused);
    }

    [Fact]
    public void Content_revision_check_applies_to_new_operations()
    {
        var g = new PlanBuilder().Node("a").Build();
        AssertRejected(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a"), "d", expectedContentRevision: 5), g, StaleContent);
    }

    [Fact]
    public void Digest_distinguishes_null_empty_absent_and_key_order_is_irrelevant()
    {
        Assert.NotEqual(PlanContentDigest.Compute(null, null), PlanContentDigest.Compute("", null));
        var withNull = new Dictionary<string, string?> { ["scope"] = null };
        var withEmpty = new Dictionary<string, string?> { ["scope"] = "" };
        Assert.NotEqual(PlanContentDigest.Compute("v", withNull), PlanContentDigest.Compute("v", withEmpty));
        Assert.NotEqual(PlanContentDigest.Compute("v", withNull), PlanContentDigest.Compute("v", null));
        var ab = new Dictionary<string, string?> { ["a"] = "1", ["b"] = "2" };
        var ba = new Dictionary<string, string?> { ["b"] = "2", ["a"] = "1" };
        Assert.Equal(PlanContentDigest.Compute("v", ab), PlanContentDigest.Compute("v", ba));
        // Length-prefixing: splitting the same text differently changes the digest.
        Assert.NotEqual(PlanContentDigest.Compute("x:y", null, "z"), PlanContentDigest.Compute("y", null, "z:x"));
    }
}
