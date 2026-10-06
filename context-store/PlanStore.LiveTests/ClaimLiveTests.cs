using CodeStoragePoc.PlanContracts;
using Npgsql;
using Xunit;

namespace CodeStoragePoc.PlanStore.LiveTests;

using Store = CodeStoragePoc.PlanContracts.PlanStore;

/// <summary>Plan 019 (3b1): durable claim receipts and attempt pins, against a live database.</summary>
[Collection("live")]
public class ClaimLiveTests(LiveDatabase db)
{
    private Store NewStore(string graph = "code_graph") => new(db.ConnectionString, graph);
    private static OperationContext Ctx(string key, long rev, string actor = "live-test") => new(key, rev, actor);
    private static string K() => Guid.NewGuid().ToString("N");

    private static void Applied(PlanStoreResult r) =>
        Assert.True(r.Outcome == OpOutcome.Applied, $"{r.Outcome}: {string.Join("; ", r.Errors.Select(e => e.Code + " " + e.Message))}");

    private static ClaimResult Ok(ClaimResult r)
    {
        Assert.True(r.Ok, string.Join("; ", r.Errors.Select(e => e.Code + " " + e.Message)));
        return r;
    }

    private static async Task<long> Rev(Store s, Guid root, Guid id) => (await s.LoadAsync(root))!.Graph.StateOf(id).StateRevision;
    private static async Task<NodeState> State(Store s, Guid root, Guid id) => (await s.LoadAsync(root))!.Graph.StateOf(id);

    /// <summary>A plan with leaves a (order 0) and b (order 1); b depends on a when <paramref name="chain"/>.</summary>
    private async Task<(Store s, Guid root, Guid a, Guid b)> Plan(bool chain = false, string aValue = "spec a")
    {
        var s = NewStore();
        var root = Guid.NewGuid();
        Applied(await s.CreatePlanAsync(root, db.ProjectId, "Claim plan", PlanContent.Empty, GatePolicy.Accepted, Ctx(K(), 0)));
        var a = Guid.NewGuid();
        var b = Guid.NewGuid();
        Applied(await s.AddChildAsync(root, a, "task", "A", 0,
            new PlanContent(aValue, new Dictionary<string, string> { ["acceptance_criteria"] = "tests pass" }), Ctx(K(), await Rev(s, root, root))));
        Applied(await s.AddChildAsync(root, b, "task", "B", 1, PlanContent.Empty, Ctx(K(), await Rev(s, root, root))));
        if (chain) Applied(await s.AddDependencyAsync(b, a, null, Ctx(K(), await Rev(s, root, b))));
        return (s, root, a, b);
    }

    private async Task<long> Count(string table, Guid root) =>
        (long)(await db.Scalar($"SELECT count(*) FROM public.{table} WHERE root_node_id = @r", ("r", root)))!;

    private async Task<long> EventSeq(Guid root) =>
        (long)(await db.Scalar("SELECT event_seq FROM public.managed_plans WHERE root_node_id = @r", ("r", root)))!;

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

    private async Task Fenced(string sql, bool asStore, params (string, object)[] p)
    {
        await using var conn = new NpgsqlConnection(db.ConnectionString);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync();
        if (asStore) { await using var set = new NpgsqlCommand("SET LOCAL hekate.plan_contract = 'on'", conn, tx); await set.ExecuteNonQueryAsync(); }
        await using var cmd = new NpgsqlCommand(sql, conn, tx);
        foreach (var (n, v) in p) cmd.Parameters.AddWithValue(n, v);
        var ex = await Assert.ThrowsAsync<PostgresException>(() => cmd.ExecuteNonQueryAsync());
        Assert.Equal(PlanStoreSchema.FenceSqlState, ex.SqlState);
    }

    /// <summary>Everything a claim could write, for "nothing changed" assertions.</summary>
    private async Task<string> Fingerprint(Guid root) => (string)(await db.Scalar("""
        SELECT concat_ws('|',
            (SELECT string_agg(row_to_json(s)::jsonb::text, ',' ORDER BY node_id) FROM public.plan_node_state s WHERE root_node_id = @r),
            (SELECT count(*) FROM public.plan_attempt_events WHERE root_node_id = @r),
            (SELECT count(*) FROM public.plan_claim_receipts WHERE root_node_id = @r),
            (SELECT event_seq FROM public.managed_plans WHERE root_node_id = @r))
        """, ("r", root)))!;

    // -----------------------------------------------------------------------

    [Fact]
    public async Task Claim_starts_the_first_ready_leaf_and_records_pins_event_and_receipt()
    {
        var (s, root, a, b) = await Plan(chain: true);
        var r = Ok(await s.ClaimAsync(root, "job-1", "att-1", "gods:engine_tasks:1", "worker"));
        Assert.False(r.Replayed);
        Assert.True(r.StillCurrent);
        var rc = r.Receipt!;
        Assert.Equal(("claimed", a, "att-1", 1L, "gods:engine_tasks:1", 1L, "worker"),
            (rc.Outcome, rc.NodeId!.Value, rc.AttemptId, rc.AttemptEpoch!.Value, rc.ExecutorRef, rc.ContentRevision!.Value, rc.Actor));

        var snap = (await s.LoadAsync(root))!;
        var st = snap.Graph.StateOf(a);
        Assert.Equal((WorkStatus.InProgress, "att-1", 1L, (long?)1, rc.PrereqDigest), (st.Work, st.AttemptId, st.AttemptEpoch, st.AttemptContentRevision, st.AttemptPrereqDigest));
        Assert.Equal(PlanRules.PrerequisiteSnapshot(snap.Graph, a).Digest, rc.PrereqDigest);
        Assert.Equal(snap.Contents[a].Digest(), rc.ContentDigest);
        Assert.Equal("""{"value": "spec a", "attributes": {"acceptance_criteria": "tests pass"}}""",
            (string)(await db.Scalar("SELECT content_snapshot::text FROM public.plan_claim_receipts WHERE root_node_id = @r", ("r", root)))!);
        Assert.Equal(rc.PrereqDigest, (string)(await db.Scalar("SELECT prereq_snapshot->>'digest' FROM public.plan_claim_receipts WHERE root_node_id = @r", ("r", root)))!);

        var (page, _) = await s.ReadEventsAsync(root, null, 0, 100);
        var ev = page!.Events.Single();
        Assert.Equal(rc.EventSeq, ev.Seq);
        Assert.Equal((AuditEventKind.AttemptStarted, "job-1", Store.ReservedClaimKeyPrefix + "job-1", "att-1", (long?)1, rc.PrereqDigest),
            (ev.Event.Kind, ev.Event.ClaimKey, ev.Event.OperationKey, ev.Event.AttemptId, ev.Event.AttemptContentRevision, ev.Event.AttemptPrereqDigest));
        Assert.Equal(1L, await db.TaggedEdgeCount(root));   // projection reconciled (b -> a)

        // b is not ready (gate on a): the next claim records no_ready_work.
        var none = Ok(await s.ClaimAsync(root, "job-2", "att-2", null, "worker"));
        Assert.Equal(("no_ready_work", (Guid?)null, false, (ClaimCurrent?)null), (none.Receipt!.Outcome, none.Receipt.NodeId, none.StillCurrent, none.Current));
        Assert.Equal(WorkStatus.Todo, (await State(s, root, b)).Work);
    }

    [Fact]
    public async Task Replay_returns_the_original_receipt_writes_nothing_and_reports_factual_correlation()
    {
        var (s, root, a, b) = await Plan();
        var first = Ok(await s.ClaimAsync(root, "job-r", "att-1", null, "worker")).Receipt!;
        var before = await Fingerprint(root);

        var again = Ok(await s.ClaimAsync(root, "job-r", "att-1", null, "worker"));
        Assert.True(again.Replayed);
        Assert.True(again.StillCurrent);
        Assert.Equal(first, again.Receipt);
        Assert.Equal(before, await Fingerprint(root));

        // Finish: replay is still the same receipt, no new claim, stillCurrent false.
        Applied(await s.TransitionAsync(a, WorkStatus.Done, Ctx(K(), await Rev(s, root, a), "worker"), "att-1", 1, "sha-1"));
        var afterFinish = await Fingerprint(root);
        var replay = Ok(await s.ClaimAsync(root, "job-r", "att-1", null, "worker"));
        Assert.Equal((first, true, false, WorkStatus.Done), (replay.Receipt, replay.Replayed, replay.StillCurrent, replay.Current!.Work));
        Assert.Equal(afterFinish, await Fingerprint(root));

        // A new claim on another node, then release it: the old receipt never moves.
        var other = Ok(await s.ClaimAsync(root, "job-s", "att-2", null, "worker")).Receipt!;
        Assert.Equal(b, other.NodeId);
        Applied(await s.TransitionAsync(b, WorkStatus.Todo, Ctx(K(), await Rev(s, root, b), "worker"), "att-2", 1, null));
        Assert.Equal(first, Ok(await s.ReadClaimAsync(root, "job-r")).Receipt);
        var otherRead = Ok(await s.ReadClaimAsync(root, "job-s"));
        Assert.Equal((other, false, WorkStatus.Todo), (otherRead.Receipt, otherRead.StillCurrent, otherRead.Current!.Work));
    }

    [Fact]
    public async Task Payload_conflict_and_input_errors_write_nothing()
    {
        var (s, root, _, _) = await Plan();
        Ok(await s.ClaimAsync(root, "job-c", "att-1", "run-1", "worker"));
        var before = await Fingerprint(root);
        Assert.Equal(PlanErrorCodes.OperationKeyReused, (await s.ClaimAsync(root, "job-c", "att-2", "run-1", "worker")).Errors[0].Code);
        Assert.Equal(PlanErrorCodes.OperationKeyReused, (await s.ClaimAsync(root, "job-c", "att-1", null, "worker")).Errors[0].Code);
        Assert.Equal(PlanErrorCodes.OperationKeyReused, (await s.ClaimAsync(root, "job-c", "att-1", "run-1", "someone")).Errors[0].Code);
        foreach (var bad in new[] { "", ".", "..", "a/b", "a%2Fb", "a?b", "a#b", "a b", "hekate-claim:x", new string('k', 129) })
            Assert.Equal(PlanStoreErrorCodes.InvalidInput, (await s.ClaimAsync(root, bad, "att", null, "worker")).Errors[0].Code);
        Assert.Equal(PlanErrorCodes.InvalidExecutorRef, (await s.ClaimAsync(root, "ok", "att", "has space", "worker")).Errors[0].Code);
        Assert.Equal(PlanStoreErrorCodes.InvalidInput, (await s.ClaimAsync(root, "ok", " ", null, "worker")).Errors[0].Code);
        Assert.Equal(PlanStoreErrorCodes.PlanNotFound, (await s.ClaimAsync(Guid.NewGuid(), "ok", "att", null, "worker")).Errors[0].Code);
        Assert.Equal(PlanStoreErrorCodes.ClaimNotFound, (await s.ReadClaimAsync(root, "never")).Errors[0].Code);
        Assert.Equal(before, await Fingerprint(root));
        Ok(await s.ClaimAsync(root, "x._~-123", "att-9", null, "worker"));   // every allowed punctuation
    }

    [Fact]
    public async Task No_ready_work_is_stable_and_needs_a_new_key()
    {
        var s = NewStore();
        var root = Guid.NewGuid();
        Applied(await s.CreatePlanAsync(root, db.ProjectId, "Single", PlanContent.Empty, GatePolicy.Accepted, Ctx(K(), 0)));
        var t = Guid.NewGuid();
        Applied(await s.AddChildAsync(root, t, "task", "T", 0, PlanContent.Empty, Ctx(K(), 1)));
        Ok(await s.ClaimAsync(root, "first", "att-1", null, "worker"));
        var none = Ok(await s.ClaimAsync(root, "second", "att-2", null, "worker"));
        Assert.Equal("no_ready_work", none.Receipt!.Outcome);
        Assert.Equal(1L, await Count("plan_attempt_events", root));

        Applied(await s.TransitionAsync(t, WorkStatus.Todo, Ctx(K(), await Rev(s, root, t), "worker"), "att-1", 1, null));   // work ready again
        var replay = Ok(await s.ClaimAsync(root, "second", "att-2", null, "worker"));
        Assert.Equal((none.Receipt, true, false), (replay.Receipt, replay.Replayed, replay.StillCurrent));
        Assert.Equal(WorkStatus.Todo, (await State(s, root, t)).Work);
        var fresh = Ok(await s.ClaimAsync(root, "third", "att-3", null, "worker"));
        Assert.Equal((t, 2L), (fresh.Receipt!.NodeId!.Value, fresh.Receipt.AttemptEpoch!.Value));
    }

    [Fact]
    public async Task Concurrent_claims_with_different_keys_get_different_leaves_or_no_ready_work()
    {
        var (s, root, a, b) = await Plan();
        var results = await Task.WhenAll(Enumerable.Range(0, 3).Select(i => NewStore().ClaimAsync(root, $"par-{i}", $"att-{i}", null, "worker")));
        Assert.All(results, r => Ok(r));
        var claimed = results.Where(r => r.Receipt!.Outcome == "claimed").Select(r => r.Receipt!.NodeId!.Value).OrderBy(x => x).ToArray();
        Assert.Equal(new[] { a, b }.OrderBy(x => x).ToArray(), claimed);
        Assert.Single(results, r => r.Receipt!.Outcome == "no_ready_work");
        Assert.Equal(2L, await Count("plan_attempt_events", root));
        Assert.Equal(new long[] { 1, 2 }, (await s.ReadEventsAsync(root, null, 0, 10)).Page!.Events.Select(e => e.Seq).ToArray());
    }

    [Fact]
    public async Task Concurrent_same_key_same_payload_claims_once_and_both_see_the_same_receipt()
    {
        var (s, root, _, _) = await Plan();
        var results = await Task.WhenAll(Enumerable.Range(0, 4).Select(_ => NewStore().ClaimAsync(root, "dup", "att-1", "run-1", "worker")));
        Assert.All(results, r => Ok(r));
        Assert.Single(results, r => !r.Replayed);
        Assert.Single(results.Select(r => r.Receipt).Distinct());
        Assert.Equal(1L, await Count("plan_claim_receipts", root));
        Assert.Equal(1L, await Count("plan_attempt_events", root));
    }

    [Fact]
    public async Task AGE_failure_and_seq_exhaustion_leave_no_receipt_event_or_state()
    {
        var (s, root, a, _) = await Plan();
        var before = await Fingerprint(root);
        var broken = NewStore(graph: "no_such_graph");
        Assert.Equal(PlanStoreErrorCodes.ProjectionFailed, (await broken.ClaimAsync(root, "age-1", "att", null, "worker")).Errors[0].Code);
        Assert.Equal(before, await Fingerprint(root));

        // no_ready_work is a write too: AGE failure leaves no receipt.
        Ok(await s.ClaimAsync(root, "fill-1", "att-1", null, "worker"));
        Ok(await s.ClaimAsync(root, "fill-2", "att-2", null, "worker"));
        var full = await Fingerprint(root);
        Assert.Equal(PlanStoreErrorCodes.ProjectionFailed, (await broken.ClaimAsync(root, "age-2", "att", null, "worker")).Errors[0].Code);
        Assert.Equal(full, await Fingerprint(root));
        Assert.Equal(PlanStoreErrorCodes.ClaimNotFound, (await s.ReadClaimAsync(root, "age-2")).Errors[0].Code);

        // Sequence overflow: the claim is refused whole.
        var (s2, root2, a2, _) = await Plan();
        await AsStore("UPDATE public.managed_plans SET event_seq = 9223372036854775807 WHERE root_node_id = @r", ("r", root2));
        var exhausted = await Fingerprint(root2);
        Assert.Equal(PlanErrorCodes.RevisionExhausted, (await s2.ClaimAsync(root2, "seq", "att", null, "worker")).Errors[0].Code);
        Assert.Equal(exhausted, await Fingerprint(root2));
        Assert.Equal(WorkStatus.Todo, (await State(s2, root2, a2)).Work);
        Assert.Equal(WorkStatus.InProgress, (await State(s, root, a)).Work);
    }

    [Fact]
    public async Task Receipts_are_append_only_even_for_the_store()
    {
        var (s, root, _, _) = await Plan();
        Ok(await s.ClaimAsync(root, "imm", "att-1", null, "worker"));
        await Fenced("UPDATE public.plan_claim_receipts SET actor = 'forged' WHERE root_node_id = @r", asStore: true, ("r", root));
        await Fenced("DELETE FROM public.plan_claim_receipts WHERE root_node_id = @r", asStore: true, ("r", root));
        await Fenced("TRUNCATE public.plan_claim_receipts", asStore: true);
        await Fenced("INSERT INTO public.plan_claim_receipts (root_node_id, claim_key, request_fingerprint, outcome, actor) VALUES (@r, 'raw', 'f', 'no_ready_work', 'x')",
            asStore: false, ("r", root));
        Assert.Equal("worker", Ok(await s.ReadClaimAsync(root, "imm")).Receipt!.Actor);
        Assert.Equal(1L, await Count("plan_claim_receipts", root));
    }

    [Fact]
    public async Task Reserved_claim_prefix_is_rejected_for_new_generic_operations_but_exact_replay_holds()
    {
        var (s, root, a, b) = await Plan();
        var rb = await Rev(s, root, b);
        Assert.Equal(PlanErrorCodes.InvalidOperationKey,
            (await s.TransitionAsync(b, WorkStatus.InProgress, Ctx("hekate-claim:forged", rb), "x", null, null)).Errors[0].Code);
        Assert.Equal(PlanErrorCodes.InvalidOperationKey,
            (await s.CreatePlanAsync(Guid.NewGuid(), db.ProjectId, "P", PlanContent.Empty, GatePolicy.Accepted, Ctx("hekate-claim:p", 0))).Errors[0].Code);
        Assert.Equal(PlanErrorCodes.InvalidOperationKey,
            (await s.AddDependencyAsync(b, a, null, Ctx("hekate-claim:dep", rb))).Errors[0].Code);
        Assert.Equal(rb, await Rev(s, root, b));

        // The claim's own start, retried as the identical generic transition, is the node's latest key: Unchanged.
        Ok(await s.ClaimAsync(root, "rk", "att-1", "run-1", "worker"));
        var replay = await s.TransitionAsync(a, WorkStatus.InProgress, Ctx(Store.ReservedClaimKeyPrefix + "rk", 0, "worker"), "att-1", null, null, "run-1");
        Assert.Equal(OpOutcome.Unchanged, replay.Outcome);
        // Once it is no longer the latest key, the prefix is refused.
        Applied(await s.TransitionAsync(a, WorkStatus.Done, Ctx(K(), await Rev(s, root, a), "worker"), "att-1", 1, "sha"));
        Assert.Equal(PlanErrorCodes.InvalidOperationKey,
            (await s.TransitionAsync(a, WorkStatus.InProgress, Ctx(Store.ReservedClaimKeyPrefix + "rk", 0, "worker"), "att-1", null, null, "run-1")).Errors[0].Code);
    }

    [Fact]
    public async Task Still_current_compares_the_saved_content_digest_not_only_the_revision()
    {
        var (s, root, a, _) = await Plan();
        Ok(await s.ClaimAsync(root, "dg", "att-1", null, "worker"));
        Assert.True(Ok(await s.ReadClaimAsync(root, "dg")).StillCurrent);
        // A raw trusted repair (holder of store credentials) changes the value without bumping the revision.
        await AsStore("UPDATE public.nodes SET value = 'repaired' WHERE id = @a", ("a", a));
        Assert.Equal(1L, (await s.LoadAsync(root))!.Graph.Nodes[a].ContentRevision);
        var read = Ok(await s.ReadClaimAsync(root, "dg"));
        Assert.False(read.StillCurrent);
        Assert.Equal(WorkStatus.InProgress, read.Current!.Work);   // the attempt itself is untouched
    }

    [Fact]
    public async Task Replay_and_read_fail_closed_on_invalid_or_unsupported_plans_without_writing()
    {
        var (s, root, a, _) = await Plan();
        var claimed = Ok(await s.ClaimAsync(root, "inv", "att-1", null, "worker")).Receipt!;

        // Partial pins are invalid stored state.
        await AsStore("UPDATE public.plan_node_state SET attempt_prereq_digest = NULL WHERE node_id = @a", ("a", a));
        var before = await Fingerprint(root);
        var replay = Ok(await s.ClaimAsync(root, "inv", "att-1", null, "worker"));
        Assert.Equal((claimed, true, false), (replay.Receipt, replay.Replayed, replay.StillCurrent));
        Assert.Equal(before, await Fingerprint(root));
        Assert.Equal(PlanErrorCodes.InvalidGraph, (await s.ClaimAsync(root, "new-on-invalid", "att-2", null, "worker")).Errors[0].Code);
        Assert.Equal(before, await Fingerprint(root));

        // Unsupported contract version.
        var (s2, root2, _, _) = await Plan();
        var r2 = Ok(await s2.ClaimAsync(root2, "ver", "att-1", null, "worker")).Receipt!;
        await AsStore("UPDATE public.managed_plans SET contract_version = 'plan-contract/v9' WHERE root_node_id = @r", ("r", root2));
        var read = Ok(await s2.ReadClaimAsync(root2, "ver"));
        Assert.Equal((r2, false), (read.Receipt, read.StillCurrent));
        Assert.Equal(PlanStoreErrorCodes.UnsupportedContractVersion, (await s2.ClaimAsync(root2, "ver-new", "att-2", null, "worker")).Errors[0].Code);
    }

    [Fact]
    public async Task Replay_and_read_fail_closed_on_an_unknown_stored_enum_without_writing()
    {
        // Own database: the corruption needs the work_status CHECK dropped, never on the shared fixture.
        var own = new LiveDatabase();
        try
        {
            await own.InitializeAsync();
            var s = new Store(own.ConnectionString);
            var root = Guid.NewGuid();
            var t = Guid.NewGuid();
            Applied(await s.CreatePlanAsync(root, own.ProjectId, "Enum plan", PlanContent.Empty, GatePolicy.Accepted, Ctx(K(), 0)));
            Applied(await s.AddChildAsync(root, t, "task", "T", 0, PlanContent.Empty, Ctx(K(), 1)));
            var claimed = Ok(await s.ClaimAsync(root, "enum", "att-1", null, "worker")).Receipt!;

            await own.Exec("ALTER TABLE public.plan_node_state DROP CONSTRAINT plan_node_state_work_status_check");
            await using (var conn = new NpgsqlConnection(own.ConnectionString))
            {
                await conn.OpenAsync();
                await using var tx = await conn.BeginTransactionAsync();
                await using (var set = new NpgsqlCommand("SET LOCAL hekate.plan_contract = 'on'", conn, tx)) await set.ExecuteNonQueryAsync();
                await using (var upd = new NpgsqlCommand("UPDATE public.plan_node_state SET work_status = 'paused' WHERE node_id = @t", conn, tx))
                { upd.Parameters.AddWithValue("t", t); await upd.ExecuteNonQueryAsync(); }
                await tx.CommitAsync();
            }
            async Task<string> Snapshot() => (string)(await own.Scalar("""
                SELECT concat_ws('|', (SELECT string_agg(row_to_json(s)::jsonb::text, ',' ORDER BY node_id) FROM public.plan_node_state s),
                    (SELECT count(*) FROM public.plan_attempt_events), (SELECT count(*) FROM public.plan_claim_receipts))
                """))!;
            var before = await Snapshot();

            var replay = Ok(await s.ClaimAsync(root, "enum", "att-1", null, "worker"));
            Assert.Equal((claimed, true, false, (ClaimCurrent?)null), (replay.Receipt, replay.Replayed, replay.StillCurrent, replay.Current));
            var read = Ok(await s.ReadClaimAsync(root, "enum"));
            Assert.Equal((claimed, false), (read.Receipt, read.StillCurrent));
            Assert.Equal(PlanErrorCodes.InvalidGraph, (await s.ClaimAsync(root, "enum-new", "att-2", null, "worker")).Errors[0].Code);
            Assert.Equal(before, await Snapshot());
        }
        finally
        {
            await own.DisposeAsync();
        }
    }

    [Fact]
    public async Task Stale_prerequisites_and_stale_content_reject_a_pinned_finish()
    {
        var (s, root, a, b) = await Plan();
        Ok(await s.ClaimAsync(root, "pa", "att-a", null, "worker"));   // a
        Ok(await s.ClaimAsync(root, "pb", "att-b", null, "worker"));   // b
        // a gains a prerequisite (b) while in progress -> stale_prerequisites.
        Applied(await s.AddDependencyAsync(a, b, GatePolicy.Completed, Ctx(K(), await Rev(s, root, a))));
        var fa = await s.TransitionAsync(a, WorkStatus.Done, Ctx(K(), await Rev(s, root, a), "worker"), "att-a", 1, "sha-a");
        Assert.Equal(PlanErrorCodes.StalePrerequisites, fa.Errors[0].Code);
        Assert.False(Ok(await s.ReadClaimAsync(root, "pa")).StillCurrent);
        // b's own content changes -> stale_content.
        Applied(await s.ReviseContentAsync(b, new PlanContent("v2", null), 1, Ctx(K(), await Rev(s, root, b))));
        var fb = await s.TransitionAsync(b, WorkStatus.Done, Ctx(K(), await Rev(s, root, b), "worker"), "att-b", 1, "sha-b");
        Assert.Equal(PlanErrorCodes.StaleContent, fb.Errors[0].Code);
        // Release stays possible and records the pins before they are cleared.
        Applied(await s.TransitionAsync(b, WorkStatus.Todo, Ctx(K(), await Rev(s, root, b), "worker"), "att-b", 1, null));
        var released = (await s.ReadEventsAsync(null, b, 0, 50)).Page!.Events[^1].Event;
        Assert.Equal((AuditEventKind.AttemptReleased, (long?)1), (released.Kind, released.AttemptContentRevision));
        Assert.NotNull(released.AttemptPrereqDigest);
        Assert.Null((await State(s, root, b)).AttemptPrereqDigest);
    }

    [Fact]
    public async Task Real_schema_upgrade_from_the_frozen_3a_shape_adds_3b1_and_preserves_existing_state()
    {
        // Own fresh database; the frozen 3a DDL is restored onto the base schema, never a downgrade of the shared fixture.
        var own = LiveDatabase.WithoutPlanSchema();
        try
        {
            await own.InitializeAsync();
            async Task<object?> Q(string sql, params (string, object)[] p) => await own.Scalar(sql, p);
            await own.Exec(await File.ReadAllTextAsync(Path.Combine(AppContext.BaseDirectory, "Fixtures", "schema-3a.sql")));
            Assert.Equal(14L, await Q("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_hekate_guard_%'"));
            Assert.Equal(false, await Q("SELECT to_regclass('public.plan_claim_receipts') IS NOT NULL"));
            Assert.Equal(0L, await Q("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND column_name IN ('attempt_content_revision', 'attempt_prereq_digest', 'claim_key')"));

            // 3a-era data: an in-progress attempt with its frozen 3a start fingerprint, its 3a event, and an accepted legacy node.
            var root = Guid.NewGuid();
            var t = Guid.NewGuid();
            var done = Guid.NewGuid();
            const string fp = "10:transition;6:worker;10:InProgress;2:x1;~;~;12:executor_ref;5:run-1;";
            await using (var conn = new NpgsqlConnection(own.ConnectionString))
            {
                await conn.OpenAsync();
                await using var tx = await conn.BeginTransactionAsync();
                await using (var set = new NpgsqlCommand("SET LOCAL hekate.plan_contract = 'on'", conn, tx)) await set.ExecuteNonQueryAsync();
                await using var cmd = new NpgsqlCommand("""
                    INSERT INTO public.nodes (id, project_id, node_type, name) VALUES (@r, @p, 'plan', 'Upgrade plan');
                    INSERT INTO public.nodes (id, project_id, node_type, name, parent_id, sibling_order) VALUES (@t, @p, 'task', 'T', @r, 0);
                    INSERT INTO public.nodes (id, project_id, node_type, name, parent_id, sibling_order) VALUES (@d, @p, 'task', 'D', @r, 1);
                    INSERT INTO public.managed_plans (root_node_id, project_id, default_gate, contract_version, created_by, event_seq)
                        VALUES (@r, @p, 'accepted', 'plan-contract/v1', 'pre-3b1', 1);
                    INSERT INTO public.plan_node_state (node_id, root_node_id, content_revision, state_revision, work_status, attempt_epoch)
                        VALUES (@r, @r, 1, 3, 'todo', 0);
                    INSERT INTO public.plan_node_state (node_id, root_node_id, content_revision, state_revision, work_status, attempt_id,
                            attempt_epoch, last_op_key, last_op_fingerprint, executor_ref, updated_by)
                        VALUES (@t, @r, 1, 1, 'in_progress', 'x1', 1, 'start-3a', @fp, 'run-1', 'worker');
                    INSERT INTO public.plan_node_state (node_id, root_node_id, content_revision, state_revision, work_status, attempt_id,
                            attempt_epoch, artifact_ref, acc_decision, acc_content_revision, acc_artifact_ref, acc_attempt_id, acc_attempt_epoch,
                            acc_decided_by, acc_evidence_ref, updated_by)
                        VALUES (@d, @r, 1, 3, 'done', 'y1', 1, 'sha-d', 'accepted', 1, 'sha-d', 'y1', 1, 'reviewer', 'ev', 'reviewer');
                    INSERT INTO public.plan_attempt_events (root_node_id, seq, node_id, node_state_revision, kind, work_from, work_to,
                            content_revision, attempt_id, attempt_epoch, executor_ref, actor, operation_key)
                        VALUES (@r, 1, @t, 1, 'attempt_started', 'todo', 'in_progress', 1, 'x1', 1, 'run-1', 'worker', 'start-3a');
                    """, conn, tx);
                cmd.Parameters.AddWithValue("r", root);
                cmd.Parameters.AddWithValue("t", t);
                cmd.Parameters.AddWithValue("d", done);
                cmd.Parameters.AddWithValue("p", own.ProjectId);
                cmd.Parameters.AddWithValue("fp", fp);
                await cmd.ExecuteNonQueryAsync();
                await tx.CommitAsync();
            }
            var stateBefore = (string)(await Q("SELECT string_agg(row_to_json(s)::jsonb::text, ',' ORDER BY node_id) FROM public.plan_node_state s"))!;
            var eventBefore = (string)(await Q("SELECT row_to_json(e)::jsonb::text FROM public.plan_attempt_events e"))!;

            // Upgrade twice.
            await PlanStoreSchema.Ensure(own.ConnectionString);
            await PlanStoreSchema.Ensure(own.ConnectionString);

            Assert.Equal(17L, await Q("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_hekate_guard_%'"));
            Assert.Equal(true, await Q("SELECT to_regclass('public.plan_claim_receipts') IS NOT NULL"));
            Assert.Equal(5L, await Q("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND column_name IN ('attempt_content_revision', 'attempt_prereq_digest', 'claim_key') AND table_name IN ('plan_node_state', 'plan_attempt_events')"));
            Assert.Equal(stateBefore, (string)(await Q("SELECT string_agg((row_to_json(s)::jsonb - 'attempt_content_revision' - 'attempt_prereq_digest')::text, ',' ORDER BY node_id) FROM public.plan_node_state s"))!);
            Assert.Equal(eventBefore, (string)(await Q("SELECT (row_to_json(e)::jsonb - 'attempt_content_revision' - 'attempt_prereq_digest' - 'claim_key')::text FROM public.plan_attempt_events e"))!);
            Assert.Equal(0L, await Q("SELECT count(*) FROM public.plan_node_state WHERE attempt_content_revision IS NOT NULL OR attempt_prereq_digest IS NOT NULL"));
            Assert.Equal(0L, await Q("SELECT count(*) FROM public.plan_attempt_events WHERE attempt_content_revision IS NOT NULL OR attempt_prereq_digest IS NOT NULL OR claim_key IS NOT NULL"));
            Assert.Equal(0L, await Q("SELECT count(*) FROM public.plan_claim_receipts"));

            var store = new Store(own.ConnectionString);
            // The exact 3a retry still replays (fingerprint format unchanged), writing nothing.
            Assert.Equal(OpOutcome.Unchanged, (await store.TransitionAsync(t, WorkStatus.InProgress, new OperationContext("start-3a", 0, "worker"), "x1", null, null, "run-1")).Outcome);
            Assert.Equal(1L, await Q("SELECT count(*) FROM public.plan_attempt_events"));
            // Legacy acceptance keeps its meaning; a NULL-pin attempt fails closed on finish but can be released.
            var g = (await store.LoadAsync(root))!.Graph;
            Assert.Equal(EffectiveAcceptance.Accepted, PlanRules.EffectiveAcceptanceOf(g, done));
            Assert.Equal(PlanErrorCodes.StaleContent,
                (await store.TransitionAsync(t, WorkStatus.Done, new OperationContext("finish-3b", 1, "worker"), "x1", 1, "sha")).Errors[0].Code);
            var release = await store.TransitionAsync(t, WorkStatus.Todo, new OperationContext("release-3b", 1, "worker"), "x1", 1, null);
            Assert.Equal(OpOutcome.Applied, release.Outcome);
            var released = (await store.ReadEventsAsync(root, null, 0, 10)).Page!.Events[^1].Event;
            Assert.Equal((AuditEventKind.AttemptReleased, (long?)null, (string?)null), (released.Kind, released.AttemptContentRevision, released.AttemptPrereqDigest));
            // A new claim now works on the upgraded plan.
            var claim = await store.ClaimAsync(root, "post-upgrade", "x2", null, "worker");
            Assert.True(claim.Ok);
            Assert.Equal((t, 2L), (claim.Receipt!.NodeId!.Value, claim.Receipt.AttemptEpoch!.Value));
        }
        finally
        {
            await own.DisposeAsync();
        }
    }
}
