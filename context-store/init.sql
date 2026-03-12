-- Enable extensions
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;

-- AGE requires loading per-session, but this runs in the init context
LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- Create the code graph
SELECT create_graph('code_graph');

-- Create vertex and edge labels
SELECT create_vlabel('code_graph', 'CodeNode');

-- Code domain edges
SELECT create_elabel('code_graph', 'CALLS');
SELECT create_elabel('code_graph', 'REFERENCES');
SELECT create_elabel('code_graph', 'DEPENDS_ON');
SELECT create_elabel('code_graph', 'MODIFIES');

-- Ideation & planning domain edges
SELECT create_elabel('code_graph', 'EXTRACTED');
SELECT create_elabel('code_graph', 'SPAWNED_FROM');
SELECT create_elabel('code_graph', 'IMPLEMENTED_BY');
SELECT create_elabel('code_graph', 'PRODUCES');
SELECT create_elabel('code_graph', 'CONSTRAINS');
SELECT create_elabel('code_graph', 'BLOCKS');
SELECT create_elabel('code_graph', 'RELATES_TO');
SELECT create_elabel('code_graph', 'CONTRADICTS');
