# Plan 007: Add Project CLI (`tools/cli.py`)

Add an introspection CLI per the global CLAUDE.md conventions. This gives the AI a live window into the DB and codebase -- dynamic queries against the node tree, graph, and schema as they currently exist. Read-only, Python, uses psycopg2 for Postgres access.

```xml
<plan level="L2">

<context>
  The CodeStoragePoc stores all code, plans, conversations, and ideas as nodes
  in a PostgreSQL database with AGE graph and pgvector extensions. There are 4
  relational tables (projects, files, nodes, node_attributes) and an AGE graph
  (code_graph) with CodeNode vertices and 12 edge labels.

  The AI agent currently has no way to inspect DB state without running the
  full C# POC app or writing ad-hoc SQL. Repeated questions arise:
    - How many nodes exist, broken down by type?
    - What does a specific subtree look like?
    - Which nodes have embeddings?
    - What edges exist in the AGE graph?
    - Is the DB even running?
    - What projects/files are registered?

  No tools/cli.py exists yet. The project has no Python dependencies.

  Connection: port 5433, database code_storage, user postgres, password postgres.
  Env var: CODESTORAGE_CONNSTR (.NET format). The Python CLI will use its own
  CODESTORAGE_DSN env var (libpq format) with matching default credentials.

  Node types span 3 domains: code (11 types), planning (8 types), ideation (9 types).
  AGE requires LOAD 'age' + SET search_path per connection for Cypher queries.
</context>

<thinking>
  **CLI framework**: Click + psycopg2-binary. Two deps, both well-known. Click
  is genuinely better than argparse for subcommand CLIs.

  **AGE from Python**: AGE Cypher queries are just SQL wrapped in
  SELECT * FROM cypher('code_graph', $$ ... $$) as (col agtype).
  psycopg2 can execute these after LOAD 'age' + SET search_path on the
  connection. Results come back as strings, same as the C# AgeLayer does.

  **Connection string**: Python psycopg2 uses DSN format (host=... port=...
  dbname=...), different from .NET. The CLI should accept CODESTORAGE_DSN with
  a fallback matching docker-compose.yml defaults (zero-config for POC).

  **Which commands first** (based on repeated AI agent queries):
  1. status  -- health check: DB up, extensions, row counts
  2. counts  -- node counts grouped by type (most common query)
  3. tree    -- subtree visualization from a root node ID
  4. projects -- list projects with file/node counts
  5. search  -- find nodes by name/type substring
  6. node    -- inspect one node with all attributes
  7. graph-stats -- AGE vertex/edge counts
  8. embeddings -- embedding coverage by node type
  9. roots   -- list root nodes (parent_id IS NULL)

  **Deferred commands** (add when the need arises):
  - intent -- test intent classifier (needs Python port of patterns)
  - graph-query -- arbitrary Cypher (security concern)
  - validate -- referential integrity check (relational vs graph)
  - conversations -- list conversations with turn/idea counts
</thinking>

<approach>
  1. Create tools/cli.py with Click framework and psycopg2-binary.
  2. Add db_connect() helper: reads CODESTORAGE_DSN env var, falls back to
     host=localhost port=5433 dbname=code_storage user=postgres password=postgres.
  3. Add age_connect() helper: wraps db_connect + LOAD 'age' + SET search_path
     for Cypher-capable connections.
  4. Implement 9 read-only commands:

     a. status -- Connect, SELECT 1, report extensions loaded, table row
        counts, AGE graph existence. Quick health check.

     b. counts -- GROUP BY node_type with counts, sorted descending.
        Optional --project filter by project name.

     c. tree NODE_ID -- Recursive CTE to fetch subtree, render as indented
        text tree with type, name, attribute count. Optional --depth to
        limit recursion. Optional --attrs to show key=value pairs inline.

     d. projects -- List projects: ID, name, root_path, file count, node
        count. Table format.

     e. search QUERY -- ILIKE on nodes.name and nodes.value. Optional
        --type to filter by node_type. Shows ID, type, name, truncated
        value. Limit 20.

     f. node NODE_ID -- Full node record + all attributes + parent info +
        child count.

     g. graph-stats -- Count vertices and edges by label in AGE via Cypher.

     h. embeddings -- Nodes with/without embeddings by node_type. Coverage %.

     i. roots -- Nodes where parent_id IS NULL (entry points). ID, type,
        name, child count.

  5. Add tools/requirements.txt with psycopg2-binary and click.
  6. Update CLAUDE.md project structure to include tools/.
</approach>

<outputs>
  | File                    | Action | What Changes                                    |
  |-------------------------|--------|-------------------------------------------------|
  | tools/cli.py            | create | Full CLI with 9 read-only commands              |
  | tools/requirements.txt  | create | psycopg2-binary, click                          |
  | CLAUDE.md               | modify | Add tools/ to project structure, CLI usage docs  |
</outputs>

<testing>
  | Test                                  | Type        | Command / Check                         |
  |---------------------------------------|-------------|-----------------------------------------|
  | CLI loads without errors              | unit        | python tools/cli.py --help              |
  | Each command shows help               | unit        | python tools/cli.py status --help       |
  | status connects to running DB         | integration | python tools/cli.py status              |
  | counts returns node type breakdown    | integration | python tools/cli.py counts              |
  | tree renders a known root node        | integration | python tools/cli.py tree {NODE_ID}      |
  | search finds nodes by name            | integration | python tools/cli.py search Vector2      |
  | graph-stats returns without error     | integration | python tools/cli.py graph-stats         |
  | embeddings shows coverage             | integration | python tools/cli.py embeddings          |
  | roots lists entry-point nodes         | integration | python tools/cli.py roots               |

  Integration tests require Docker DB running (docker compose up -d).
  All commands are read-only so they are safe to run against live data.
</testing>

<questions>
  1. **DSN env var name**: Use CODESTORAGE_DSN (Python psycopg2 format) separate
     from the C# CODESTORAGE_CONNSTR? The formats differ (.NET vs libpq).
     Proposed answer: Yes, use CODESTORAGE_DSN for Python, keep the fallback
     defaults identical to docker-compose.yml so it works with zero config.

  2. **Virtual environment**: Should we add a venv setup script or just document
     pip install -r tools/requirements.txt?
     Proposed answer: Just document it. The AI can run pip directly. A venv
     script is over-engineering for a POC introspection tool.

  3. **Output format**: Plain text tables, or support --json flag?
     Proposed answer: Plain text only for now. Add --json later if a consumer
     needs structured output. The primary consumer is an AI reading stdout.
</questions>

</plan>
```
