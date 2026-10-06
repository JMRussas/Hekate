using CodeStoragePoc.PlanContracts;
using Npgsql;
using Xunit;

namespace CodeStoragePoc.PlanStore.LiveTests;

using Store = CodeStoragePoc.PlanContracts.PlanStore;

/// <summary>Plan 021: read-only discovery of managed plans, against a live database.</summary>
[Collection("live")]
public class PlanListingLiveTests(LiveDatabase db)
{
    private static OperationContext Ctx(string key, long rev) => new(key, rev, "live-test");
    private static string K() => Guid.NewGuid().ToString("N");

    /// <summary>A fresh project so the listing is isolated from other tests' plans.</summary>
    private async Task<Guid> NewProject()
    {
        var p = Guid.NewGuid();
        await db.Exec("INSERT INTO public.projects (id, name, root_path) VALUES (@p, 'listing', 'disposable://listing')", ("p", p));
        return p;
    }

    private async Task<Guid> NewPlan(Store s, Guid project, string name)
    {
        var root = Guid.NewGuid();
        var r = await s.CreatePlanAsync(root, project, name, PlanContent.Empty, GatePolicy.Accepted, Ctx(K(), 0));
        Assert.Equal(OpOutcome.Applied, r.Outcome);
        return root;
    }

    private async Task AsStore(string sql, params (string, object)[] p)
    {
        await using var conn = new NpgsqlConnection(db.ConnectionString);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync();
        await using (var set = new NpgsqlCommand("SET LOCAL hekate.plan_contract = 'on'", conn, tx)) await set.ExecuteNonQueryAsync();
        await using (var cmd = new NpgsqlCommand(sql, conn, tx))
        {
            foreach (var (n, v) in p) cmd.Parameters.AddWithValue(n, v);
            await cmd.ExecuteNonQueryAsync();
        }
        await tx.CommitAsync();
    }

    /// <summary>PostgreSQL's own uuid order (it differs from System.Guid's comparison).</summary>
    private async Task<Guid[]> PgOrder(Guid project)
    {
        await using var conn = new NpgsqlConnection(db.ConnectionString);
        await conn.OpenAsync();
        await using var cmd = new NpgsqlCommand("SELECT root_node_id FROM public.managed_plans WHERE project_id = @p ORDER BY root_node_id", conn);
        cmd.Parameters.AddWithValue("p", project);
        var ids = new List<Guid>();
        await using var r = await cmd.ExecuteReaderAsync();
        while (await r.ReadAsync()) ids.Add(r.GetGuid(0));
        return [.. ids];
    }

    [Fact]
    public async Task Lists_only_managed_plans_with_project_filter_and_a_gapless_uuid_cursor()
    {
        var s = new Store(db.ConnectionString);
        var project = await NewProject();
        var other = await NewProject();
        var roots = new List<Guid>();
        for (var i = 0; i < 5; i++) roots.Add(await NewPlan(s, project, $"P{i}"));
        var foreign = await NewPlan(s, other, "other project");
        // An unmanaged plan node in the same project (legacy writer path, outside any managed tree).
        var unmanaged = Guid.NewGuid();
        await db.Exec("INSERT INTO public.nodes (id, project_id, node_type, name) VALUES (@id, @p, 'plan', 'legacy plan')", ("id", unmanaged), ("p", project));

        var seen = new List<Guid>();
        Guid? cursor = null;
        var pages = 0;
        do
        {
            var page = await s.ListPlansAsync(project, cursor, 2);
            Assert.True(page.Plans.Count <= 2);
            seen.AddRange(page.Plans.Select(p => p.RootId));
            cursor = page.NextAfterRootId;
            pages++;
        } while (cursor is not null && pages < 10);

        Assert.Equal(3, pages);   // 2 + 2 + 1, the last page has no cursor
        Assert.Equal(await PgOrder(project), seen.ToArray());   // database order, every root exactly once
        Assert.Equal(roots.OrderBy(x => x).ToArray(), seen.OrderBy(x => x).ToArray());
        Assert.DoesNotContain(unmanaged, seen);
        Assert.DoesNotContain(foreign, seen);

        var item = (await s.ListPlansAsync(project, null, 500)).Plans.Single(p => p.RootId == roots[0]);
        Assert.Equal((project, "P0", "accepted", PlanContract.Version, true, 0L, "live-test"),
            (item.ProjectId, item.Name, item.DefaultGate, item.ContractVersion, item.Supported, item.EventSeq, item.CreatedBy));

        // Without a filter, both projects' plans are present and the unmanaged node is not.
        var all = new List<Guid>();
        cursor = null;
        do { var page = await s.ListPlansAsync(null, cursor, 500); all.AddRange(page.Plans.Select(p => p.RootId)); cursor = page.NextAfterRootId; } while (cursor is not null);
        Assert.Contains(foreign, all);
        Assert.Contains(roots[4], all);
        Assert.DoesNotContain(unmanaged, all);
        Assert.Equal(all.Count, all.Distinct().Count());
    }

    [Fact]
    public async Task Unsupported_and_corrupt_plans_are_listed_as_metadata_and_listing_writes_nothing()
    {
        var s = new Store(db.ConnectionString);
        var project = await NewProject();
        var ok = await NewPlan(s, project, "ok");
        var unsupported = await NewPlan(s, project, "future");
        var corrupt = await NewPlan(s, project, "corrupt");
        var t = Guid.NewGuid();
        Assert.Equal(OpOutcome.Applied, (await s.AddChildAsync(corrupt, t, "task", "T", 0, PlanContent.Empty, Ctx(K(), 1))).Outcome);
        Assert.True((await s.ClaimAsync(corrupt, "c1", "att", null, "w")).Ok);
        await AsStore("UPDATE public.managed_plans SET contract_version = 'plan-contract/v9' WHERE root_node_id = @r", ("r", unsupported));
        await AsStore("UPDATE public.plan_node_state SET attempt_prereq_digest = NULL WHERE node_id = @t", ("t", t));   // invalid stored state

        async Task<string> Fingerprint() => (string)(await db.Scalar("""
            SELECT concat_ws('|',
                (SELECT string_agg(row_to_json(m)::jsonb::text, ',' ORDER BY root_node_id) FROM public.managed_plans m),
                (SELECT string_agg(row_to_json(s)::jsonb::text, ',' ORDER BY node_id) FROM public.plan_node_state s),
                (SELECT count(*) FROM public.plan_dependencies), (SELECT count(*) FROM public.plan_attempt_events),
                (SELECT count(*) FROM public.plan_claim_receipts))
            """))!;
        var before = await Fingerprint();
        var edgesBefore = await db.TaggedEdgeCount(corrupt);

        var plans = (await s.ListPlansAsync(project, null, 500)).Plans.ToDictionary(p => p.RootId);
        Assert.Equal(3, plans.Count);
        Assert.True(plans[ok].Supported);
        Assert.Equal(("plan-contract/v9", false), (plans[unsupported].ContractVersion, plans[unsupported].Supported));
        Assert.Equal((true, 1L), (plans[corrupt].Supported, plans[corrupt].EventSeq));   // listed; opening it reports the stored errors

        Assert.Equal(before, await Fingerprint());
        Assert.Equal(edgesBefore, await db.TaggedEdgeCount(corrupt));
    }

    [Fact]
    public async Task Listing_guards_its_arguments_and_an_unknown_project_is_an_empty_page()
    {
        var s = new Store(db.ConnectionString);
        await Assert.ThrowsAsync<ArgumentOutOfRangeException>(() => s.ListPlansAsync(null, null, 0));
        await Assert.ThrowsAsync<ArgumentOutOfRangeException>(() => s.ListPlansAsync(null, null, 501));
        var empty = await s.ListPlansAsync(Guid.NewGuid(), null, 10);
        Assert.Empty(empty.Plans);
        Assert.Null(empty.NextAfterRootId);
        var past = await s.ListPlansAsync(null, Guid.Parse("ffffffff-ffff-ffff-ffff-ffffffffffff"), 10);
        Assert.Empty(past.Plans);
    }
}
