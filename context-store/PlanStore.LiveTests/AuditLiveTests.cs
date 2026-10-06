using CodeStoragePoc.PlanContracts;
using Npgsql;
using Xunit;

namespace CodeStoragePoc.PlanStore.LiveTests;

using Store = CodeStoragePoc.PlanContracts.PlanStore;

/// <summary>Plan 016: executor reference + append-only attempt provenance, against a live database.</summary>
[Collection("live")]
public class AuditLiveTests(LiveDatabase db)
{
    private Store NewStore(string graph = "code_graph") => new(db.ConnectionString, graph);
    private static OperationContext Ctx(string key, long rev, string actor = "live-test") => new(key, rev, actor);
    private static string K() => Guid.NewGuid().ToString("N");

    private async Task<(Store s, Guid root, Guid task)> PlanWithTask()
    {
        var s = NewStore();
        var root = Guid.NewGuid();
        Assert.Equal(OpOutcome.Applied, (await s.CreatePlanAsync(root, db.ProjectId, "Audit plan", PlanContent.Empty, GatePolicy.Accepted, Ctx(K(), 0))).Outcome);
        var task = Guid.NewGuid();
        Assert.Equal(OpOutcome.Applied, (await s.AddChildAsync(root, task, "task", "T", 0, PlanContent.Empty, Ctx(K(), 1))).Outcome);
        return (s, root, task);
    }

    private static async Task<long> Rev(Store s, Guid root, Guid id) => (await s.LoadAsync(root))!.Graph.StateOf(id).StateRevision;
    private static async Task<NodeState> State(Store s, Guid root, Guid id) => (await s.LoadAsync(root))!.Graph.StateOf(id);

    private static void Applied(PlanStoreResult r) =>
        Assert.True(r.Outcome == OpOutcome.Applied, $"{r.Outcome}: {string.Join("; ", r.Errors.Select(e => e.Code + " " + e.Message))}");

    private async Task<Store.EventPage> Events(Store s, Guid root, long after = 0, int limit = 500)
    {
        var (page, notFound) = await s.ReadEventsAsync(root, null, after, limit);
        Assert.Null(notFound);
        return page!;
    }

    private async Task<long> EventSeq(Guid root) =>
        (long)(await db.Scalar("SELECT event_seq FROM public.managed_plans WHERE root_node_id = @r", ("r", root)))!;

    private async Task<long> EventCount(Guid root) =>
        (long)(await db.Scalar("SELECT count(*) FROM public.plan_attempt_events WHERE root_node_id = @r", ("r", root)))!;

    private async Task FencedExec(string sql, bool asStore, params (string, object)[] p)
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

    [Fact]
    public async Task Full_attempt_history_is_recorded_in_order_with_exact_fields()
    {
        var (s, root, t) = await PlanWithTask();
        Applied(await s.TransitionAsync(t, WorkStatus.InProgress, Ctx("k-start", await Rev(s, root, t), "worker"), "x1", null, null, "gods:engine_tasks:7"));
        Applied(await s.TransitionAsync(t, WorkStatus.Done, Ctx(K(), await Rev(s, root, t), "worker"), "x1", 1, "sha-1"));
        Applied(await s.DecideAsync(t, AcceptanceDecision.Rejected, 1, "sha-1", 1, "review-1", Ctx(K(), await Rev(s, root, t), "reviewer")));
        Applied(await s.TransitionAsync(t, WorkStatus.InProgress, Ctx(K(), await Rev(s, root, t), "worker"), "x2", null, null, "gods:engine_tasks:8"));
        Applied(await s.TransitionAsync(t, WorkStatus.Todo, Ctx(K(), await Rev(s, root, t), "worker"), "x2", 2, null));
        Applied(await s.TransitionAsync(t, WorkStatus.InProgress, Ctx(K(), await Rev(s, root, t), "worker"), "x3", null, null, "gods:engine_tasks:9"));
        Applied(await s.TransitionAsync(t, WorkStatus.Cancelled, Ctx(K(), await Rev(s, root, t), "planner"), null, null, null));
        Applied(await s.TransitionAsync(t, WorkStatus.Todo, Ctx(K(), await Rev(s, root, t), "planner"), null, null, null));
        Applied(await s.ReviseContentAsync(t, new PlanContent("spec v2", null), 1, Ctx(K(), await Rev(s, root, t), "planner")));

        var page = await Events(s, root);
        var e = page.Events;
        Assert.Equal(Enumerable.Range(1, 9).Select(i => (long)i), e.Select(x => x.Seq));
        Assert.Equal(
            [AuditEventKind.AttemptStarted, AuditEventKind.AttemptFinished, AuditEventKind.DecisionRecorded, AuditEventKind.AttemptReopened,
             AuditEventKind.AttemptReleased, AuditEventKind.AttemptStarted, AuditEventKind.AttemptCancelled, AuditEventKind.WorkRestored,
             AuditEventKind.ContentRevised],
            e.Select(x => x.Event.Kind));
        Assert.Equal(("x1", 1L, "gods:engine_tasks:7", "worker", "k-start"), (e[0].Event.AttemptId, e[0].Event.AttemptEpoch, e[0].Event.ExecutorRef, e[0].Event.Actor, e[0].Event.OperationKey));
        Assert.Equal(("sha-1", "gods:engine_tasks:7"), (e[1].Event.ArtifactRef, e[1].Event.ExecutorRef));
        Assert.Equal((AcceptanceDecision.Rejected, 1L, "review-1"), (e[2].Event.Decision!.Value, e[2].Event.ReviewedContentRevision!.Value, e[2].Event.EvidenceRef));
        Assert.Equal(("x2", 2L, "gods:engine_tasks:8"), (e[3].Event.AttemptId, e[3].Event.AttemptEpoch, e[3].Event.ExecutorRef));
        Assert.Equal(("x2", 2L, "gods:engine_tasks:8"), (e[4].Event.AttemptId, e[4].Event.AttemptEpoch, e[4].Event.ExecutorRef));   // pre-clear
        Assert.Equal(("x3", 3L, "gods:engine_tasks:9", WorkStatus.InProgress), (e[6].Event.AttemptId, e[6].Event.AttemptEpoch, e[6].Event.ExecutorRef, e[6].Event.WorkFrom));
        Assert.Equal(2L, e[8].Event.ContentRevision);
        Assert.Equal(PlanContentDigest.Compute("spec v2", null), e[8].Event.ContentDigest);
        Assert.Equal(e.Select(x => x.Event.NodeStateRevision).Distinct().Count(), e.Count);
        Assert.Equal(1L, page.HistoryStartsAtSeq);
        Assert.Null((await State(s, root, t)).ExecutorRef);   // cleared by cancel

        // Plan 019: every attempt event carries the attempt's pins; release/cancel carry them pre-clear.
        var digest = PlanRules.PrerequisiteSnapshot((await s.LoadAsync(root))!.Graph, t).Digest;   // t has no prerequisites
        (long?, string?) Pins(int i) => (e[i].Event.AttemptContentRevision, e[i].Event.AttemptPrereqDigest);
        foreach (var i in new[] { 0, 1, 2, 3, 4, 5, 6 }) Assert.Equal((1L, digest), Pins(i));
        Assert.Equal(((long?)null, (string?)null), Pins(7));   // work_restored: no attempt
        Assert.Equal(((long?)null, (string?)null), Pins(8));   // content_revised on Todo
        Assert.All(e, x => Assert.Null(x.Event.ClaimKey));    // generic operations carry no claim key
        var final = await State(s, root, t);
        Assert.Equal(((long?)null, (string?)null), (final.AttemptContentRevision, final.AttemptPrereqDigest));
        Assert.Equal(9L, await EventSeq(root));
    }

    [Fact]
    public async Task Replays_rejections_and_structural_ops_write_no_events()
    {
        var (s, root, t) = await PlanWithTask();
        Assert.Equal(0L, await EventCount(root));   // create + add child: none
        var ctx = Ctx("start-" + K(), await Rev(s, root, t));
        Applied(await s.TransitionAsync(t, WorkStatus.InProgress, ctx, "x1", null, null, "run-1"));
        Assert.Equal(OpOutcome.Unchanged, (await s.TransitionAsync(t, WorkStatus.InProgress, ctx, "x1", null, null, "run-1")).Outcome);
        Assert.Equal(OpOutcome.Rejected, (await s.TransitionAsync(t, WorkStatus.Done, Ctx(K(), await Rev(s, root, t)), "wrong", 1, "a")).Outcome);
        Assert.Equal(OpOutcome.Rejected, (await s.TransitionAsync(t, WorkStatus.Done, Ctx(K(), await Rev(s, root, t)), "x1", 1, "a", "run-other")).Outcome);
        var other = Guid.NewGuid();
        Applied(await s.AddChildAsync(root, other, "task", "O", 1, PlanContent.Empty, Ctx(K(), await Rev(s, root, root))));
        Applied(await s.AddDependencyAsync(other, t, null, Ctx(K(), await Rev(s, root, other))));
        Assert.Equal(1L, await EventCount(root));
        Assert.Equal(1L, await EventSeq(root));
    }

    [Fact]
    public async Task Concurrent_operations_on_two_nodes_get_contiguous_unique_seqs()
    {
        var (s, root, a) = await PlanWithTask();
        var b = Guid.NewGuid();
        Applied(await s.AddChildAsync(root, b, "task", "B", 1, PlanContent.Empty, Ctx(K(), await Rev(s, root, root))));
        var revA = await Rev(s, root, a);
        var revB = await Rev(s, root, b);
        var results = await Task.WhenAll(
            NewStore().TransitionAsync(a, WorkStatus.InProgress, Ctx(K(), revA), "xa", null, null, "run-a"),
            NewStore().TransitionAsync(b, WorkStatus.InProgress, Ctx(K(), revB), "xb", null, null, "run-b"));
        Assert.All(results, Applied);
        var seqs = (await Events(s, root)).Events.Select(e => e.Seq).ToArray();
        Assert.Equal([1L, 2L], seqs);
    }

    [Fact]
    public async Task AGE_failure_and_seq_exhaustion_leave_no_trace()
    {
        var (s, root, t) = await PlanWithTask();
        var rev = await Rev(s, root, t);
        var broken = NewStore(graph: "no_such_graph");
        Assert.Contains((await broken.TransitionAsync(t, WorkStatus.InProgress, Ctx(K(), rev), "x1", null, null, "run-1")).Errors,
            e => e.Code == PlanStoreErrorCodes.ProjectionFailed);
        Assert.Equal((0L, 0L, rev), (await EventCount(root), await EventSeq(root), await Rev(s, root, t)));

        // Exhaust the sequence (increase is allowed with the store flag), then any audited op is refused whole.
        await db.Exec("SELECT 1");
        await using (var conn = new NpgsqlConnection(db.ConnectionString))
        {
            await conn.OpenAsync();
            await using var tx = await conn.BeginTransactionAsync();
            await using (var set = new NpgsqlCommand("SET LOCAL hekate.plan_contract = 'on'", conn, tx)) await set.ExecuteNonQueryAsync();
            await using (var upd = new NpgsqlCommand("UPDATE public.managed_plans SET event_seq = 9223372036854775807 WHERE root_node_id = @r", conn, tx))
            { upd.Parameters.AddWithValue("r", root); await upd.ExecuteNonQueryAsync(); }
            await tx.CommitAsync();
        }
        var r = await s.TransitionAsync(t, WorkStatus.InProgress, Ctx(K(), rev), "x1", null, null, "run-1");
        Assert.Contains(r.Errors, e => e.Code == PlanErrorCodes.RevisionExhausted);
        var st = await State(s, root, t);
        Assert.Equal((WorkStatus.Todo, rev, (string?)null), (st.Work, st.StateRevision, st.ExecutorRef));
        Assert.Equal(0L, await EventCount(root));
        Assert.Equal(0L, await db.TaggedEdgeCount(root));
    }

    [Fact]
    public async Task Events_are_append_only_even_for_the_store()
    {
        var (s, root, t) = await PlanWithTask();
        Applied(await s.TransitionAsync(t, WorkStatus.InProgress, Ctx(K(), await Rev(s, root, t)), "x1", null, null, "run-1"));
        await FencedExec("UPDATE public.plan_attempt_events SET actor = 'forged' WHERE root_node_id = @r", asStore: true, ("r", root));
        await FencedExec("DELETE FROM public.plan_attempt_events WHERE root_node_id = @r", asStore: true, ("r", root));
        // Since 3b1 receipts reference events: a plain TRUNCATE is refused by the foreign key before
        // any trigger runs; CASCADE reaches the append-only fence (on both tables).
        await using (var conn = new NpgsqlConnection(db.ConnectionString))
        {
            await conn.OpenAsync();
            await using var tx = await conn.BeginTransactionAsync();
            await using (var set = new NpgsqlCommand("SET LOCAL hekate.plan_contract = 'on'", conn, tx)) await set.ExecuteNonQueryAsync();
            await using var trunc = new NpgsqlCommand("TRUNCATE public.plan_attempt_events", conn, tx);
            Assert.Equal(PostgresErrorCodes.FeatureNotSupported, (await Assert.ThrowsAsync<PostgresException>(() => trunc.ExecuteNonQueryAsync())).SqlState);
        }
        await FencedExec("TRUNCATE public.plan_attempt_events CASCADE", asStore: true);
        await FencedExec("UPDATE public.managed_plans SET event_seq = 0 WHERE root_node_id = @r", asStore: true, ("r", root));
        await FencedExec("""
            INSERT INTO public.plan_attempt_events (root_node_id, seq, node_id, node_state_revision, kind, work_from, work_to,
                content_revision, attempt_epoch, actor, operation_key)
            VALUES (@r, 99, @t, 99, 'attempt_started', 'todo', 'in_progress', 1, 1, 'x', 'y')
            """, asStore: false, ("r", root), ("t", t));
        Assert.Equal(1L, await EventCount(root));
        Assert.Equal("live-test", (await Events(s, root)).Events.Single().Event.Actor);
    }

    [Fact]
    public async Task Pagination_is_a_stable_seq_cursor_with_node_filter()
    {
        var (s, root, a) = await PlanWithTask();
        var b = Guid.NewGuid();
        Applied(await s.AddChildAsync(root, b, "task", "B", 1, PlanContent.Empty, Ctx(K(), await Rev(s, root, root))));
        for (var i = 1; i <= 3; i++)
        {
            Applied(await s.TransitionAsync(a, WorkStatus.InProgress, Ctx(K(), await Rev(s, root, a)), $"a{i}", null, null, null));
            Applied(await s.TransitionAsync(a, WorkStatus.Todo, Ctx(K(), await Rev(s, root, a)), $"a{i}", i, null));
            Applied(await s.TransitionAsync(b, i == 1 ? WorkStatus.InProgress : WorkStatus.Todo, Ctx(K(), await Rev(s, root, b)),
                "b1", i == 1 ? null : 1, null));
            if (i == 2) break;
        }
        var all = (await Events(s, root)).Events.Select(e => e.Seq).ToArray();
        var p1 = await Events(s, root, 0, 2);
        Assert.Equal(all[..2], p1.Events.Select(e => e.Seq));
        Assert.Equal(all[1], p1.NextAfterSeq);
        // Events appended between calls land after the cursor.
        Applied(await s.TransitionAsync(a, WorkStatus.InProgress, Ctx(K(), await Rev(s, root, a)), "late", null, null, null));
        var rest = await Events(s, root, p1.NextAfterSeq!.Value, 500);
        Assert.Equal(all[2..].Append(all.Max() + 1), rest.Events.Select(e => e.Seq));
        Assert.Null(rest.NextAfterSeq);

        var (nodePage, nf) = await s.ReadEventsAsync(null, b, 0, 500);
        Assert.Null(nf);
        Assert.All(nodePage!.Events, e => Assert.Equal(b, e.Event.NodeId));
        Assert.Equal(nodePage.Events.Select(e => e.Seq).Order(), nodePage.Events.Select(e => e.Seq));
    }

    [Fact]
    public async Task Missing_plan_or_node_is_not_found_but_empty_history_is_a_page()
    {
        var s = NewStore();
        Assert.Equal(PlanStoreErrorCodes.PlanNotFound, (await s.ReadEventsAsync(Guid.NewGuid(), null, 0, 10)).NotFoundCode);
        Assert.Equal(PlanErrorCodes.NodeNotFound, (await s.ReadEventsAsync(null, Guid.NewGuid(), 0, 10)).NotFoundCode);
        var (_, root, t) = await PlanWithTask();
        var (rootPage, rnf) = await s.ReadEventsAsync(root, null, 0, 10);
        Assert.Null(rnf);
        Assert.Empty(rootPage!.Events);
        Assert.Null(rootPage.HistoryStartsAtSeq);
        var (nodePage, nnf) = await s.ReadEventsAsync(null, t, 0, 10);
        Assert.Null(nnf);
        Assert.Empty(nodePage!.Events);
        await Assert.ThrowsAsync<ArgumentException>(() => s.ReadEventsAsync(root, t, 0, 10));
        await Assert.ThrowsAsync<ArgumentException>(() => s.ReadEventsAsync(null, null, 0, 10));
        await Assert.ThrowsAsync<ArgumentOutOfRangeException>(() => s.ReadEventsAsync(root, null, -1, 10));
        await Assert.ThrowsAsync<ArgumentOutOfRangeException>(() => s.ReadEventsAsync(root, null, 0, 501));
    }

    [Fact]
    public async Task Real_schema_upgrade_from_the_2b1_shape_adds_3a_and_preserves_existing_state()
    {
        // A SEPARATE, fresh disposable database owned by this test only; the shared fixture is never downgraded.
        var own = new LiveDatabase();
        try
        {
            // Inside try: if initialisation fails after CREATE DATABASE, finally still drops it (_created guard).
            await own.InitializeAsync();
            async Task<object?> Q(string sql, params (string, object)[] p) => await own.Scalar(sql, p);
            async Task AsStore(string sql, params (string, object)[] p)
            {
                await using var conn = new NpgsqlConnection(own.ConnectionString);
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

            // 1. Restore the 2b1 schema shape: remove the 3b1 additions (receipts first: they reference
            //    events), then ONLY the 3a additions.
            await own.Exec("""
                DROP TRIGGER IF EXISTS trg_hekate_guard_receipts_insert ON public.plan_claim_receipts;
                DROP TRIGGER IF EXISTS trg_hekate_guard_receipts_immutable ON public.plan_claim_receipts;
                DROP TRIGGER IF EXISTS trg_hekate_guard_receipts_truncate ON public.plan_claim_receipts;
                DROP TABLE public.plan_claim_receipts;
                ALTER TABLE public.plan_node_state DROP COLUMN attempt_content_revision, DROP COLUMN attempt_prereq_digest;
                DROP TRIGGER IF EXISTS trg_hekate_guard_events_insert ON public.plan_attempt_events;
                DROP TRIGGER IF EXISTS trg_hekate_guard_events_immutable ON public.plan_attempt_events;
                DROP TRIGGER IF EXISTS trg_hekate_guard_events_truncate ON public.plan_attempt_events;
                DROP TABLE public.plan_attempt_events;
                DROP TRIGGER IF EXISTS trg_hekate_guard_event_seq ON public.managed_plans;
                DROP FUNCTION IF EXISTS public.hekate_guard_events_immutable();
                DROP FUNCTION IF EXISTS public.hekate_guard_event_seq();
                ALTER TABLE public.plan_node_state DROP COLUMN executor_ref;
                ALTER TABLE public.managed_plans DROP COLUMN event_seq;
                """);
            Assert.Equal(0L, await Q("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND ((table_name = 'plan_node_state' AND column_name = 'executor_ref') OR (table_name = 'managed_plans' AND column_name = 'event_seq'))"));
            Assert.Equal(false, await Q("SELECT to_regclass('public.plan_attempt_events') IS NOT NULL"));
            Assert.Equal(false, await Q("SELECT to_regclass('public.plan_claim_receipts') IS NOT NULL"));
            Assert.Equal(0L, await Q("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'plan_node_state' AND column_name IN ('attempt_content_revision', 'attempt_prereq_digest')"));

            // 2. 2b1-era data: a plan with an in-progress attempt and the stored 2b1 start fingerprint.
            var root = Guid.NewGuid();
            var task = Guid.NewGuid();
            const string fp = "10:transition;6:worker;10:InProgress;2:x1;~;~;";
            await AsStore("""
                INSERT INTO public.nodes (id, project_id, node_type, name) VALUES (@r, @p, 'plan', 'Upgrade plan');
                INSERT INTO public.nodes (id, project_id, node_type, name, parent_id, sibling_order) VALUES (@t, @p, 'task', 'T', @r, 0);
                INSERT INTO public.managed_plans (root_node_id, project_id, default_gate, contract_version, created_by)
                    VALUES (@r, @p, 'accepted', 'plan-contract/v1', 'pre-3a');
                INSERT INTO public.plan_node_state (node_id, root_node_id, content_revision, state_revision, work_status, attempt_epoch)
                    VALUES (@r, @r, 1, 2, 'todo', 0);
                INSERT INTO public.plan_node_state (node_id, root_node_id, content_revision, state_revision, work_status, attempt_id,
                        attempt_epoch, last_op_key, last_op_fingerprint, updated_by)
                    VALUES (@t, @r, 1, 1, 'in_progress', 'x1', 1, 'start-2b1', @fp, 'worker');
                """, ("r", root), ("t", task), ("p", own.ProjectId), ("fp", fp));
            var before = (string)(await Q("SELECT row_to_json(s)::jsonb::text FROM public.plan_node_state s WHERE node_id = @t", ("t", task)))!;

            // 3. Upgrade twice.
            await PlanStoreSchema.Ensure(own.ConnectionString);
            await PlanStoreSchema.Ensure(own.ConnectionString);

            Assert.Equal(2L, await Q("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND ((table_name = 'plan_node_state' AND column_name = 'executor_ref') OR (table_name = 'managed_plans' AND column_name = 'event_seq'))"));
            Assert.Equal(true, await Q("SELECT to_regclass('public.plan_attempt_events') IS NOT NULL"));
            Assert.Equal(17L, await Q("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'trg_hekate_guard_%'"));
            // Existing state preserved exactly; the only differences are the new columns, NULL.
            var after = (string)(await Q("SELECT (row_to_json(s)::jsonb - 'executor_ref' - 'attempt_content_revision' - 'attempt_prereq_digest')::text FROM public.plan_node_state s WHERE node_id = @t", ("t", task)))!;
            Assert.Equal(before, after);   // both canonical jsonb text
            Assert.Equal(DBNull.Value, await Q("SELECT executor_ref FROM public.plan_node_state WHERE node_id = @t", ("t", task)) ?? DBNull.Value);
            Assert.Equal(0L, await Q("SELECT event_seq FROM public.managed_plans WHERE root_node_id = @r", ("r", root)));
            Assert.Equal(0L, await Q("SELECT count(*) FROM public.plan_attempt_events"));

            // 4. The 2b1 retry replays as Unchanged against the upgraded schema; still no events.
            var store = new Store(own.ConnectionString);
            var retry = await store.TransitionAsync(task, WorkStatus.InProgress, new OperationContext("start-2b1", 0, "worker"), "x1", null, null);
            Assert.Equal(OpOutcome.Unchanged, retry.Outcome);
            Assert.Equal(0L, await Q("SELECT count(*) FROM public.plan_attempt_events"));
            var reloaded = await store.LoadAsync(root);
            Assert.Equal((WorkStatus.InProgress, "x1", 1L, 1L), (reloaded!.Graph.StateOf(task).Work, reloaded.Graph.StateOf(task).AttemptId,
                reloaded.Graph.StateOf(task).AttemptEpoch, reloaded.Graph.StateOf(task).StateRevision));
        }
        finally
        {
            await own.DisposeAsync();
        }
    }

    [Fact]
    public async Task Upgrade_over_2b1_state_preserves_it_writes_no_events_and_old_retries_replay()
    {
        var (s, root, t) = await PlanWithTask();
        // Simulate an attempt started under 2b1: no executor_ref, no events, 2b1-format fingerprint.
        // Frozen fingerprint exactly as 2b1 stored it for this start operation.
        const string fp = "10:transition;6:worker;10:InProgress;2:x1;~;~;";
        var rev = await Rev(s, root, t);
        await using (var conn = new NpgsqlConnection(db.ConnectionString))
        {
            await conn.OpenAsync();
            await using var tx = await conn.BeginTransactionAsync();
            await using (var set = new NpgsqlCommand("SET LOCAL hekate.plan_contract = 'on'", conn, tx)) await set.ExecuteNonQueryAsync();
            await using (var upd = new NpgsqlCommand("""
                UPDATE public.plan_node_state SET work_status = 'in_progress', attempt_id = 'x1', attempt_epoch = 1,
                    state_revision = state_revision + 1, last_op_key = 'start-2b1', last_op_fingerprint = @fp, executor_ref = NULL
                WHERE node_id = @t
                """, conn, tx))
            { upd.Parameters.AddWithValue("fp", fp); upd.Parameters.AddWithValue("t", t); await upd.ExecuteNonQueryAsync(); }
            await tx.CommitAsync();
        }

        await PlanStoreSchema.Ensure(db.ConnectionString);
        await PlanStoreSchema.Ensure(db.ConnectionString);
        var st = await State(s, root, t);
        Assert.Equal((WorkStatus.InProgress, "x1", 1L, rev + 1, (string?)null), (st.Work, st.AttemptId, st.AttemptEpoch, st.StateRevision, st.ExecutorRef));
        Assert.Equal(0L, await EventCount(root));

        var retry = await s.TransitionAsync(t, WorkStatus.InProgress, Ctx("start-2b1", rev, "worker"), "x1", null, null);
        Assert.Equal(OpOutcome.Unchanged, retry.Outcome);
        Assert.Equal(0L, await EventCount(root));
        var page = await Events(s, root);
        Assert.Null(page.HistoryStartsAtSeq);   // pre-3a history is unknown, never synthesised
    }
}
