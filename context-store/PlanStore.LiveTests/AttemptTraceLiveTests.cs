using System.Text;
using System.Text.Json;
using CodeStoragePoc.Api;
using CodeStoragePoc.PlanContracts;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.HttpResults;
using Xunit;

namespace CodeStoragePoc.PlanStore.LiveTests;

using Store = CodeStoragePoc.PlanContracts.PlanStore;

/// <summary>
/// Plan 047: the attempt-trace endpoint against a live database — identity binding (node, attempt,
/// receipt, journal), status and integrity, confinement and paging. The supervisor journal is the
/// fixture schema the local store uses; trace files live in a per-test temp runs root.
/// </summary>
[Collection("live")]
public sealed class AttemptTraceLiveTests(LiveDatabase db) : IDisposable
{
    private readonly string _traceRoot = Directory.CreateDirectory(Path.Combine(Path.GetTempPath(), "hekate-trace-live-" + Guid.NewGuid().ToString("N"))).FullName;

    public void Dispose() => Directory.Delete(_traceRoot, true);

    private static OperationContext Ctx(string key, long rev, string actor = "live-test") => new(key, rev, actor);
    private static string K() => Guid.NewGuid().ToString("N");
    private static async Task<long> Rev(Store s, Guid root, Guid id) => (await s.LoadAsync(root))!.Graph.StateOf(id).StateRevision;

    private static void Applied(PlanStoreResult r) =>
        Assert.True(r.Outcome == OpOutcome.Applied, $"{r.Outcome}: {string.Join("; ", r.Errors.Select(e => e.Code + " " + e.Message))}");

    /// <summary>A plan with leaf a (claimed with claimKey/attemptId when given) and leaf b.</summary>
    private async Task<(Store s, Guid root, Guid a, Guid b)> Plan(string? claimKey = null, string? attemptId = null)
    {
        await EnsureJournal(db);
        var s = new Store(db.ConnectionString);
        var root = Guid.NewGuid();
        Guid a = Guid.NewGuid(), b = Guid.NewGuid();
        Applied(await s.CreatePlanAsync(root, db.ProjectId, "Trace plan", PlanContent.Empty, GatePolicy.Accepted, Ctx(K(), 0)));
        Applied(await s.AddChildAsync(root, a, "task", "A", 0, PlanContent.Empty, Ctx(K(), await Rev(s, root, root))));
        Applied(await s.AddChildAsync(root, b, "task", "B", 1, PlanContent.Empty, Ctx(K(), await Rev(s, root, root))));
        if (claimKey is not null)
        {
            var claim = await s.ClaimAsync(root, claimKey, attemptId!, null, "worker");
            Assert.True(claim.Ok && claim.Receipt!.NodeId == a, string.Join("; ", claim.Errors.Select(e => e.Code)));
        }
        return (s, root, a, b);
    }

    private static async Task EnsureJournal(LiveDatabase d)
    {
        if (await d.Scalar("SELECT to_regclass('supervisor_journal.records') IS NOT NULL") is true) return;
        await d.Exec(File.ReadAllText(RepoFile("scripts/local/supervisor_e1/e1/journal_schema.sql")));
        await d.Exec("INSERT INTO supervisor_journal.writer_usage (writer_id, epoch, unresolved) VALUES ('w', 1, 0)");
    }

    private static string RepoFile(string relative)
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
            if (File.Exists(Path.Combine(dir.FullName, relative))) return Path.Combine(dir.FullName, relative);
        throw new FileNotFoundException(relative);
    }

    private async Task Journal(Guid root, string claimKey, string state, params (string Kind, string Data)[] records)
    {
        await db.Exec("""
            INSERT INTO supervisor_journal.streams (root, claim_key, writer_id, state, count, bytes, reserved, fault_reserved, next_seq, head_hash)
            VALUES (@r, @k, 'w', @st, 0, 0, 0, 0, 1, 'h')
            """, ("r", root.ToString("D")), ("k", claimKey), ("st", state));
        var seq = 1;
        foreach (var (kind, data) in records)
            await db.Exec("""
                INSERT INTO supervisor_journal.records (record_id, root, claim_key, seq, kind, writer_id, writer_epoch, at_json, data,
                    version, intent_digest, prev_hash, record_hash, budget_bytes)
                VALUES (@id, @r, @k, @seq, @kind, 'w', 1, '0', @data, 1, 'd', 'p', 'h', 0)
                """, ("id", Guid.NewGuid()), ("r", root.ToString("D")), ("k", claimKey), ("seq", (long)seq++), ("kind", kind), ("data", data));
    }

    private string RunDir(string name = "run-1") => Directory.CreateDirectory(Path.Combine(_traceRoot, name)).FullName;

    private static string Launch(string runDir) => JsonSerializer.Serialize(new
    {
        baseRef = "abc", trace = new { version = AttemptTrace.Version, executionKind = "fake-cli", runDir, prompt = "attempt-r1.prompt.txt", trace = "attempt-r1.trace.jsonl" },
    });

    private static string Exited(byte[] prompt, byte[] trace, int records, bool complete = true, bool writeError = false, string? sha = null) => JsonSerializer.Serialize(new
    {
        code = 0, reason = "exit",
        trace = new
        {
            complete,
            prompt = new { bytes = prompt.Length, sha256 = AttemptTrace.Sha256Hex(prompt) },
            trace = new { bytes = trace.Length, sha256 = sha ?? AttemptTrace.Sha256Hex(trace), records, capped = false, writeError },
        },
    });

    private static byte[] Records(int n) => Encoding.ASCII.GetBytes(string.Concat(Enumerable.Range(0, n).Select(i =>
        JsonSerializer.Serialize(new { seq = i, tMs = i * 10, stream = i % 2 == 0 ? "stdout" : "stderr", text = $"line {i}", cut = false, redacted = false }) + "\n")));

    private static (byte[] Prompt, byte[] Trace) Files(string runDir, byte[] trace)
    {
        var prompt = "Edit README.md only.\n"u8.ToArray();
        File.WriteAllBytes(Path.Combine(runDir, "attempt-r1.prompt.txt"), prompt);
        File.WriteAllBytes(Path.Combine(runDir, "attempt-r1.trace.jsonl"), trace);
        return (prompt, trace);
    }

    private static (int Status, JsonElement Body) Run(IResult r)
    {
        if (r is FileContentHttpResult f) return (200, JsonDocument.Parse(f.FileContents).RootElement.Clone());
        var status = (r as IStatusCodeHttpResult)?.StatusCode ?? 200;
        var value = (r as IValueHttpResult)?.Value;
        return (status, JsonDocument.Parse(JsonSerializer.SerializeToUtf8Bytes(value, AttemptTrace.Json)).RootElement.Clone());
    }

    private async Task<(int Status, JsonElement Body)> Get(Store s, Guid node, string attempt, string? afterSeq = null, string? limit = null, string? root = "")
        => Run(await PlanContractEndpoints.Trace(s, root == "" ? _traceRoot : root, node, attempt, afterSeq, limit));

    private static string? S(JsonElement e, string k) => e.GetProperty(k).ValueKind == JsonValueKind.Null ? null : e.GetProperty(k).ToString();

    // -----------------------------------------------------------------------

    [Fact]
    public async Task A_finished_trace_is_verified_and_pages_with_the_prompt_only_first()
    {
        var (s, root, a, _) = await Plan("k-ok", "att-ok");
        var run = RunDir();
        var (prompt, trace) = Files(run, Records(5));
        await Journal(root, "k-ok", "resolved", ("launch_intent", Launch(run)), ("launched", "{}"), ("exited", Exited(prompt, trace, 5)));

        var (status, body) = await Get(s, a, "att-ok", limit: "3");
        Assert.Equal(200, status);
        Assert.Equal(["contractVersion", "nodeId", "attemptId", "attemptEpoch", "claimKey", "status", "reason", "integrity", "executionKind",
            "exit", "prompt", "records", "nextAfterSeq", "capped"], body.EnumerateObject().Select(p => p.Name));
        Assert.Equal(("exited", "verified", "k-ok", "fake-cli"), (S(body, "status"), S(body, "integrity"), S(body, "claimKey"), S(body, "executionKind")));
        Assert.Null(S(body, "reason"));
        Assert.Equal(0, body.GetProperty("exit").GetProperty("code").GetInt64());
        Assert.Equal("Edit README.md only.\n", body.GetProperty("prompt").GetProperty("text").GetString());
        Assert.Equal([0L, 1L, 2L], body.GetProperty("records").EnumerateArray().Select(r => r.GetProperty("seq").GetInt64()));
        Assert.Equal("stderr", body.GetProperty("records")[1].GetProperty("stream").GetString());
        Assert.Equal(2, body.GetProperty("nextAfterSeq").GetInt64());

        var (_, rest) = await Get(s, a, "att-ok", afterSeq: "2", limit: "3");
        Assert.Equal(JsonValueKind.Null, rest.GetProperty("prompt").ValueKind);
        Assert.Equal([3L, 4L], rest.GetProperty("records").EnumerateArray().Select(r => r.GetProperty("seq").GetInt64()));
        Assert.Equal(JsonValueKind.Null, rest.GetProperty("nextAfterSeq").ValueKind);
    }

    [Fact]
    public async Task An_attempt_still_in_progress_is_unverified_and_its_partial_line_is_not_read()
    {
        var (s, root, a, _) = await Plan("k-run", "att-run");
        var run = RunDir();
        Files(run, [.. Records(2), .. "{\"seq\":2,\"tM"u8.ToArray()]);
        await Journal(root, "k-run", "active", ("launch_intent", Launch(run)));
        var (status, body) = await Get(s, a, "att-run");
        Assert.Equal(200, status);
        Assert.Equal(("running", "unverified"), (S(body, "status"), S(body, "integrity")));
        Assert.Equal(JsonValueKind.Null, body.GetProperty("exit").ValueKind);
        Assert.Equal(2, body.GetProperty("records").GetArrayLength());
    }

    [Fact]
    public async Task Without_an_exited_record_an_attempt_that_is_no_longer_current_is_unfinished()
    {
        var (s, root, a, _) = await Plan("k-abort", "att-abort");
        Applied(await s.TransitionAsync(a, WorkStatus.Done, Ctx(K(), await Rev(s, root, a), "worker"), "att-abort", 1, "sha-1"));
        var run = RunDir();
        Files(run, Records(1));
        await Journal(root, "k-abort", "active", ("launch_intent", Launch(run)));
        var (_, body) = await Get(s, a, "att-abort");
        Assert.Equal(("unfinished", "unverified"), (S(body, "status"), S(body, "integrity")));
    }

    [Fact]
    public async Task A_write_error_or_incomplete_final_stays_unverified_and_a_hash_mismatch_is_refused()
    {
        var (s, root, a, _) = await Plan("k-we", "att-we");
        var run = RunDir();
        var (prompt, trace) = Files(run, Records(2));
        await Journal(root, "k-we", "resolved", ("launch_intent", Launch(run)), ("exited", Exited(prompt, trace, 2, complete: false, writeError: true)));
        var (_, body) = await Get(s, a, "att-we");
        Assert.Equal(("exited", "unverified"), (S(body, "status"), S(body, "integrity")));

        var (s2, root2, a2, _) = await Plan("k-bad", "att-bad");
        var run2 = RunDir("run-2");
        var (p2, t2) = Files(run2, Records(2));
        await Journal(root2, "k-bad", "resolved", ("launch_intent", Launch(run2)), ("exited", Exited(p2, t2, 2, sha: new string('0', 64))));
        var (status, err) = await Get(s2, a2, "att-bad");
        Assert.Equal((409, AttemptTraceCodes.IntegrityMismatch), (status, S(err, "code")));
    }

    [Fact]
    public async Task Older_runs_compacted_journals_and_unclaimed_attempts_are_not_captured_with_their_reason()
    {
        var (s, root, a, b) = await Plan("k-old", "att-old");
        await Journal(root, "k-old", "resolved", ("launch_intent", """{"baseRef":"abc"}"""), ("exited", """{"code":0,"reason":"exit"}"""));
        var (_, old) = await Get(s, a, "att-old");
        Assert.Equal(("not_captured", "no_trace_block", "none"), (S(old, "status"), S(old, "reason"), S(old, "integrity")));
        Assert.Equal(0, old.GetProperty("records").GetArrayLength());

        var (s2, root2, a2, _) = await Plan("k-cmp", "att-cmp");
        await Journal(root2, "k-cmp", "compacted");
        await db.Exec("INSERT INTO supervisor_journal.summaries (root, claim_key, writer_id, resolved_at, summary, head_hash) VALUES (@r, 'k-cmp', 'w', 1, '{}', 'h')",
            ("r", root2.ToString("D")));
        var (_, cmp) = await Get(s2, a2, "att-cmp");
        Assert.Equal(("not_captured", "journal_compacted"), (S(cmp, "status"), S(cmp, "reason")));

        Applied(await s.TransitionAsync(b, WorkStatus.InProgress, Ctx(K(), await Rev(s, root, b), "worker"), "att-plain", null, null, null));
        var (_, plain) = await Get(s, b, "att-plain");
        Assert.Equal(("not_captured", "no_claim_receipt"), (S(plain, "status"), S(plain, "reason")));
    }

    [Fact]
    public async Task Unknown_nodes_and_attempts_are_404_and_two_receipts_for_one_attempt_conflict()
    {
        var (s, root, a, _) = await Plan("k-id", "att-id");
        Assert.Equal((404, PlanErrorCodes.NodeNotFound), Code(await Get(s, Guid.NewGuid(), "att-id")));
        Assert.Equal((404, AttemptTraceCodes.AttemptNotFound), Code(await Get(s, a, "att-other")));

        // A second claimed receipt naming the same attempt (planted under the write fence).
        await db.Exec("""
            BEGIN;
            SET LOCAL hekate.plan_contract = 'on';
            INSERT INTO public.plan_claim_receipts (root_node_id, claim_key, request_fingerprint, outcome, node_id, attempt_id, attempt_epoch,
                executor_ref, content_revision, content_digest, content_snapshot, prereq_digest, prereq_snapshot, event_seq, actor, created_at)
            SELECT root_node_id, 'k-id-2', request_fingerprint, outcome, node_id, attempt_id, attempt_epoch, executor_ref, content_revision,
                content_digest, content_snapshot, prereq_digest, prereq_snapshot, event_seq, actor, created_at
            FROM public.plan_claim_receipts WHERE root_node_id = @r AND claim_key = 'k-id';
            COMMIT;
            """, ("r", root));
        Assert.Equal((409, AttemptTraceCodes.IdentityConflict), Code(await Get(s, a, "att-id")));
    }

    [Fact]
    public async Task Paths_outside_the_root_are_refused_absent_files_are_missing_and_an_unset_root_is_503()
    {
        var (s, root, a, _) = await Plan("k-out", "att-out");
        var outside = Directory.CreateDirectory(Path.Combine(Path.GetTempPath(), "hekate-trace-outside-" + Guid.NewGuid().ToString("N"))).FullName;
        try
        {
            Files(outside, Records(1));
            await Journal(root, "k-out", "active", ("launch_intent", Launch(outside)));
            Assert.Equal((409, AttemptTraceCodes.PathRefused), Code(await Get(s, a, "att-out")));
            Assert.Equal((503, AttemptTraceCodes.RootNotConfigured), Code(await Get(s, a, "att-out", root: null)));
        }
        finally { Directory.Delete(outside, true); }

        var (s2, root2, a2, _) = await Plan("k-miss", "att-miss");
        await Journal(root2, "k-miss", "active", ("launch_intent", Launch(Path.Combine(_traceRoot, "never-created"))));
        var (status, body) = await Get(s2, a2, "att-miss");
        Assert.Equal((200, "missing", "none"), (status, S(body, "status"), S(body, "integrity")));
    }

    [Fact]
    public async Task A_database_without_a_supervisor_journal_is_503_not_an_empty_trace()
    {
        var bare = new LiveDatabase();
        await bare.InitializeAsync();
        try
        {
            var s = new Store(bare.ConnectionString);
            var root = Guid.NewGuid();
            var a = Guid.NewGuid();
            Applied(await s.CreatePlanAsync(root, bare.ProjectId, "Bare", PlanContent.Empty, GatePolicy.Accepted, Ctx(K(), 0)));
            Applied(await s.AddChildAsync(root, a, "task", "A", 0, PlanContent.Empty, Ctx(K(), await Rev(s, root, root))));
            Assert.True((await s.ClaimAsync(root, "k-bare", "att-bare", null, "worker")).Ok);
            Assert.Equal((503, AttemptTraceCodes.JournalUnavailable), Code(await Get(s, a, "att-bare")));
        }
        finally { await bare.DisposeAsync(); }
    }

    [Theory]
    [InlineData(AttemptTraceCodes.AttemptNotFound, 404)]
    [InlineData(AttemptTraceCodes.IdentityConflict, 409)]
    [InlineData(AttemptTraceCodes.PathRefused, 409)]
    [InlineData(AttemptTraceCodes.IntegrityMismatch, 409)]
    [InlineData(AttemptTraceCodes.RecordTooLarge, 409)]
    [InlineData(AttemptTraceCodes.RootNotConfigured, 503)]
    [InlineData(AttemptTraceCodes.JournalUnavailable, 503)]
    public void Trace_codes_map_to_their_statuses(string code, int status) => Assert.Equal(status, PlanContractEndpoints.StatusFor(code));

    private static (int, string?) Code((int Status, JsonElement Body) r) => (r.Status, S(r.Body, "code"));
}
