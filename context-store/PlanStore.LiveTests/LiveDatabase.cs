using Npgsql;
using Xunit;

namespace CodeStoragePoc.PlanStore.LiveTests;

/// <summary>
/// Creates a NEW disposable database for this test run: vector + AGE + code_graph, the base
/// Hekate schema, then the plan-contract schema. Never touches any other database.
/// </summary>
public sealed class LiveDatabase : IAsyncLifetime
{
    public const string Prefix = "hekate_plan_live_";
    public string Name { get; } = $"{Prefix}{DateTime.UtcNow:yyyyMMddHHmmss}_{Guid.NewGuid():N}"[..48];
    public string ConnectionString { get; private set; } = "";
    public Guid ProjectId { get; } = Guid.NewGuid();
    private string _admin = "";

    public async Task InitializeAsync()
    {
        var admin = Environment.GetEnvironmentVariable("HEKATE_PLAN_LIVE_CONNSTR");
        if (string.IsNullOrWhiteSpace(admin))
            throw new InvalidOperationException(
                "HEKATE_PLAN_LIVE_CONNSTR is not set. The live suite needs a loopback admin connection to the 'postgres' database " +
                "of the hekate-local container, e.g. Host=127.0.0.1;Port=5434;Database=postgres;Username=postgres;Password=postgres. Not skipping.");
        var csb = new NpgsqlConnectionStringBuilder(admin);
        if (csb.Host is not ("127.0.0.1" or "localhost" or "::1")) throw new InvalidOperationException($"Refusing non-loopback host '{csb.Host}'.");
        if (csb.Database != "postgres") throw new InvalidOperationException("HEKATE_PLAN_LIVE_CONNSTR must point at the 'postgres' admin database.");
        _admin = admin;

        await using (var conn = new NpgsqlConnection(admin))
        {
            await conn.OpenAsync();
            await using var cmd = new NpgsqlCommand($"CREATE DATABASE \"{Name}\"", conn);
            await cmd.ExecuteNonQueryAsync();
        }
        csb.Database = Name;
        csb.Pooling = false;
        ConnectionString = csb.ConnectionString;
        if (!Name.StartsWith(Prefix, StringComparison.Ordinal)) throw new InvalidOperationException("Database name guard failed.");

        await using (var conn = new NpgsqlConnection(ConnectionString))
        {
            await conn.OpenAsync();
            foreach (var sql in new[]
            {
                "CREATE EXTENSION IF NOT EXISTS vector",
                "CREATE EXTENSION IF NOT EXISTS age",
                "LOAD 'age'",
                "SET search_path = ag_catalog, \"$user\", public",
                "SELECT create_graph('code_graph')",
                "SELECT create_vlabel('code_graph', 'CodeNode')",
                "SELECT create_elabel('code_graph', 'DEPENDS_ON')",
            })
            {
                await using var cmd = new NpgsqlCommand(sql, conn);
                await cmd.ExecuteNonQueryAsync();
            }
        }
        await CodeStoragePoc.DbLayer.Schema.Initialize(ConnectionString);
        await CodeStoragePoc.PlanContracts.PlanStoreSchema.Ensure(ConnectionString);
        await Exec("INSERT INTO public.projects (id, name, root_path) VALUES (@p, 'plan-live-tests', 'disposable://plan-live-tests')", ("p", ProjectId));
    }

    public async Task DisposeAsync()
    {
        if (Environment.GetEnvironmentVariable("HEKATE_PLAN_LIVE_KEEP") == "1" || _admin == "") return;
        if (!Name.StartsWith(Prefix, StringComparison.Ordinal)) return;
        NpgsqlConnection.ClearAllPools();
        await using var conn = new NpgsqlConnection(_admin);
        await conn.OpenAsync();
        await using var cmd = new NpgsqlCommand($"DROP DATABASE IF EXISTS \"{Name}\" WITH (FORCE)", conn);
        await cmd.ExecuteNonQueryAsync();
    }

    public async Task Exec(string sql, params (string Name, object Value)[] p)
    {
        await using var conn = new NpgsqlConnection(ConnectionString);
        await conn.OpenAsync();
        await using var cmd = new NpgsqlCommand(sql, conn);
        foreach (var (n, v) in p) cmd.Parameters.AddWithValue(n, v);
        await cmd.ExecuteNonQueryAsync();
    }

    public async Task<object?> Scalar(string sql, params (string Name, object Value)[] p)
    {
        await using var conn = new NpgsqlConnection(ConnectionString);
        await conn.OpenAsync();
        await using var cmd = new NpgsqlCommand(sql, conn);
        foreach (var (n, v) in p) cmd.Parameters.AddWithValue(n, v);
        return await cmd.ExecuteScalarAsync();
    }

    /// <summary>Count of DEPENDS_ON edges tagged with this plan root between succ and pred.</summary>
    public async Task<long> TaggedEdgeCount(Guid root, Guid? succ = null, Guid? pred = null)
    {
        await using var conn = new NpgsqlConnection(ConnectionString);
        await conn.OpenAsync();
        foreach (var s in new[] { "LOAD 'age'", "SET search_path = ag_catalog, \"$user\", public" })
        {
            await using var c = new NpgsqlCommand(s, conn);
            await c.ExecuteNonQueryAsync();
        }
        var a = succ is Guid sg ? $" {{node_id: '{sg}'}}" : "";
        var b = pred is Guid pg ? $" {{node_id: '{pg}'}}" : "";
        var sql = $"SELECT count::text FROM cypher('code_graph', $$ MATCH (x:CodeNode{a})-[e:DEPENDS_ON {{plan_root: '{root}'}}]->(y:CodeNode{b}) RETURN count(e) $$) AS (count agtype)";
        await using var cmd = new NpgsqlCommand(sql, conn);
        return long.Parse((string)(await cmd.ExecuteScalarAsync())!);
    }
}

[CollectionDefinition("live")]
public class LiveCollection : ICollectionFixture<LiveDatabase>;
