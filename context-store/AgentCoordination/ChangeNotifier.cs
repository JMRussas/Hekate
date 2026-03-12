// CodeStoragePoc - Change Notifier (LISTEN/NOTIFY)
//
// Subscribes to PostgreSQL NOTIFY events on the 'node_changed' channel.
// When a node is inserted or updated, the trigger fires pg_notify with
// the node ID as payload.
//
// Runs as a background Task, printing notifications as they arrive.
// This simulates a second agent observing changes made by the first.
//
// Depends on: Npgsql
// Used by:    Program

using Npgsql;

namespace CodeStoragePoc.AgentCoordination;

public class ChangeNotifier : IAsyncDisposable
{
    private readonly string _connStr;
    private NpgsqlConnection? _conn;
    private CancellationTokenSource? _cts;
    private Task? _listenerTask;

    public ChangeNotifier(string connectionString) => _connStr = connectionString;

    /// <summary>
    /// Start listening for node_changed notifications in a background task.
    /// Each notification prints to console, simulating a second agent.
    /// </summary>
    public async Task StartListening()
    {
        _conn = new NpgsqlConnection(_connStr);
        await _conn.OpenAsync();

        // Register the notification handler
        _conn.Notification += (_, args) =>
        {
            Console.WriteLine($"[NOTIFY]   node_changed → {args.Payload}");
        };

        // Subscribe to the channel
        await using var cmd = new NpgsqlCommand("LISTEN node_changed", _conn);
        await cmd.ExecuteNonQueryAsync();

        // Background task that waits for notifications
        _cts = new CancellationTokenSource();
        var token = _cts.Token;
        _listenerTask = Task.Run(async () =>
        {
            try
            {
                while (!token.IsCancellationRequested)
                {
                    // WaitAsync blocks until a notification arrives or timeout
                    await _conn.WaitAsync(token);
                }
            }
            catch (OperationCanceledException)
            {
                // Expected on shutdown
            }
        }, token);

        Console.WriteLine("[NOTIFY]   Listener started on 'node_changed' channel");
    }

    /// <summary>
    /// Stop listening and clean up.
    /// </summary>
    public async ValueTask DisposeAsync()
    {
        if (_cts != null)
        {
            await _cts.CancelAsync();
            if (_listenerTask != null)
            {
                try { await _listenerTask; } catch (OperationCanceledException) { }
            }
            _cts.Dispose();
        }

        if (_conn != null)
            await _conn.DisposeAsync();
    }
}
