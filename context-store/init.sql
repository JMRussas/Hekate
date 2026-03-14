-- Enable extensions
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;

-- AGE requires loading per-session, but this runs in the init context
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- Create the code graph (idempotent — skip if already exists)
DO $$
BEGIN
    PERFORM create_graph('code_graph');
EXCEPTION WHEN duplicate_schema THEN
    -- Graph already exists (e.g., volume persisted across container rebuilds)
    NULL;
END $$;

-- Create vertex and edge labels (idempotent — skip if already exist)
DO $$
BEGIN PERFORM create_vlabel('code_graph', 'CodeNode');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

-- Code domain edges
DO $$
BEGIN PERFORM create_elabel('code_graph', 'CALLS');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'REFERENCES');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'DEPENDS_ON');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'MODIFIES');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

-- Ideation & planning domain edges
DO $$
BEGIN PERFORM create_elabel('code_graph', 'EXTRACTED');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'SPAWNED_FROM');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'IMPLEMENTED_BY');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'PRODUCES');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'CONSTRAINS');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'BLOCKS');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'RELATES_TO');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'CONTRADICTS');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

-- Hecate semantic edges (code analysis)
DO $$
BEGIN PERFORM create_elabel('code_graph', 'IMPLEMENTS');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'DEFINES');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;

DO $$
BEGIN PERFORM create_elabel('code_graph', 'ALLOCATES');
EXCEPTION WHEN duplicate_table THEN NULL; END $$;
