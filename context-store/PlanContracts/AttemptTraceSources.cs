// CodeStoragePoc - Attempt trace v0: identity binding and journal reads (plan 047; capture: plan 046)
//
// Resolves (node, attemptId) to the journal records that locate its trace, in ONE read-only
// repeatable-read transaction:
//   node -> root and current attempt (plan_node_state);
//   attempt -> its attempt_started events (epochs, claim keys);
//   attempt -> exactly one claimed receipt (plan_claim_receipts), whose epoch must be one of
//              the started epochs and whose claim key must equal every event claim key;
//   (root, claim key) -> launch_intent / exited in supervisor_journal.records (text root).
// Every query is parameterized and row-bounded; journal payloads are length-checked in SQL
// before they are read. Disagreeing identities are a conflict, never a guess. Nothing writes.
//
// Depends on: Npgsql, PlanStoreErrorCodes, PlanErrorCodes, AttemptTrace
// Used by:    PlanStore.ReadAttemptTraceSourceAsync, PlanStore.LiveTests/AttemptTraceLiveTests

using Npgsql;

namespace CodeStoragePoc.PlanContracts;

/// <summary>What the stores say about one attempt's trace; file reads happen later.</summary>
public sealed record AttemptTraceSource(
    Guid RootId, Guid NodeId, string AttemptId, long AttemptEpoch, string? ClaimKey,
    string? NotCapturedReason, string? LaunchDataJson, string? ExitedDataJson, bool CurrentInProgress);

public sealed record AttemptTraceSourceResult(AttemptTraceSource? Source, string? ErrorCode, string? ErrorMessage)
{
    internal static AttemptTraceSourceResult Fail(string code, string message) => new(null, code, message);
}

public static class AttemptTraceSources
{
    public const string NoTraceBlock = "no_trace_block";
    public const string NoClaimReceipt = "no_claim_receipt";
    public const string JournalCompacted = "journal_compacted";
    public const int MaxAttemptIdLength = 256;

    public static async Task<AttemptTraceSourceResult> ReadAsync(string connectionString, Guid nodeId, string attemptId)
    {
        if (attemptId.Length is 0 or > MaxAttemptIdLength)
            return AttemptTraceSourceResult.Fail(PlanStoreErrorCodes.InvalidInput, $"attemptId must be 1-{MaxAttemptIdLength} characters.");
        await using var conn = new NpgsqlConnection(connectionString);
        await conn.OpenAsync();
        await using var tx = await conn.BeginTransactionAsync(System.Data.IsolationLevel.RepeatableRead);
        try { return await Read(conn, tx, nodeId, attemptId); }
        finally { await tx.RollbackAsync(); }
    }

    private static async Task<AttemptTraceSourceResult> Read(NpgsqlConnection conn, NpgsqlTransaction tx, Guid nodeId, string attemptId)
    {
        Guid root;
        string? currentAttempt;
        long currentEpoch;
        string work;
        await using (var cmd = new NpgsqlCommand(
            "SELECT root_node_id, attempt_id, attempt_epoch, work_status FROM public.plan_node_state WHERE node_id = @n", conn, tx))
        {
            cmd.Parameters.AddWithValue("n", nodeId);
            await using var r = await cmd.ExecuteReaderAsync();
            if (!await r.ReadAsync()) return AttemptTraceSourceResult.Fail(PlanErrorCodes.NodeNotFound, $"Node {nodeId} is not part of a managed plan.");
            root = r.GetGuid(0);
            currentAttempt = r.IsDBNull(1) ? null : r.GetString(1);
            currentEpoch = r.GetInt64(2);
            work = r.GetString(3);
        }

        var started = new List<(long Epoch, string? ClaimKey)>();
        await using (var cmd = new NpgsqlCommand("""
            SELECT attempt_epoch, claim_key FROM public.plan_attempt_events
            WHERE root_node_id = @r AND node_id = @n AND attempt_id = @a AND kind = 'attempt_started'
            ORDER BY seq LIMIT 16
            """, conn, tx))
        {
            cmd.Parameters.AddWithValue("r", root);
            cmd.Parameters.AddWithValue("n", nodeId);
            cmd.Parameters.AddWithValue("a", attemptId);
            await using var r = await cmd.ExecuteReaderAsync();
            while (await r.ReadAsync()) started.Add((r.GetInt64(0), r.IsDBNull(1) ? null : r.GetString(1)));
        }
        if (started.Count == 0) return AttemptTraceSourceResult.Fail(AttemptTraceCodes.AttemptNotFound, $"Attempt '{attemptId}' is not recorded for node {nodeId}.");

        var receipts = new List<(string ClaimKey, long Epoch)>();
        await using (var cmd = new NpgsqlCommand("""
            SELECT claim_key, attempt_epoch FROM public.plan_claim_receipts
            WHERE root_node_id = @r AND node_id = @n AND attempt_id = @a AND outcome = 'claimed'
            ORDER BY claim_key LIMIT 2
            """, conn, tx))
        {
            cmd.Parameters.AddWithValue("r", root);
            cmd.Parameters.AddWithValue("n", nodeId);
            cmd.Parameters.AddWithValue("a", attemptId);
            await using var r = await cmd.ExecuteReaderAsync();
            while (await r.ReadAsync()) receipts.Add((r.GetString(0), r.GetInt64(1)));
        }

        var eventKeys = started.Select(s => s.ClaimKey).Where(k => k is not null).Distinct().ToList();
        var inProgress = currentAttempt == attemptId && work == "in_progress";
        if (receipts.Count == 0)
        {
            // An event that names a claim key without its receipt is contradictory, not merely old.
            if (eventKeys.Count > 0) return Conflict("The attempt's start names a claim key that has no claimed receipt.");
            return Found(new AttemptTraceSource(root, nodeId, attemptId, started[^1].Epoch, null, NoClaimReceipt, null, null, false));
        }
        if (receipts.Count > 1) return Conflict("More than one claimed receipt names this attempt.");
        var (claimKey, epoch) = receipts[0];
        if (!started.Any(s => s.Epoch == epoch)) return Conflict("The claimed receipt's epoch matches no start of this attempt.");
        if (eventKeys.Any(k => k != claimKey)) return Conflict("The attempt's start names a different claim key than its receipt.");
        var current = inProgress && currentEpoch == epoch;

        if (!await JournalPresent(conn, tx))
            return AttemptTraceSourceResult.Fail(AttemptTraceCodes.JournalUnavailable, "This database has no supervisor journal.");

        string? launch = null, exited = null;
        await using (var cmd = new NpgsqlCommand($"""
            SELECT kind, length(data) <= {AttemptTrace.MaxJournalDataChars}, CASE WHEN length(data) <= {AttemptTrace.MaxJournalDataChars} THEN data END
            FROM supervisor_journal.records
            WHERE root = @root AND claim_key = @k AND kind IN ('launch_intent', 'exited')
            ORDER BY seq LIMIT 3
            """, conn, tx))
        {
            cmd.Parameters.AddWithValue("root", root.ToString("D"));
            cmd.Parameters.AddWithValue("k", claimKey);
            await using var r = await cmd.ExecuteReaderAsync();
            while (await r.ReadAsync())
            {
                if (!r.GetBoolean(1)) return AttemptTraceSourceResult.Fail(AttemptTraceCodes.Invalid, $"A {r.GetString(0)} record is larger than expected.");
                var kind = r.GetString(0);
                if (kind == "launch_intent") { if (launch is not null) return Conflict("The journal has more than one launch_intent."); launch = r.GetString(2); }
                else { if (exited is not null) return Conflict("The journal has more than one exited record."); exited = r.GetString(2); }
            }
        }
        if (launch is null)
        {
            var reason = await Compacted(conn, tx, root, claimKey) ? JournalCompacted : NoTraceBlock;
            return Found(new AttemptTraceSource(root, nodeId, attemptId, epoch, claimKey, reason, null, null, current));
        }
        return Found(new AttemptTraceSource(root, nodeId, attemptId, epoch, claimKey, null, launch, exited, current));
    }

    private static async Task<bool> JournalPresent(NpgsqlConnection conn, NpgsqlTransaction tx)
    {
        await using var cmd = new NpgsqlCommand("""
            SELECT to_regclass('supervisor_journal.records') IS NOT NULL
               AND to_regclass('supervisor_journal.streams') IS NOT NULL
               AND to_regclass('supervisor_journal.summaries') IS NOT NULL
            """, conn, tx);
        return await cmd.ExecuteScalarAsync() is true;
    }

    /// <summary>A stream whose records were compacted into a summary no longer has its launch_intent.</summary>
    private static async Task<bool> Compacted(NpgsqlConnection conn, NpgsqlTransaction tx, Guid root, string claimKey)
    {
        await using var cmd = new NpgsqlCommand("""
            SELECT EXISTS (SELECT 1 FROM supervisor_journal.streams WHERE root = @root AND claim_key = @k AND state = 'compacted')
                OR EXISTS (SELECT 1 FROM supervisor_journal.summaries WHERE root = @root AND claim_key = @k)
            """, conn, tx);
        cmd.Parameters.AddWithValue("root", root.ToString("D"));
        cmd.Parameters.AddWithValue("k", claimKey);
        return await cmd.ExecuteScalarAsync() is true;
    }

    private static AttemptTraceSourceResult Found(AttemptTraceSource s) => new(s, null, null);
    private static AttemptTraceSourceResult Conflict(string message) => AttemptTraceSourceResult.Fail(AttemptTraceCodes.IdentityConflict, message);
}
