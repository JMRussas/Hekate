using System.Collections.Immutable;
using System.Security.Cryptography;
using System.Text;
using Xunit;

namespace CodeStoragePoc.PlanContracts.Tests;

/// <summary>Builds plan snapshots with readable names mapped to deterministic ids.</summary>
internal sealed class PlanBuilder
{
    public static readonly Guid Project = Id("project");
    public static readonly Guid Root = Id("root");

    private readonly List<PlanNode> _nodes = [new PlanNode(Root, Project, "plan", null, 0, 1)];
    private readonly List<Dependency> _deps = [];
    private readonly Dictionary<Guid, NodeState> _states = [];

    public static Guid Id(string name) => new(MD5.HashData(Encoding.UTF8.GetBytes(name)));

    public PlanBuilder Node(string name, string parent = "root", string type = "task", int order = 0, Guid? project = null)
    {
        _nodes.Add(new PlanNode(Id(name), project ?? Project, type, Id(parent), order, 1));
        return this;
    }

    public PlanBuilder Dep(string predecessor, string successor, GatePolicy? gate = null)
    {
        _deps.Add(new Dependency(Id(predecessor), Id(successor), gate));
        return this;
    }

    public PlanBuilder State(string name, NodeState state)
    {
        _states[Id(name)] = state;
        return this;
    }

    public PlanGraph Build(GatePolicy defaultGate = GatePolicy.Accepted) =>
        PlanGraph.Create(Project, Root, _nodes, _deps, _states, defaultGate);
}

/// <summary>Operation helpers that read current revisions so tests stay focused on rules.</summary>
internal static class Ops
{
    private static int _key;
    public static string NextKey() => $"op-{Interlocked.Increment(ref _key)}";

    public static OperationContext Ctx(PlanGraph g, string name, string? key = null, string actor = "tester") =>
        new(key ?? NextKey(), g.StateOf(PlanBuilder.Id(name)).StateRevision, actor);

    public static PlanGraph Ok(OpResult r)
    {
        Assert.True(r.Outcome == OpOutcome.Applied, $"Expected Applied, got {r.Outcome}: {string.Join("; ", r.Errors.Select(e => e.Code + " " + e.Message))}");
        return r.Graph;
    }

    public static PlanGraph Start(PlanGraph g, string name, string attempt) =>
        Ok(PlanRules.Transition(g, PlanBuilder.Id(name), WorkStatus.InProgress, Ctx(g, name), attempt));

    public static OpResult FinishResult(PlanGraph g, string name, string? artifact, string? key = null)
    {
        var s = g.StateOf(PlanBuilder.Id(name));
        return PlanRules.Transition(g, PlanBuilder.Id(name), WorkStatus.Done, Ctx(g, name, key), s.AttemptId, s.AttemptEpoch, artifact);
    }

    public static PlanGraph Finish(PlanGraph g, string name, string? artifact = null) =>
        Ok(FinishResult(g, name, artifact ?? $"{name}-artifact"));

    public static OpResult Decide(PlanGraph g, string name, AcceptanceDecision decision, string? key = null, string? evidence = "evidence")
    {
        var id = PlanBuilder.Id(name);
        var s = g.StateOf(id);
        return PlanRules.Decide(g, id, decision, g.Nodes[id].ContentRevision, s.ArtifactRef, s.AttemptEpoch, evidence, Ctx(g, name, key, "reviewer"));
    }

    public static PlanGraph Accept(PlanGraph g, string name) => Ok(Decide(g, name, AcceptanceDecision.Accepted));

    /// <summary>Start, finish and accept a leaf.</summary>
    public static PlanGraph Complete(PlanGraph g, string name, string attempt = "a1") => Accept(Finish(Start(g, name, attempt), name), name);

    public static void AssertRejected(OpResult r, PlanGraph input, string code)
    {
        Assert.Equal(OpOutcome.Rejected, r.Outcome);
        Assert.Same(input, r.Graph);
        Assert.Contains(r.Errors, e => e.Code == code);
    }

    public static ImmutableArray<string> Codes(ImmutableArray<PlanError> errors) => [.. errors.Select(e => e.Code)];
}
