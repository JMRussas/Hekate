using CodeStoragePoc.Api.Services;
using CodeStoragePoc.PlanContracts;
using Npgsql;
using Xunit;

namespace CodeStoragePoc.PlanStore.LiveTests;

using Store = CodeStoragePoc.PlanContracts.PlanStore;

[Collection("live")]
public class PlanStoreLiveTests(LiveDatabase db)
{
    private Store NewStore(string graph = "code_graph") => new(db.ConnectionString, graph);
    private static OperationContext Ctx(string key, long rev, string actor = "live-test") => new(key, rev, actor);
    private static string K() => Guid.NewGuid().ToString("N");

    private async Task<Guid> NewPlan(Store s, GatePolicy gate = GatePolicy.Accepted)
    {
        var root = Guid.NewGuid();
        var r = await s.CreatePlanAsync(root, db.ProjectId, "Live plan", PlanContent.Empty, gate, Ctx(K(), 0));
        Assert.True(r.Outcome == OpOutcome.Applied, string.Join("; ", r.Errors.Select(e => e.Code + " " + e.Message)));
        return root;
    }

    private static async Task<long> Rev(Store s, Guid root, Guid id) => (await s.LoadAsync(root))!.Graph.StateOf(id).StateRevision;

    private async Task<Guid> AddChild(Store s, Guid root, Guid parent, string type = "task", string name = "node", PlanContent? content = null)
    {
        var id = Guid.NewGuid();
        var r = await s.AddChildAsync(parent, id, type, name, 0, content ?? PlanContent.Empty, Ctx(K(), await Rev(s, root, parent)));
        Assert.True(r.Outcome == OpOutcome.Applied, string.Join("; ", r.Errors.Select(e => e.Code + " " + e.Message)));
        return id;
    }

    private static void Applied(PlanStoreResult r) =>
        Assert.True(r.Outcome == OpOutcome.Applied, $"{r.Outcome}: {string.Join("; ", r.Errors.Select(e => e.Code + " " + e.Message))}");

    private static void RejectedWith(PlanStoreResult r, string code)
    {
        Assert.Equal(OpOutcome.Rejected, r.Outcome);
        Assert.Contains(r.Errors, e => e.Code == code);
    }

    private async Task<long> DepCount(Guid root) =>
        (long)(await db.Scalar("SELECT count(*) FROM public.plan_dependencies WHERE root_node_id = @r", ("r", root)))!;

    private static async Task<PostgresException> Fenced(Func<Task> action)
    {
        var ex = await Assert.ThrowsAnyAsync<Exception>(action);
        for (Exception? e = ex; e is not null; e = e.InnerException)
            if (e is PostgresException pe && pe.SqlState == PlanStoreSchema.FenceSqlState) return pe;
        throw new Xunit.Sdk.XunitException($"Expected fence HP409, got {ex.GetType().Name}: {ex.Message}");
    }

    // -----------------------------------------------------------------------

    [Fact]
    public async Task Schema_ensure_is_idempotent()
    {
        await PlanStoreSchema.Ensure(db.ConnectionString);
        await PlanStoreSchema.Ensure(db.ConnectionString);
        var triggers = (long)(await db.Scalar("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_hekate_guard_%'"))!;
        Assert.Equal(10, triggers);
    }

    [Fact]
    public async Task Full_lifecycle_persists_across_a_new_store_and_connection()
    {
        var s = NewStore();
        var root = await NewPlan(s);
        var phase = await AddChild(s, root, root, "plan_phase", "Phase");
        var t1 = await AddChild(s, root, phase, "task", "T1", new PlanContent("Build it", new Dictionary<string, string> { ["acceptance_criteria"] = "tests pass" }));
        var t2 = await AddChild(s, root, phase, "task", "T2");
        Applied(await s.AddDependencyAsync(t2, t1, null, Ctx(K(), await Rev(s, root, t2))));
        Applied(await s.TransitionAsync(t1, WorkStatus.InProgress, Ctx(K(), await Rev(s, root, t1)), "attempt-1", null, null));
        Applied(await s.TransitionAsync(t1, WorkStatus.Done, Ctx(K(), await Rev(s, root, t1)), "attempt-1", 1, "sha-abc"));
        Applied(await s.DecideAsync(t1, AcceptanceDecision.Accepted, 1, "sha-abc", 1, "review-1", Ctx(K(), await Rev(s, root, t1), "reviewer")));

        var fresh = await NewStore().LoadAsync(root);
        Assert.NotNull(fresh);
        var g = fresh!.Graph;
        Assert.Equal(GatePolicy.Accepted, g.DefaultGate);
        Assert.Equal(WorkStatus.Done, g.StateOf(t1).Work);
        Assert.Equal(EffectiveAcceptance.Accepted, PlanRules.EffectiveAcceptanceOf(g, t1));
        Assert.Equal("T1", fresh.Names[t1]);
        Assert.Equal("Build it", fresh.Contents[t1].Value);
        Assert.Equal("tests pass", fresh.Contents[t1].Attributes!["acceptance_criteria"]);
        Assert.Null(fresh.Graph.Nodes[root].ParentId);
        Assert.True(PlanRules.Evaluate(g).Leaves.Single(l => l.NodeId == t2).Ready);
        Assert.Equal(1, await DepCount(root));
        Assert.Equal(1, await db.TaggedEdgeCount(root, t2, t1));
        Assert.Equal("tests pass", await db.Scalar("SELECT value FROM public.node_attributes WHERE node_id = @n AND key = 'acceptance_criteria'", ("n", t1)));
    }

    [Fact]
    public async Task Plan_create_is_idempotent_and_never_duplicates_roots()
    {
        var s = NewStore();
        var root = Guid.NewGuid();
        var ctx = Ctx("create-" + K(), 0);
        var content = new PlanContent("v1", null);
        Applied(await s.CreatePlanAsync(root, db.ProjectId, "P", content, GatePolicy.Accepted, ctx));
        Assert.Equal(OpOutcome.Unchanged, (await s.CreatePlanAsync(root, db.ProjectId, "P", content, GatePolicy.Accepted, ctx)).Outcome);
        RejectedWith(await s.CreatePlanAsync(root, db.ProjectId, "P", new PlanContent("v2", null), GatePolicy.Accepted, ctx), PlanStoreErrorCodes.PlanExists);
        RejectedWith(await s.CreatePlanAsync(root, db.ProjectId, "P", content, GatePolicy.Accepted, Ctx(K(), 0)), PlanStoreErrorCodes.PlanExists);
        Assert.Equal(1L, await db.Scalar("SELECT count(*) FROM public.nodes WHERE id = @r", ("r", root)));
        RejectedWith(await s.CreatePlanAsync(Guid.NewGuid(), Guid.NewGuid(), "P", content, GatePolicy.Accepted, Ctx(K(), 0)), PlanStoreErrorCodes.ProjectNotFound);
    }

    [Fact]
    public async Task Two_CAS_writers_exactly_one_applies()
    {
        var s = NewStore();
        var root = await NewPlan(s);
        var t = await AddChild(s, root, root);
        var rev = await Rev(s, root, t);
        var results = await Task.WhenAll(
            NewStore().TransitionAsync(t, WorkStatus.InProgress, Ctx(K(), rev, "worker-a"), "attempt-a", null, null),
            NewStore().TransitionAsync(t, WorkStatus.InProgress, Ctx(K(), rev, "worker-b"), "attempt-b", null, null));
        Assert.Single(results, r => r.Outcome == OpOutcome.Applied);
        Assert.Single(results, r => r.Outcome == OpOutcome.Rejected && r.Errors.Any(e => e.Code == PlanErrorCodes.StaleRevision));
        Assert.Equal(1L, (await s.LoadAsync(root))!.Graph.StateOf(t).AttemptEpoch);
    }

    [Fact]
    public async Task Opposing_dependencies_exactly_one_rejects_the_cycle()
    {
        var s = NewStore();
        var root = await NewPlan(s);
        var a = await AddChild(s, root, root, name: "A");
        var b = await AddChild(s, root, root, name: "B");
        var revA = await Rev(s, root, a);
        var revB = await Rev(s, root, b);
        var results = await Task.WhenAll(
            NewStore().AddDependencyAsync(b, a, null, Ctx(K(), revB)),
            NewStore().AddDependencyAsync(a, b, null, Ctx(K(), revA)));
        Assert.Single(results, r => r.Outcome == OpOutcome.Applied);
        Assert.Single(results, r => r.Outcome == OpOutcome.Rejected && r.Errors.Any(e => e.Code == PlanErrorCodes.DependencyCycle));
        Assert.Equal(1, await DepCount(root));
        Assert.Equal(1, await db.TaggedEdgeCount(root));
    }

    [Fact]
    public async Task Reused_key_with_different_content_is_rejected_and_exact_retry_is_unchanged()
    {
        var s = NewStore();
        var root = await NewPlan(s);
        var t = await AddChild(s, root, root);
        var ctx = Ctx("edit-" + K(), await Rev(s, root, t));
        var v1 = new PlanContent("spec v1", new Dictionary<string, string> { ["scope"] = "small" });
        Applied(await s.ReviseContentAsync(t, v1, 1, ctx));
        Assert.Equal(OpOutcome.Unchanged, (await s.ReviseContentAsync(t, v1, 1, ctx)).Outcome);   // old expected revisions
        RejectedWith(await s.ReviseContentAsync(t, new PlanContent("spec v2", null), 1, ctx), PlanErrorCodes.OperationKeyReused);
        RejectedWith(await s.ReviseContentAsync(t, new PlanContent("spec v1", new Dictionary<string, string> { ["scope"] = "small", ["description"] = "x" }), 1, ctx),
            PlanErrorCodes.OperationKeyReused);
        Assert.Equal("spec v1", await db.Scalar("SELECT value FROM public.nodes WHERE id = @t", ("t", t)));
        Assert.Equal(2L, (await s.LoadAsync(root))!.Graph.Nodes[t].ContentRevision);
        RejectedWith(await s.ReviseContentAsync(t, new PlanContent("x", new Dictionary<string, string> { ["status"] = "done" }), 2, Ctx(K(), await Rev(s, root, t))),
            PlanStoreErrorCodes.InvalidContent);
    }

    [Fact]
    public async Task Legacy_and_raw_SQL_writers_are_fenced_atomically()
    {
        var s = NewStore();
        var root = await NewPlan(s);
        var t = await AddChild(s, root, root, "task", "Fenced", new PlanContent("spec", new Dictionary<string, string> { ["scope"] = "s" }));
        var nodes = new NodeService(db.ConnectionString);

        // Presentation attribute: allowed.
        await db.Exec("INSERT INTO public.node_attributes (node_id, key, value) VALUES (@t, 'priority', 'high')", ("t", t));

        // Legacy replace-all attribute writer (Odin/orchestration path): fenced, nothing changed.
        await Fenced(() => nodes.UpdateAttributes(t, new Dictionary<string, string> { ["status"] = "completed" }));
        Assert.Equal("high", await db.Scalar("SELECT value FROM public.node_attributes WHERE node_id = @t AND key = 'priority'", ("t", t)));
        Assert.Equal("s", await db.Scalar("SELECT value FROM public.node_attributes WHERE node_id = @t AND key = 'scope'", ("t", t)));

        // Legacy content overwrite (UpdateNodeFull): fenced. Name-only change: allowed.
        await Fenced(() => nodes.UpdateNodeFull(t, "Fenced", "overwritten"));
        await db.Exec("UPDATE public.nodes SET name = 'Renamed' WHERE id = @t", ("t", t));
        Assert.Equal("spec", await db.Scalar("SELECT value FROM public.nodes WHERE id = @t", ("t", t)));

        // Raw structural child under a managed node: fenced. Non-structural child: allowed.
        await Fenced(() => db.Exec("INSERT INTO public.nodes (id, project_id, node_type, name, parent_id) VALUES (@id, @p, 'task', 'raw', @t)",
            ("id", Guid.NewGuid()), ("p", db.ProjectId), ("t", t)));
        var risk = Guid.NewGuid();
        await db.Exec("INSERT INTO public.nodes (id, project_id, node_type, name, parent_id) VALUES (@id, @p, 'risk', 'r', @root)",
            ("id", risk), ("p", db.ProjectId), ("root", root));
        // ...which cannot become structural, nor host a structural descendant.
        await Fenced(() => db.Exec("UPDATE public.nodes SET node_type = 'task' WHERE id = @id", ("id", risk)));
        await Fenced(() => db.Exec("INSERT INTO public.nodes (id, project_id, node_type, name, parent_id) VALUES (@id, @p, 'task', 'deep', @risk)",
            ("id", Guid.NewGuid()), ("p", db.ProjectId), ("risk", risk)));

        // Reparent an outside node into, or the risk node out of, the managed tree: fenced.
        var outside = Guid.NewGuid();
        await db.Exec("INSERT INTO public.nodes (id, project_id, node_type, name) VALUES (@id, @p, 'note', 'outside')", ("id", outside), ("p", db.ProjectId));
        await Fenced(() => db.Exec("UPDATE public.nodes SET parent_id = @root WHERE id = @id", ("root", root), ("id", outside)));
        await Fenced(() => db.Exec("UPDATE public.nodes SET parent_id = NULL WHERE id = @id", ("id", risk)));

        // Moving an attribute off a managed node, direct contract-table writes, deletes, truncates: fenced.
        await Fenced(() => db.Exec("UPDATE public.node_attributes SET node_id = @o WHERE node_id = @t AND key = 'priority'", ("o", outside), ("t", t)));
        await Fenced(() => db.Exec("UPDATE public.plan_node_state SET work_status = 'done' WHERE node_id = @t", ("t", t)));
        await Fenced(() => db.Exec("DELETE FROM public.plan_dependencies"));
        await Fenced(() => db.Exec("DELETE FROM public.nodes WHERE id = @t", ("t", t)));
        await Fenced(() => db.Exec("TRUNCATE public.node_attributes"));

        // Documented trust boundary: a caller with full DB credentials can open the fence itself.
        await using var conn = new NpgsqlConnection(db.ConnectionString);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync();
        await using (var set = new NpgsqlCommand("SET LOCAL hekate.plan_contract = 'on'", conn, tx)) await set.ExecuteNonQueryAsync();
        await using (var upd = new NpgsqlCommand("UPDATE public.nodes SET value = 'bypass' WHERE id = @t", conn, tx))
        {
            upd.Parameters.AddWithValue("t", t);
            Assert.Equal(1, await upd.ExecuteNonQueryAsync());
        }
        await tx.RollbackAsync();
        Assert.Equal("spec", await db.Scalar("SELECT value FROM public.nodes WHERE id = @t", ("t", t)));

        // Fence errors leave the contract state readable and unchanged.
        Assert.Equal(WorkStatus.Todo, (await s.LoadAsync(root))!.Graph.StateOf(t).Work);
    }

    [Fact]
    public async Task Managed_tree_walk_is_complete_on_deep_chains_and_terminates_on_cycles()
    {
        var s = NewStore();
        var root = await NewPlan(s);

        // 12,000 unmanaged non-structural nodes chained below the managed root (beyond any
        // fixed depth limit): a structural node at the bottom must still be fenced.
        var ids = Enumerable.Range(0, 12_000).Select(_ => Guid.NewGuid()).ToArray();
        await using (var conn = new NpgsqlConnection(db.ConnectionString))
        {
            await conn.OpenAsync();
            await using var cmd = new NpgsqlCommand("""
                INSERT INTO public.nodes (id, project_id, node_type, name, parent_id)
                SELECT ids[i], @p, 'note', 'chain', CASE WHEN i = 1 THEN @root ELSE ids[i - 1] END
                FROM (SELECT @ids::uuid[] AS ids) a, generate_series(1, array_length(@ids::uuid[], 1)) AS i
                """, conn);
            cmd.Parameters.AddWithValue("p", db.ProjectId);
            cmd.Parameters.AddWithValue("root", root);
            cmd.Parameters.AddWithValue("ids", ids);
            await cmd.ExecuteNonQueryAsync();
        }
        await Fenced(() => db.Exec("INSERT INTO public.nodes (id, project_id, node_type, name, parent_id) VALUES (@id, @p, 'task', 'deep', @leaf)",
            ("id", Guid.NewGuid()), ("p", db.ProjectId), ("leaf", ids[^1])));

        // An unmanaged parent cycle outside any plan: the walk terminates and the insert is allowed.
        var a = Guid.NewGuid();
        var b = Guid.NewGuid();
        await db.Exec("INSERT INTO public.nodes (id, project_id, node_type, name) VALUES (@a, @p, 'note', 'a'), (@b, @p, 'note', 'b')",
            ("a", a), ("b", b), ("p", db.ProjectId));
        await db.Exec("UPDATE public.nodes SET parent_id = @b WHERE id = @a", ("a", a), ("b", b));
        await db.Exec("UPDATE public.nodes SET parent_id = @a WHERE id = @b", ("a", a), ("b", b));
        await db.Exec("INSERT INTO public.nodes (id, project_id, node_type, name, parent_id) VALUES (@id, @p, 'task', 'free', @a)",
            ("id", Guid.NewGuid()), ("p", db.ProjectId), ("a", a));
        Assert.Equal(false, await db.Scalar("SELECT public.hekate_in_managed_tree(@a)", ("a", a)));
        Assert.Equal(true, await db.Scalar("SELECT public.hekate_in_managed_tree(@leaf)", ("leaf", ids[^1])));
    }

    [Fact]
    public async Task AGE_failure_rolls_back_the_whole_operation()
    {
        var s = NewStore();
        var root = await NewPlan(s);
        var a = await AddChild(s, root, root, name: "A");
        var b = await AddChild(s, root, root, name: "B");
        var revB = await Rev(s, root, b);
        var broken = NewStore(graph: "no_such_graph");
        RejectedWith(await broken.AddDependencyAsync(b, a, null, Ctx(K(), revB)), PlanStoreErrorCodes.ProjectionFailed);
        Assert.Equal(0, await DepCount(root));
        Assert.Equal(revB, await Rev(s, root, b));
        // The same operation succeeds on the real graph afterwards.
        Applied(await s.AddDependencyAsync(b, a, null, Ctx(K(), revB)));
    }

    [Fact]
    public async Task Replay_remove_and_reconcile_never_duplicate_projected_edges()
    {
        var s = NewStore();
        var root = await NewPlan(s);
        var a = await AddChild(s, root, root, name: "A");
        var b = await AddChild(s, root, root, name: "B");
        var ctx = Ctx("dep-" + K(), await Rev(s, root, b));
        Applied(await s.AddDependencyAsync(b, a, null, ctx));
        Assert.Equal(OpOutcome.Unchanged, (await s.AddDependencyAsync(b, a, null, ctx)).Outcome);
        Applied(await s.ReconcileProjectionAsync(root));
        Applied(await s.ReconcileProjectionAsync(root));
        Assert.Equal(1, await db.TaggedEdgeCount(root, b, a));

        Applied(await s.RemoveDependencyAsync(b, a, Ctx(K(), await Rev(s, root, b))));
        Assert.Equal(0, await db.TaggedEdgeCount(root));
        Applied(await s.AddDependencyAsync(b, a, GatePolicy.Completed, Ctx(K(), await Rev(s, root, b))));
        Assert.Equal(1, await db.TaggedEdgeCount(root, b, a));

        // A stray duplicate projected edge (AGE is not authority) is repaired by reconcile.
        await using (var conn = new NpgsqlConnection(db.ConnectionString))
        {
            await conn.OpenAsync();
            foreach (var sql in new[]
            {
                "LOAD 'age'", "SET search_path = ag_catalog, \"$user\", public",
                $"SELECT * FROM cypher('code_graph', $$ MATCH (x:CodeNode {{node_id: '{b}'}}), (y:CodeNode {{node_id: '{a}'}}) CREATE (x)-[:DEPENDS_ON {{plan_root: '{root}'}}]->(y) RETURN 1 $$) AS (r agtype)",
            })
            {
                await using var cmd = new NpgsqlCommand(sql, conn);
                await cmd.ExecuteNonQueryAsync();
            }
        }
        Assert.Equal(2, await db.TaggedEdgeCount(root, b, a));
        Applied(await s.ReconcileProjectionAsync(root));
        Assert.Equal(1, await db.TaggedEdgeCount(root, b, a));
    }

    [Fact]
    public async Task A_corrupt_stored_root_parent_is_reported_not_normalised_and_never_projected()
    {
        var s = NewStore();
        var root = await NewPlan(s);
        var t = await AddChild(s, root, root);
        await using (var conn = new NpgsqlConnection(db.ConnectionString))
        {
            await conn.OpenAsync();
            await using var tx = await conn.BeginTransactionAsync();
            await using (var set = new NpgsqlCommand("SET LOCAL hekate.plan_contract = 'on'", conn, tx)) await set.ExecuteNonQueryAsync();
            await using (var upd = new NpgsqlCommand("UPDATE public.nodes SET parent_id = @t WHERE id = @r", conn, tx))
            {
                upd.Parameters.AddWithValue("t", t);
                upd.Parameters.AddWithValue("r", root);
                await upd.ExecuteNonQueryAsync();
            }
            await tx.CommitAsync();
        }
        var snap = await s.LoadAsync(root);
        Assert.Equal(t, snap!.Graph.Nodes[root].ParentId);
        Assert.Contains(PlanRules.ValidateGraph(snap.Graph), e => e.Code == PlanErrorCodes.InvalidRootParent);
        RejectedWith(await s.ReconcileProjectionAsync(root), PlanErrorCodes.InvalidGraph);
        RejectedWith(await s.TransitionAsync(t, WorkStatus.Cancelled, Ctx(K(), await Rev(s, root, t)), null, null, null), PlanErrorCodes.InvalidGraph);
    }

    [Fact]
    public async Task Ids_are_global_child_and_root_conflicts_are_409_not_500()
    {
        var s = NewStore();
        var root = await NewPlan(s);
        var other = await NewPlan(s);
        var otherChild = await AddChild(s, other, other);
        var unmanaged = Guid.NewGuid();
        await db.Exec("INSERT INTO public.nodes (id, project_id, node_type, name) VALUES (@id, @p, 'note', 'free')", ("id", unmanaged), ("p", db.ProjectId));
        var rev = await Rev(s, root, root);

        RejectedWith(await s.AddChildAsync(root, unmanaged, "task", "x", 0, PlanContent.Empty, Ctx(K(), rev)), PlanStoreErrorCodes.NodeExists);
        RejectedWith(await s.AddChildAsync(root, otherChild, "task", "x", 0, PlanContent.Empty, Ctx(K(), rev)), PlanStoreErrorCodes.NodeExists);
        RejectedWith(await s.CreatePlanAsync(unmanaged, db.ProjectId, "P", PlanContent.Empty, GatePolicy.Accepted, Ctx(K(), 0)), PlanStoreErrorCodes.NodeExists);
        Assert.Equal(rev, await Rev(s, root, root));
        Assert.Equal("note", await db.Scalar("SELECT node_type FROM public.nodes WHERE id = @id", ("id", unmanaged)));
    }

    [Fact]
    public async Task Concurrent_create_of_the_same_root_in_two_projects_applies_exactly_once()
    {
        var project2 = Guid.NewGuid();
        await db.Exec("INSERT INTO public.projects (id, name, root_path) VALUES (@p, 'second', 'disposable://second')", ("p", project2));
        var root = Guid.NewGuid();
        var results = await Task.WhenAll(
            NewStore().CreatePlanAsync(root, db.ProjectId, "P1", PlanContent.Empty, GatePolicy.Accepted, Ctx(K(), 0)),
            NewStore().CreatePlanAsync(root, project2, "P2", PlanContent.Empty, GatePolicy.Accepted, Ctx(K(), 0)));
        Assert.Single(results, r => r.Outcome == OpOutcome.Applied);
        var loser = Assert.Single(results, r => r.Outcome == OpOutcome.Rejected);
        Assert.Contains(loser.Errors, e => e.Code is PlanStoreErrorCodes.NodeExists or PlanStoreErrorCodes.PlanExists);
        Assert.Equal(1L, await db.Scalar("SELECT count(*) FROM public.nodes WHERE id = @r", ("r", root)));
        Assert.Equal(1L, await db.Scalar("SELECT count(*) FROM public.managed_plans WHERE root_node_id = @r", ("r", root)));
    }

    [Theory]
    [InlineData(PlanErrorCodes.StaleContent, 409)]
    [InlineData(PlanErrorCodes.StaleRevision, 409)]
    [InlineData(PlanErrorCodes.OperationKeyReused, 409)]
    [InlineData(PlanStoreErrorCodes.NodeExists, 409)]
    [InlineData(PlanStoreErrorCodes.ManagedPlanProtected, 409)]
    [InlineData(CodeStoragePoc.Api.PlanContractEndpoints.MissingField, 400)]
    [InlineData(PlanErrorCodes.InvalidEnum, 400)]
    [InlineData(PlanStoreErrorCodes.PlanNotFound, 404)]
    [InlineData(PlanStoreErrorCodes.ProjectionFailed, 503)]
    [InlineData(PlanErrorCodes.DependencyCycle, 422)]
    [InlineData(PlanErrorCodes.NotReady, 422)]
    public void Error_codes_map_to_stable_http_statuses(string code, int status) =>
        Assert.Equal(status, CodeStoragePoc.Api.PlanContractEndpoints.StatusFor(code));

    [Fact]
    public async Task Rejected_operations_change_no_rows()
    {
        var s = NewStore();
        var root = await NewPlan(s);
        var a = await AddChild(s, root, root, name: "A");
        var b = await AddChild(s, root, root, name: "B");
        Applied(await s.AddDependencyAsync(b, a, null, Ctx(K(), await Rev(s, root, b))));
        var revA = await Rev(s, root, a);
        RejectedWith(await s.AddDependencyAsync(a, b, null, Ctx(K(), revA)), PlanErrorCodes.DependencyCycle);
        RejectedWith(await s.TransitionAsync(b, WorkStatus.InProgress, Ctx(K(), await Rev(s, root, b)), "x", null, null), PlanErrorCodes.NotReady);
        RejectedWith(await s.TransitionAsync(a, WorkStatus.InProgress, Ctx(K(), revA + 5), "x", null, null), PlanErrorCodes.StaleRevision);
        RejectedWith(await s.AddChildAsync(a, a, "task", "dup", 0, PlanContent.Empty, Ctx(K(), revA)), PlanErrorCodes.InvalidChild);
        Assert.Equal(revA, await Rev(s, root, a));
        Assert.Equal(1, await DepCount(root));
        Assert.Equal(1, await db.TaggedEdgeCount(root));
    }
}
