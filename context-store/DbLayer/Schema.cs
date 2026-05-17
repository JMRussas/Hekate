// CodeStoragePoc - Database Schema Setup
//
// Creates relational tables for code-as-nodes storage.
// Enables AGE + pgvector extensions, creates the graph and its labels.
// Idempotent — safe to call multiple times.
//
// Depends on: Npgsql
// Used by:    Program

using Npgsql;

namespace CodeStoragePoc.DbLayer;

public static class Schema
{
    public static async Task Initialize(string connectionString)
    {
        await using var conn = new NpgsqlConnection(connectionString);
        await conn.OpenAsync();

        // Enable extensions (AGE may already be loaded via init.sql on first Docker start,
        // but this ensures it works if the app runs before the init script)
        await Execute(conn, "CREATE EXTENSION IF NOT EXISTS vector;");

        // AGE extension — may fail if shared_preload_libraries doesn't include 'age'.
        // In that case, the Docker init.sql handles it.
        try
        {
            await Execute(conn, "CREATE EXTENSION IF NOT EXISTS age;");
        }
        catch (PostgresException ex) when (ex.SqlState == "58P01")
        {
            // age.so not in shared_preload_libraries — expected if running outside Docker
            Console.WriteLine("[INIT]     WARNING: AGE extension not available (shared_preload_libraries). Graph features disabled.");
        }

        // Relational schema
        await Execute(conn, """
            CREATE TABLE IF NOT EXISTS projects (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name TEXT NOT NULL,
                root_path TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );

            CREATE TABLE IF NOT EXISTS files (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                project_id UUID NOT NULL REFERENCES projects(id),
                file_path TEXT NOT NULL,
                root_node_id UUID,
                UNIQUE(project_id, file_path)
            );

            CREATE TABLE IF NOT EXISTS nodes (
                id UUID PRIMARY KEY,
                project_id UUID NOT NULL REFERENCES projects(id),
                file_id UUID REFERENCES files(id),
                node_type TEXT NOT NULL,
                name TEXT,
                value TEXT,
                parent_id UUID REFERENCES nodes(id),
                sibling_order INT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                modified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                modified_by TEXT,
                embedding vector(768)
            );

            CREATE TABLE IF NOT EXISTS node_attributes (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                node_id UUID NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                UNIQUE(node_id, key)
            );
        """);

        // Indexes
        await Execute(conn, """
            CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
            CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
            CREATE INDEX IF NOT EXISTS idx_nodes_modified_by ON nodes(modified_by);
            CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id);
            CREATE INDEX IF NOT EXISTS idx_node_attrs_node ON node_attributes(node_id);
            CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id);
        """);

        // pgvector IVFFlat index — requires rows to exist for training,
        // so we create it with lists=1 for the POC (production would use lists=100+)
        // Only create if it doesn't exist
        try
        {
            await Execute(conn, """
                CREATE INDEX IF NOT EXISTS idx_nodes_embedding
                ON nodes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 1);
            """);
        }
        catch (PostgresException)
        {
            // IVFFlat may fail on empty table — will retry after seeding
        }

        // Notification trigger: fires NOTIFY on every node mutation
        await Execute(conn, """
            CREATE OR REPLACE FUNCTION notify_node_changed()
            RETURNS TRIGGER AS $$
            BEGIN
                PERFORM pg_notify('node_changed', NEW.id::text);
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            DROP TRIGGER IF EXISTS trg_node_changed ON nodes;
            CREATE TRIGGER trg_node_changed
                AFTER INSERT OR UPDATE ON nodes
                FOR EACH ROW EXECUTE FUNCTION notify_node_changed();
        """);

        Console.WriteLine("[INIT]     Schema migrated");
    }

    private static async Task Execute(NpgsqlConnection conn, string sql)
    {
        await using var cmd = new NpgsqlCommand(sql, conn);
        await cmd.ExecuteNonQueryAsync();
    }
}
