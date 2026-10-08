// CodeStoragePoc - Attempt trace v0: pure file, path and paging rules (plan 047; capture: plan 046)
//
// A supervised worker's retained trace is two files in its run directory: the exact prompt
// bytes and one ASCII-escaped JSON record per line ({seq, tMs, stream, text, cut, redacted},
// seq contiguous from 0). The journal's launch_intent names them relative to runDir before
// spawn; exited adds the final bytes/sha256 when it is written. This file holds everything
// that needs no database: ref parsing, confinement of a file under HEKATE_TRACE_ROOT
// (symlinks and reparse points refused at every component), integrity of the retained bytes,
// and one bounded page of records. Nothing here writes.
//
// Depends on: System.Text.Json, System.Security.Cryptography
// Used by:    Api/PlanContractEndpoints, PlanContracts.Tests/AttemptTraceTests

using System.Security.Cryptography;
using System.Text.Json;

namespace CodeStoragePoc.PlanContracts;

/// <summary>Trace error codes (trace contract v0). Uppercase as agreed with the UI contract.</summary>
public static class AttemptTraceCodes
{
    public const string AttemptNotFound = "ATTEMPT_NOT_FOUND";
    public const string IdentityConflict = "TRACE_IDENTITY_CONFLICT";
    public const string PathRefused = "TRACE_PATH_REFUSED";
    public const string IntegrityMismatch = "TRACE_INTEGRITY_MISMATCH";
    public const string RecordTooLarge = "TRACE_RECORD_TOO_LARGE";
    public const string FileTooLarge = "TRACE_FILE_TOO_LARGE";
    public const string Invalid = "TRACE_INVALID";
    public const string RootNotConfigured = "TRACE_ROOT_NOT_CONFIGURED";
    public const string JournalUnavailable = "TRACE_JOURNAL_UNAVAILABLE";
}

/// <summary>launch_intent.data.trace: where the attempt's files are, written before spawn.</summary>
public sealed record TraceRef(string ExecutionKind, string RunDir, string PromptName, string TraceName);

/// <summary>exited.data.trace plus the exit facts: final retained bytes/hashes when written.</summary>
public sealed record TraceFinal(bool Complete, long PromptBytes, string PromptSha256, long TraceBytes, string TraceSha256,
    long Records, bool Capped, bool WriteError);

public sealed record TraceExit(long? Code, string? KillReason);

public sealed record TraceRecord(long Seq, [property: System.Text.Json.Serialization.JsonPropertyName("tMs")] long TMs,
    string Stream, string Text, bool Cut, bool Redacted);

/// <summary>One page: the records, the cursor for more complete records, or a typed refusal.</summary>
public sealed record TracePage(IReadOnlyList<TraceRecord> Records, long? NextAfterSeq, string? ErrorCode, string? ErrorMessage);

public enum TracePathKind { Ok, Missing, Refused }

public sealed record TracePath(TracePathKind Kind, string? FullPath, string? Reason);

public static class AttemptTrace
{
    public const string Version = "hekate-attempt-trace.v0";
    public const string RootVariable = "HEKATE_TRACE_ROOT";
    /// <summary>Serialized UTF-8 budget for one trace response, header and prompt included.</summary>
    public const int ResponseBudgetBytes = 4 * 1024 * 1024;
    /// <summary>A retained file larger than this is refused rather than read.</summary>
    public const long MaxFileBytes = 64L * 1024 * 1024;
    /// <summary>Largest journal data payload read for a launch_intent or exited record.</summary>
    public const int MaxJournalDataChars = 64 * 1024;

    private static readonly string[] Streams = ["stdout", "stderr", "hekate"];
    private static readonly StringComparison PathComparison =
        OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;

    /// <summary>The same serializer settings the Api uses: camelCase names, default escaping, nulls written.</summary>
    public static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase };

    // -----------------------------------------------------------------------
    // Journal payloads
    // -----------------------------------------------------------------------

    /// <summary>
    /// Parse launch_intent.data. Returns (null, null) when there is no trace block (an older run);
    /// (null, message) when a block exists but is malformed or of another version.
    /// </summary>
    public static (TraceRef? Ref, string? Error) ParseRef(string launchDataJson)
    {
        try
        {
            using var doc = JsonDocument.Parse(launchDataJson);
            if (doc.RootElement.ValueKind != JsonValueKind.Object) return (null, "launch_intent data is not an object.");
            if (!doc.RootElement.TryGetProperty("trace", out var t)) return (null, null);
            if (t.ValueKind != JsonValueKind.Object) return (null, "launch_intent trace is not an object.");
            if (Str(t, "version") != Version) return (null, $"Unsupported trace version '{Str(t, "version")}'.");
            var kind = Str(t, "executionKind");
            var runDir = Str(t, "runDir");
            var prompt = Str(t, "prompt");
            var trace = Str(t, "trace");
            if (kind is null || runDir is null || prompt is null || trace is null) return (null, "launch_intent trace is missing a field.");
            return (new TraceRef(kind, runDir, prompt, trace), null);
        }
        catch (JsonException) { return (null, "launch_intent data is not valid JSON."); }
    }

    /// <summary>
    /// Parse exited.data. Final is null when the record has no trace block; Exit is always read
    /// (code may be null, e.g. spawn_failed). Error is set for a malformed block.
    /// </summary>
    public static (TraceFinal? Final, TraceExit? Exit, string? Error) ParseExited(string exitedDataJson)
    {
        try
        {
            using var doc = JsonDocument.Parse(exitedDataJson);
            var d = doc.RootElement;
            if (d.ValueKind != JsonValueKind.Object) return (null, null, "exited data is not an object.");
            long? code = d.TryGetProperty("code", out var c) && c.ValueKind == JsonValueKind.Number && c.TryGetInt64(out var cv) ? cv : null;
            var reason = Str(d, "reason");
            var exit = new TraceExit(code, reason is null or "exit" ? null : reason);
            if (!d.TryGetProperty("trace", out var t)) return (null, exit, null);
            if (t.ValueKind != JsonValueKind.Object || Bool(t, "complete") is not bool complete
                || !t.TryGetProperty("prompt", out var p) || p.ValueKind != JsonValueKind.Object
                || !t.TryGetProperty("trace", out var f) || f.ValueKind != JsonValueKind.Object
                || Bool(f, "capped") is not bool capped || Bool(f, "writeError") is not bool writeError)
                return (null, exit, "exited trace block is malformed.");
            // An incomplete final is never verified, so its sizes and hashes may be absent (e.g. the
            // prompt was never written: capture records an empty prompt block). A complete one needs all.
            if (Long(p, "bytes") is long pb && Str(p, "sha256") is string ps && Long(f, "bytes") is long tb
                && Str(f, "sha256") is string ts && Long(f, "records") is long recs)
                return (new TraceFinal(complete, pb, ps, tb, ts, recs, capped, writeError), exit, null);
            if (complete) return (null, exit, "exited trace block is complete but lacks its sizes or hashes.");
            return (new TraceFinal(false, -1, "", -1, "", -1, capped, writeError), exit, null);
        }
        catch (JsonException) { return (null, null, "exited data is not valid JSON."); }
    }

    // -----------------------------------------------------------------------
    // Confinement
    // -----------------------------------------------------------------------

    /// <summary>
    /// Resolve runDir/name under root. The name must be a bare file name. The lexical result must
    /// lie strictly inside root, and every existing component below root (directories and the
    /// file) must be a real entry: a symlink, junction or other reparse point is refused, and the
    /// leaf must be a regular file. A path whose leaf (or a parent) does not exist yet is Missing.
    /// </summary>
    public static TracePath Confine(string? root, string runDir, string name)
    {
        if (string.IsNullOrWhiteSpace(root)) return Refuse($"{RootVariable} is not configured.");
        if (name.Length == 0 || name is "." or ".." || name != Path.GetFileName(name)
            || name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || name.Contains(':'))
            return Refuse("The trace file name is not a bare file name.");
        if (!Path.IsPathFullyQualified(runDir)) return Refuse("The run directory is not an absolute path.");

        string rootFull, full;
        try
        {
            rootFull = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
            full = Path.GetFullPath(Path.Combine(runDir, name));
        }
        catch (Exception e) when (e is ArgumentException or NotSupportedException or PathTooLongException)
        {
            return Refuse("The trace path cannot be resolved.");
        }
        if (!Directory.Exists(rootFull)) return Refuse($"{RootVariable} is not an existing directory.");
        if (!full.StartsWith(rootFull + Path.DirectorySeparatorChar, PathComparison)) return Refuse("The trace path is outside the runs root.");

        var parts = Path.GetRelativePath(rootFull, full).Split(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        var current = rootFull;
        for (var i = 0; i < parts.Length; i++)
        {
            if (parts[i] is "" or "." or "..") return Refuse("The trace path is outside the runs root.");
            current = Path.Combine(current, parts[i]);
            var last = i == parts.Length - 1;
            if (!File.Exists(current) && !Directory.Exists(current)) return new TracePath(TracePathKind.Missing, full, null);
            var attributes = File.GetAttributes(current);
            if ((attributes & FileAttributes.ReparsePoint) != 0) return Refuse("The trace path passes through a link or reparse point.");
            var isDirectory = (attributes & FileAttributes.Directory) != 0;
            if (last && isDirectory) return Refuse("The trace path is a directory, not a file.");
            if (!last && !isDirectory) return Refuse("A parent of the trace path is not a directory.");
        }
        return new TracePath(TracePathKind.Ok, full, null);
    }

    private static TracePath Refuse(string reason) => new(TracePathKind.Refused, null, reason);

    /// <summary>Read a confined file, sharing with a writer that may still be appending. Null when too large.</summary>
    public static byte[]? ReadBounded(string fullPath)
    {
        using var fs = new FileStream(fullPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        using var ms = new MemoryStream();
        var buffer = new byte[81920];
        int n;
        while ((n = fs.Read(buffer, 0, buffer.Length)) > 0)
        {
            if (ms.Length + n > MaxFileBytes) return null;
            ms.Write(buffer, 0, n);
        }
        return ms.ToArray();
    }

    // -----------------------------------------------------------------------
    // Integrity and paging
    // -----------------------------------------------------------------------

    public static string Sha256Hex(byte[] bytes) => Convert.ToHexStringLower(SHA256.HashData(bytes));

    /// <summary>The retained bytes equal what the exited record finalized (sizes and sha256).</summary>
    public static bool Matches(TraceFinal final, byte[] prompt, byte[] trace) =>
        final.PromptBytes == prompt.LongLength && final.TraceBytes == trace.LongLength
        && string.Equals(final.PromptSha256, Sha256Hex(prompt), StringComparison.OrdinalIgnoreCase)
        && string.Equals(final.TraceSha256, Sha256Hex(trace), StringComparison.OrdinalIgnoreCase);

    /// <summary>Serialized size of one record in the response, including its separating comma.</summary>
    public static int SerializedSize(TraceRecord r) => JsonSerializer.SerializeToUtf8Bytes(r, Json).Length + 1;

    /// <summary>
    /// Records after afterSeq (all when null), at most limit of them and at most budget serialized
    /// bytes. Only newline-terminated lines count: a partial line still being written is not read.
    /// Each line's seq must equal its line index. A first record that alone exceeds the budget is a
    /// typed refusal, never an empty page with a cursor that cannot advance.
    /// </summary>
    public static TracePage ReadPage(byte[] content, long? afterSeq, int limit, long budget)
    {
        var records = new List<TraceRecord>();
        long used = 0, index = -1, start = 0;
        long? next = null;
        for (var nl = Array.IndexOf(content, (byte)'\n'); nl >= 0; start = nl + 1, nl = Array.IndexOf(content, (byte)'\n', (int)start))
        {
            index++;
            if (afterSeq is long a && index <= a) continue;
            if (records.Count == limit) { next = records[^1].Seq; break; }
            var parsed = ParseRecord(content.AsSpan((int)start, (int)(nl - start)), index, out var error);
            if (parsed is null) return new TracePage([], null, AttemptTraceCodes.Invalid, error);
            var size = SerializedSize(parsed);
            if (used + size > budget)
            {
                if (records.Count == 0)
                    return new TracePage([], null, AttemptTraceCodes.RecordTooLarge,
                        $"Trace record {index} is larger than the response budget and cannot be served.");
                next = records[^1].Seq;
                break;
            }
            used += size;
            records.Add(parsed);
        }
        return new TracePage(records, next, null, null);
    }

    private static TraceRecord? ParseRecord(ReadOnlySpan<byte> line, long expectedSeq, out string? error)
    {
        error = null;
        try
        {
            using var doc = JsonDocument.Parse(line.ToArray());
            var o = doc.RootElement;
            if (o.ValueKind == JsonValueKind.Object
                && Long(o, "seq") is long seq && Long(o, "tMs") is long tMs && tMs >= 0
                && Str(o, "stream") is string stream && Streams.Contains(stream)
                && Str(o, "text") is string text && Bool(o, "cut") is bool cut && Bool(o, "redacted") is bool redacted)
            {
                if (seq != expectedSeq) { error = $"Trace record at line {expectedSeq} has seq {seq}."; return null; }
                return new TraceRecord(seq, tMs, stream, text, cut, redacted);
            }
        }
        catch (JsonException) { }
        error ??= $"Trace record at line {expectedSeq} is malformed.";
        return null;
    }

    private static string? Str(JsonElement o, string k) => o.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
    private static bool? Bool(JsonElement o, string k) =>
        o.TryGetProperty(k, out var v) && v.ValueKind is JsonValueKind.True or JsonValueKind.False ? v.GetBoolean() : null;
    private static long? Long(JsonElement o, string k) =>
        o.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number && v.TryGetInt64(out var n) ? n : null;
}
