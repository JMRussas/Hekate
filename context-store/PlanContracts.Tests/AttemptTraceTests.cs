using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Xunit;

namespace CodeStoragePoc.PlanContracts.Tests;

/// <summary>Plan 047: pure attempt-trace rules — refs, confinement, integrity and paging. Temp files only.</summary>
public sealed class AttemptTraceTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), "hekate-trace-tests-" + Guid.NewGuid().ToString("N"));
    private readonly string _run;
    private readonly List<string> _links = [];

    public AttemptTraceTests()
    {
        _run = Path.Combine(_root, "plan", "pilot-1");
        Directory.CreateDirectory(_run);
    }

    public void Dispose()
    {
        foreach (var link in _links) Directory.Delete(link);   // removes the link itself, never its target's contents
        Directory.Delete(_root, true);
    }

    private static string Line(long seq, string text = "x", string stream = "stdout", long tMs = 0) =>
        JsonSerializer.Serialize(new { seq, tMs, stream, text, cut = false, redacted = false }) + "\n";

    private static byte[] Lines(int n, Func<int, string>? text = null) =>
        Encoding.ASCII.GetBytes(string.Concat(Enumerable.Range(0, n).Select(i => Line(i, text?.Invoke(i) ?? $"r{i}"))));

    // --- refs -----------------------------------------------------------------

    [Fact]
    public void A_launch_intent_without_a_trace_block_is_an_older_run_not_an_error()
    {
        Assert.Equal((null, null), AttemptTrace.ParseRef("""{"baseRef":"abc","command":"claude -p"}"""));
    }

    [Fact]
    public void A_v0_trace_block_is_read_and_other_versions_or_missing_fields_are_errors()
    {
        var (r, e) = AttemptTrace.ParseRef("""{"trace":{"version":"hekate-attempt-trace.v0","executionKind":"claude-cli","runDir":"D:/runs/x","prompt":"attempt-r1.prompt.txt","trace":"attempt-r1.trace.jsonl"}}""");
        Assert.Null(e);
        Assert.Equal(new TraceRef("claude-cli", "D:/runs/x", "attempt-r1.prompt.txt", "attempt-r1.trace.jsonl"), r);
        Assert.NotNull(AttemptTrace.ParseRef("""{"trace":{"version":"hekate-attempt-trace.v1","executionKind":"k","runDir":"d","prompt":"p","trace":"t"}}""").Error);
        Assert.NotNull(AttemptTrace.ParseRef("""{"trace":{"version":"hekate-attempt-trace.v0","executionKind":"k","runDir":"d","prompt":"p"}}""").Error);
        Assert.NotNull(AttemptTrace.ParseRef("not json").Error);
    }

    [Fact]
    public void Exited_gives_the_exit_facts_and_a_final_block_only_when_present()
    {
        var (f, x, e) = AttemptTrace.ParseExited("""{"code":0,"reason":"exit","trace":{"complete":true,"prompt":{"bytes":3,"sha256":"aa"},"trace":{"bytes":9,"sha256":"bb","records":2,"capped":false,"writeError":false}}}""");
        Assert.Null(e);
        Assert.Equal(new TraceExit(0, null), x);
        Assert.Equal(new TraceFinal(true, 3, "aa", 9, "bb", 2, false, false), f);
        var killed = AttemptTrace.ParseExited("""{"code":null,"reason":"inactivity"}""");
        Assert.Equal((null, new TraceExit(null, "inactivity"), null), killed);
        Assert.NotNull(AttemptTrace.ParseExited("""{"code":1,"reason":"exit","trace":{"complete":"yes"}}""").Error);
    }

    // --- confinement ------------------------------------------------------------

    [Fact]
    public void A_file_inside_the_root_is_ok_and_an_absent_one_is_missing()
    {
        File.WriteAllText(Path.Combine(_run, "attempt-r1.trace.jsonl"), "");
        var ok = AttemptTrace.Confine(_root, _run, "attempt-r1.trace.jsonl");
        Assert.Equal(TracePathKind.Ok, ok.Kind);
        Assert.Equal(Path.Combine(_run, "attempt-r1.trace.jsonl"), ok.FullPath);
        Assert.Equal(TracePathKind.Missing, AttemptTrace.Confine(_root, _run, "attempt-r2.trace.jsonl").Kind);
        Assert.Equal(TracePathKind.Missing, AttemptTrace.Confine(_root, Path.Combine(_root, "absent-dir"), "t.jsonl").Kind);
    }

    [Theory]
    [InlineData("../escape.jsonl")]
    [InlineData("..")]
    [InlineData("sub/t.jsonl")]
    [InlineData("sub\\t.jsonl")]
    [InlineData("t.jsonl:stream")]
    [InlineData("")]
    public void Names_that_are_not_bare_file_names_are_refused(string name) =>
        Assert.Equal(TracePathKind.Refused, AttemptTrace.Confine(_root, _run, name).Kind);

    [Fact]
    public void Paths_outside_the_root_relative_run_dirs_and_an_unset_root_are_refused()
    {
        var outside = Path.Combine(Path.GetTempPath(), "hekate-trace-outside-" + Guid.NewGuid().ToString("N"));
        Assert.Equal(TracePathKind.Refused, AttemptTrace.Confine(_root, outside, "t.jsonl").Kind);
        Assert.Equal(TracePathKind.Refused, AttemptTrace.Confine(_root, _root + "-sibling", "t.jsonl").Kind);   // a prefix, not a parent
        Assert.Equal(TracePathKind.Refused, AttemptTrace.Confine(_root, "relative/run", "t.jsonl").Kind);
        Assert.Equal(TracePathKind.Refused, AttemptTrace.Confine(null, _run, "t.jsonl").Kind);
        Assert.Equal(TracePathKind.Refused, AttemptTrace.Confine(Path.Combine(_root, "no-such-root"), _run, "t.jsonl").Kind);
    }

    [Fact]
    public void A_directory_where_the_file_should_be_is_refused()
    {
        Directory.CreateDirectory(Path.Combine(_run, "attempt-r1.trace.jsonl"));
        Assert.Equal(TracePathKind.Refused, AttemptTrace.Confine(_root, _run, "attempt-r1.trace.jsonl").Kind);
    }

    [Fact]
    public void A_link_in_a_parent_or_as_the_leaf_is_refused_even_when_it_points_inside_the_root()
    {
        var target = Path.Combine(_root, "real");
        Directory.CreateDirectory(target);
        File.WriteAllText(Path.Combine(target, "t.jsonl"), "");
        var link = Path.Combine(_root, "linked");
        CreateDirectoryLink(link, target);
        Assert.Equal(TracePathKind.Ok, AttemptTrace.Confine(_root, target, "t.jsonl").Kind);
        Assert.Equal(TracePathKind.Refused, AttemptTrace.Confine(_root, link, "t.jsonl").Kind);
        var other = Path.Combine(_root, "other");
        Directory.CreateDirectory(other);
        CreateDirectoryLink(Path.Combine(target, "leaf.jsonl"), other);
        Assert.Equal(TracePathKind.Refused, AttemptTrace.Confine(_root, target, "leaf.jsonl").Kind);
    }

    /// <summary>A junction on Windows (no privilege needed), a symbolic link elsewhere; removed in Dispose.</summary>
    private void CreateDirectoryLink(string link, string target)
    {
        _links.Add(link);
        if (!OperatingSystem.IsWindows()) { Directory.CreateSymbolicLink(link, target); return; }
        using var p = Process.Start(new ProcessStartInfo("cmd.exe") { ArgumentList = { "/c", "mklink", "/J", link, target }, RedirectStandardOutput = true, UseShellExecute = false })!;
        p.WaitForExit();
        Assert.Equal(0, p.ExitCode);
        Assert.True((File.GetAttributes(link) & FileAttributes.ReparsePoint) != 0);
    }

    // --- integrity ------------------------------------------------------------------

    [Fact]
    public void Retained_bytes_match_only_with_equal_sizes_and_hashes()
    {
        byte[] prompt = "do it"u8.ToArray(), trace = Lines(2);
        var final = new TraceFinal(true, prompt.Length, AttemptTrace.Sha256Hex(prompt), trace.Length, AttemptTrace.Sha256Hex(trace), 2, false, false);
        Assert.True(AttemptTrace.Matches(final, prompt, trace));
        Assert.True(AttemptTrace.Matches(final with { TraceSha256 = final.TraceSha256.ToUpperInvariant() }, prompt, trace));
        Assert.False(AttemptTrace.Matches(final, prompt, [.. trace, (byte)'\n']));
        Assert.False(AttemptTrace.Matches(final with { PromptSha256 = new string('0', 64) }, prompt, trace));
    }

    // --- paging -------------------------------------------------------------------------

    [Fact]
    public void Pages_follow_the_limit_and_cursor_and_the_last_page_has_no_cursor()
    {
        var content = Lines(5);
        var first = AttemptTrace.ReadPage(content, null, 2, AttemptTrace.ResponseBudgetBytes);
        Assert.Equal([0L, 1L], first.Records.Select(r => r.Seq));
        Assert.Equal(1, first.NextAfterSeq);
        var second = AttemptTrace.ReadPage(content, 1, 2, AttemptTrace.ResponseBudgetBytes);
        Assert.Equal([2L, 3L], second.Records.Select(r => r.Seq));
        var last = AttemptTrace.ReadPage(content, 3, 2, AttemptTrace.ResponseBudgetBytes);
        Assert.Equal([4L], last.Records.Select(r => r.Seq));
        Assert.Null(last.NextAfterSeq);
        var beyond = AttemptTrace.ReadPage(content, 4, 2, AttemptTrace.ResponseBudgetBytes);
        Assert.Empty(beyond.Records);
        Assert.Null(beyond.NextAfterSeq);
        Assert.Null(beyond.ErrorCode);
    }

    [Fact]
    public void A_partial_last_line_still_being_written_is_not_read()
    {
        var content = Encoding.ASCII.GetBytes(Line(0) + Line(1)[..10]);
        var page = AttemptTrace.ReadPage(content, null, 200, AttemptTrace.ResponseBudgetBytes);
        Assert.Equal([0L], page.Records.Select(r => r.Seq));
        Assert.Null(page.NextAfterSeq);
    }

    [Fact]
    public void A_seq_that_is_not_its_line_index_or_a_malformed_line_is_invalid()
    {
        var gap = Encoding.ASCII.GetBytes(Line(0) + Line(2));
        Assert.Equal(AttemptTraceCodes.Invalid, AttemptTrace.ReadPage(gap, null, 200, AttemptTrace.ResponseBudgetBytes).ErrorCode);
        var bad = Encoding.ASCII.GetBytes(Line(0) + "{\"seq\":1}\n");
        Assert.Equal(AttemptTraceCodes.Invalid, AttemptTrace.ReadPage(bad, null, 200, AttemptTrace.ResponseBudgetBytes).ErrorCode);
        var stream = Encoding.ASCII.GetBytes(Line(0, stream: "stdin"));
        Assert.Equal(AttemptTraceCodes.Invalid, AttemptTrace.ReadPage(stream, null, 200, AttemptTrace.ResponseBudgetBytes).ErrorCode);
    }

    [Fact]
    public void The_budget_ends_a_page_early_with_a_cursor_that_advances()
    {
        var content = Lines(4, _ => new string('a', 100));
        var one = AttemptTrace.SerializedSize(new TraceRecord(0, 0, "stdout", new string('a', 100), false, false));
        var page = AttemptTrace.ReadPage(content, null, 200, one * 2 + 1);
        Assert.Equal([0L, 1L], page.Records.Select(r => r.Seq));
        Assert.Equal(1, page.NextAfterSeq);
    }

    [Fact]
    public void A_single_record_larger_than_the_budget_is_a_typed_refusal_not_an_empty_page()
    {
        var content = Encoding.ASCII.GetBytes(Line(0) + Line(1, new string('z', 5000)));
        var page = AttemptTrace.ReadPage(content, 0, 200, 1000);
        Assert.Equal(AttemptTraceCodes.RecordTooLarge, page.ErrorCode);
        Assert.Empty(page.Records);
        Assert.Null(page.NextAfterSeq);
    }

    [Fact]
    public void Records_serialize_with_the_contract_field_names_and_escaped_text_round_trips()
    {
        var content = Encoding.ASCII.GetBytes(JsonSerializer.Serialize(new { seq = 0, tMs = 7, stream = "stderr", text = "caf\u00e9 <b>", cut = true, redacted = true }) + "\n");
        var r = Assert.Single(AttemptTrace.ReadPage(content, null, 200, AttemptTrace.ResponseBudgetBytes).Records);
        Assert.Equal(new TraceRecord(0, 7, "stderr", "caf\u00e9 <b>", true, true), r);
        using var doc = JsonDocument.Parse(JsonSerializer.SerializeToUtf8Bytes(r, AttemptTrace.Json));
        Assert.Equal(["seq", "tMs", "stream", "text", "cut", "redacted"], doc.RootElement.EnumerateObject().Select(p => p.Name));
    }
}
