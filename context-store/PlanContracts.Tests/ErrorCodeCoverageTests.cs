using System.Reflection;
using Xunit;
using static CodeStoragePoc.PlanContracts.PlanErrorCodes;
using static CodeStoragePoc.PlanContracts.Tests.Ops;
using static CodeStoragePoc.PlanContracts.Tests.PlanBuilder;

namespace CodeStoragePoc.PlanContracts.Tests;

/// <summary>Every stable error code in the contract has a scenario that produces it.</summary>
public class ErrorCodeCoverageTests
{
    private static IEnumerable<string> Validate(PlanGraph g) => Codes(PlanRules.ValidateGraph(g));
    private static IEnumerable<string> Errors(OpResult r) => r.Errors.Select(e => e.Code);

    private static readonly Dictionary<string, Func<IEnumerable<string>>> Scenarios = new()
    {
        [MissingRoot] = () => Validate(PlanGraph.Create(Project, Id("nope"), [new PlanNode(Root, Project, "plan", null, 0, 1)])),
        [InvalidRootType] = () => Validate(PlanGraph.Create(Project, Root, [new PlanNode(Root, Project, "task", null, 0, 1)])),
        [InvalidRootParent] = () => Validate(PlanGraph.Create(Project, Root,
            [new PlanNode(Root, Project, "plan", Id("a"), 0, 1), new PlanNode(Id("a"), Project, "task", Root, 0, 1)])),
        [NodeKeyMismatch] = () =>
        {
            var g = new PlanBuilder().Build();
            return Validate(g with { Nodes = g.Nodes.Add(Id("k"), new PlanNode(Id("v"), Project, "task", Root, 0, 1)) });
        },
        [CrossProject] = () => Validate(new PlanBuilder().Node("a", project: Id("other")).Build()),
        [InvalidNodeType] = () => Validate(new PlanBuilder().Node("a", type: "risk").Build()),
        [MissingParent] = () => Validate(new PlanBuilder().Node("a", parent: "ghost").Build()),
        [HierarchyCycle] = () => Validate(new PlanBuilder().Node("a", parent: "b").Node("b", parent: "a").Build()),
        [OrphanState] = () => Validate(new PlanBuilder().State("ghost", NodeState.Initial).Build()),
        [InvalidRevision] = () => Validate(new PlanBuilder().Node("a").State("a", NodeState.Initial with { StateRevision = -1 }).Build()),
        [InvalidEnum] = () => Validate(new PlanBuilder().Build((GatePolicy)5)),
        [InvalidState] = () => Validate(new PlanBuilder().Node("a").State("a", NodeState.Initial with { Work = WorkStatus.Done }).Build()),
        [MissingDependencyEndpoint] = () => Validate(new PlanBuilder().Node("a").Dep("ghost", "a").Build()),
        [SelfDependency] = () => Validate(new PlanBuilder().Node("a").Dep("a", "a").Build()),
        [DuplicateDependency] = () => Validate(new PlanBuilder().Node("a").Node("b").Dep("a", "b").Dep("a", "b").Build()),
        [DependencyOnAncestor] = () => Validate(new PlanBuilder().Node("p", type: "plan_phase").Node("t", "p").Dep("p", "t").Build()),
        [DependencyCycle] = () => Validate(new PlanBuilder().Node("a").Node("b").Dep("a", "b").Dep("b", "a").Build()),
        [HierarchyDependencyDeadlock] = () => Validate(new PlanBuilder().Node("x", type: "plan_phase").Node("a", "x").Node("b").Dep("x", "b").Dep("b", "a").Build()),
        [InvalidGraph] = () =>
        {
            var g = new PlanBuilder().Node("a", parent: "ghost").Build();
            return Errors(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a")));
        },
        [NodeNotFound] = () =>
        {
            var g = new PlanBuilder().Build();
            return Errors(PlanRules.ReviseContent(g, Id("ghost"), new OperationContext("k", 0, "t")));
        },
        [InvalidOperationKey] = () =>
        {
            var g = new PlanBuilder().Node("a").Build();
            return Errors(PlanRules.ReviseContent(g, Id("a"), new OperationContext(" ", 0, "t")));
        },
        [ActorRequired] = () =>
        {
            var g = new PlanBuilder().Node("a").Build();
            return Errors(PlanRules.ReviseContent(g, Id("a"), new OperationContext("k", 0, "")));
        },
        [OperationKeyReused] = () =>
        {
            var g = new PlanBuilder().Node("a").Build();
            var g1 = Ok(PlanRules.ReviseContent(g, Id("a"), new OperationContext("k", 0, "t")));
            return Errors(PlanRules.Transition(g1, Id("a"), WorkStatus.Cancelled, new OperationContext("k", 1, "t")));
        },
        [StaleRevision] = () =>
        {
            var g = new PlanBuilder().Node("a").Build();
            return Errors(PlanRules.ReviseContent(g, Id("a"), new OperationContext("k", 5, "t")));
        },
        [InvalidChild] = () =>
        {
            var g = new PlanBuilder().Node("a").Build();
            return Errors(PlanRules.AddChild(g, new PlanNode(Id("a"), Project, "task", Root, 0, 1), Ctx(g, "root"), "d"));
        },
        [InvalidExecutorRef] = () =>
        {
            var g = new PlanBuilder().Node("a").Build();
            return Errors(PlanRules.Transition(g, Id("a"), WorkStatus.InProgress, Ctx(g, "a"), "x", executorRef: "has space"));
        },
        [RevisionExhausted] = () =>
        {
            var g = new PlanBuilder().Node("a").State("a", NodeState.Initial with { StateRevision = long.MaxValue }).Build();
            return Errors(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a")));
        },
        [DependencyNotFound] = () =>
        {
            var g = new PlanBuilder().Node("a").Node("b").Build();
            return Errors(PlanRules.RemoveDependency(g, Id("a"), Id("b"), Ctx(g, "b")));
        },
        [ContainerStateIsDerived] = () =>
        {
            var g = new PlanBuilder().Node("p", type: "plan_phase").Node("t", "p").Build();
            return Errors(PlanRules.Transition(g, Id("p"), WorkStatus.Cancelled, Ctx(g, "p")));
        },
        [ContainerRevisionUnsupported] = () =>
        {
            var g = new PlanBuilder().Node("p", type: "plan_phase").Node("t", "p").Build();
            return Errors(PlanRules.ReviseContent(g, Id("p"), Ctx(g, "p")));
        },
        [InvalidTransition] = () =>
        {
            var g = new PlanBuilder().Node("a").Build();
            return Errors(PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a")));
        },
        [AttemptRequired] = () =>
        {
            var g = new PlanBuilder().Node("a").Build();
            return Errors(PlanRules.Transition(g, Id("a"), WorkStatus.InProgress, Ctx(g, "a")));
        },
        [StaleAttempt] = () =>
        {
            var g = Start(new PlanBuilder().Node("a").Build(), "a", "x");
            return Errors(PlanRules.Transition(g, Id("a"), WorkStatus.Done, Ctx(g, "a"), "x", 7));
        },
        [NotReady] = () =>
        {
            var g = new PlanBuilder().Node("a").Node("b").Dep("a", "b").Build();
            return Errors(PlanRules.Transition(g, Id("b"), WorkStatus.InProgress, Ctx(g, "b"), "y"));
        },
        [NotCompleted] = () =>
        {
            var g = new PlanBuilder().Node("a").Build();
            return Errors(PlanRules.Decide(g, Id("a"), AcceptanceDecision.Accepted, 1, null, 0, "e", Ctx(g, "a")));
        },
        [StaleContent] = () =>
        {
            var g = Finish(Start(new PlanBuilder().Node("a").Build(), "a", "x"), "a", "art");
            return Errors(PlanRules.Decide(g, Id("a"), AcceptanceDecision.Accepted, 2, "art", 1, "e", Ctx(g, "a")));
        },
        [StaleArtifact] = () =>
        {
            var g = Finish(Start(new PlanBuilder().Node("a").Build(), "a", "x"), "a", "art");
            return Errors(PlanRules.Decide(g, Id("a"), AcceptanceDecision.Accepted, 1, "other", 1, "e", Ctx(g, "a")));
        },
        [ArtifactRequired] = () =>
        {
            var g = Ok(FinishResult(Start(new PlanBuilder().Node("a").Build(), "a", "x"), "a", null));
            return Errors(PlanRules.Decide(g, Id("a"), AcceptanceDecision.Accepted, 1, null, 1, "e", Ctx(g, "a")));
        },
        [EvidenceRequired] = () =>
        {
            var g = Finish(Start(new PlanBuilder().Node("a").Build(), "a", "x"), "a", "art");
            return Errors(PlanRules.Decide(g, Id("a"), AcceptanceDecision.Accepted, 1, "art", 1, null, Ctx(g, "a")));
        },
        [GatesNotSatisfied] = () =>
        {
            var g = Complete(new PlanBuilder().Node("a").Node("b").Dep("a", "b").Build(), "a");
            g = Finish(Start(g, "b", "y"), "b");
            g = Ok(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a")));
            return Errors(Decide(g, "b", AcceptanceDecision.Accepted));
        },
        [StalePrerequisites] = () =>
        {
            var g = Complete(new PlanBuilder().Node("a").Node("b").Dep("a", "b").Build(), "a");
            g = Start(g, "b", "y");
            g = Ok(PlanRules.ReviseContent(g, Id("a"), Ctx(g, "a")));
            return Errors(FinishResult(g, "b", "art"));
        },
    };

    public static IEnumerable<object[]> AllCodes() =>
        typeof(PlanErrorCodes).GetFields(BindingFlags.Public | BindingFlags.Static)
            .Where(f => f.IsLiteral).Select(f => new object[] { (string)f.GetRawConstantValue()! });

    [Theory]
    [MemberData(nameof(AllCodes))]
    public void Every_error_code_has_a_triggering_scenario(string code)
    {
        Assert.True(Scenarios.ContainsKey(code), $"No scenario for '{code}'.");
        Assert.Contains(code, Scenarios[code]());
    }

    [Fact]
    public void Error_codes_are_unique_lowercase_snake_case()
    {
        var codes = AllCodes().Select(o => (string)o[0]).ToList();
        Assert.Equal(codes.Count, codes.Distinct().Count());
        Assert.All(codes, c => Assert.Matches("^[a-z]+(_[a-z]+)*$", c));
    }
}
