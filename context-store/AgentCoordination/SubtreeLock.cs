// CodeStoragePoc - Subtree Advisory Locks
//
// Uses PostgreSQL advisory locks to claim exclusive ownership of a subtree
// before mutation. This prevents concurrent agents from modifying the same
// code nodes simultaneously.
//
// LOCK KEY DERIVATION:
// Postgres advisory locks use a bigint (64-bit) key. UUIDs are 128-bit.
// We XOR the two 64-bit halves of the UUID to produce a deterministic 64-bit key.
// This gives ~zero collision probability for the scale of code nodes we handle.
//
// USAGE PATTERN:
// 1. Agent calls TryClaimSubtree(nodeId, agentId)
//    - Acquires pg_try_advisory_lock on the derived key
//    - Returns true if acquired, false if another agent holds it
// 2. Agent performs mutations using LockConnection (same session that holds the lock)
// 3. Agent disposes the SubtreeLock (IAsyncDisposable) or calls ReleaseSubtree
//
// THREAD SAFETY:
// Single-owner — one SubtreeLock instance holds at most one lock at a time.
// The LockConnection property exposes the session holding the lock so that
// NodeRepository can execute mutations on the SAME connection.
//
// Depends on: Npgsql
// Used by:    Program

using Npgsql;

namespace CodeStoragePoc.AgentCoordination;

public class SubtreeLock : IAsyncDisposable
{
    private readonly string _connStr;

    // We keep the connection alive while the lock is held,
    // because advisory locks are session-scoped.
    private NpgsqlConnection? _lockConn;
    private Guid? _lockedNodeId;
    private string? _lockAgentId;
    private bool _released;

    public SubtreeLock(string connectionString) => _connStr = connectionString;

    /// <summary>
    /// The connection holding the advisory lock. Use this for all mutations
    /// that must be protected by the lock. Null when no lock is held.
    /// </summary>
    public NpgsqlConnection? LockConnection => _lockConn;

    /// <summary>
    /// Derive a 64-bit lock key from a UUID by XORing its two halves.
    /// Deterministic: same UUID always produces the same key.
    /// </summary>
    public static long UuidToLockKey(Guid id)
    {
        var bytes = id.ToByteArray();
        long high = BitConverter.ToInt64(bytes, 0);
        long low = BitConverter.ToInt64(bytes, 8);
        return high ^ low;
    }

    /// <summary>
    /// Try to acquire an advisory lock on the subtree rooted at nodeId.
    /// Returns true if the lock was acquired (non-blocking).
    /// The connection is kept open to maintain the lock.
    /// </summary>
    public async Task<bool> TryClaimSubtree(Guid nodeId, string agentId)
    {
        if (_lockConn != null)
            throw new InvalidOperationException("Lock already held. Release before claiming another.");

        var conn = new NpgsqlConnection(_connStr);
        try
        {
            await conn.OpenAsync();

            long key = UuidToLockKey(nodeId);

            await using var cmd = new NpgsqlCommand(
                "SELECT pg_try_advisory_lock(@key)", conn);
            cmd.Parameters.AddWithValue("key", key);

            var result = (bool)(await cmd.ExecuteScalarAsync())!;

            if (result)
            {
                _lockConn = conn;
                _lockedNodeId = nodeId;
                _lockAgentId = agentId;

                // Update modified_by on the root node to show ownership
                await using var update = new NpgsqlCommand(
                    "UPDATE nodes SET modified_by = @agent WHERE id = @id", conn);
                update.Parameters.AddWithValue("agent", agentId);
                update.Parameters.AddWithValue("id", nodeId);
                await update.ExecuteNonQueryAsync();

                Console.WriteLine($"[AGENT]    TryClaimSubtree({nodeId:N}...): ACQUIRED by {agentId}");
                return true;
            }

            // Lock not acquired — close the connection
            await conn.DisposeAsync();
            Console.WriteLine($"[AGENT]    TryClaimSubtree({nodeId:N}...): DENIED (held by another agent)");
            return false;
        }
        catch
        {
            // If anything fails during acquisition, clean up the connection
            await conn.DisposeAsync();
            throw;
        }
    }

    /// <summary>
    /// Release the advisory lock. Closes the connection.
    /// </summary>
    public async Task ReleaseSubtree(Guid nodeId, string agentId)
    {
        if (_lockConn == null)
            throw new InvalidOperationException("No lock held.");

        long key = UuidToLockKey(nodeId);

        await using var cmd = new NpgsqlCommand(
            "SELECT pg_advisory_unlock(@key)", _lockConn);
        cmd.Parameters.AddWithValue("key", key);
        await cmd.ExecuteNonQueryAsync();

        await _lockConn.DisposeAsync();
        _lockConn = null;
        _lockedNodeId = null;
        _lockAgentId = null;
        _released = true;

        Console.WriteLine($"[AGENT]    ReleaseSubtree({nodeId:N}...): RELEASED by {agentId}");
    }

    /// <summary>
    /// IAsyncDisposable — releases the lock and connection if still held.
    /// </summary>
    public async ValueTask DisposeAsync()
    {
        if (_lockConn != null && !_released)
        {
            try
            {
                if (_lockedNodeId.HasValue)
                {
                    long key = UuidToLockKey(_lockedNodeId.Value);
                    await using var cmd = new NpgsqlCommand(
                        "SELECT pg_advisory_unlock(@key)", _lockConn);
                    cmd.Parameters.AddWithValue("key", key);
                    await cmd.ExecuteNonQueryAsync();
                }
            }
            catch
            {
                // Best-effort unlock during dispose — connection close will release anyway
            }

            await _lockConn.DisposeAsync();
            _lockConn = null;
            _lockedNodeId = null;
            _lockAgentId = null;
        }
    }
}
