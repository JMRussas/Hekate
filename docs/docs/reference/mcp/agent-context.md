# Agent Context MCP

Persistent memory for AI agents. Store decisions, findings, ideas, and plans as nodes in a shared knowledge graph backed by Postgres (AGE + pgvector). Query by text search, semantic similarity, tree navigation, or temporal history.

**Port:** 5213
**Transport:** SSE
**Service name:** `HekateAgentContextMcp`
**Database:** Postgres on port 5433 (`code_storage` database)
**Embedding model:** Ollama `nomic-embed-text` (768 dimensions)

---

## Connection

### Claude Code Configuration

```json
{
  "mcpServers": {
    "agent-context": {
      "url": "http://localhost:5213/sse"
    }
  }
}
```

### Health Check

```bash
curl http://localhost:5213/health
```

```json
{"status": "ok", "service": "agent-context", "port": 5213}
```

---

## Node Types

Nodes are the fundamental storage unit. Each node has a type, name, value (content), optional parent (tree structure), and optional semantic embedding.

| Type | Description | Typical Use |
|------|-------------|-------------|
| `decision` | An architectural or design decision | "Use JWT for auth", "SQLite for local DB" |
| `finding` | A discovered fact about the codebase | "FastAPI uses dependency injection", "Port 5200 is taken" |
| `idea` | A proposed approach or feature | "Could use Redis for caching" |
| `question` | An open question needing resolution | "Should we support multiple databases?" |
| `action_item` | A concrete next step | "Add error handling to auth middleware" |
| `conversation` | A conversation container | Parent node for turns |
| `turn` | A single exchange in a conversation | Child of a conversation |
| `topic` | A thematic grouping | Parent for related nodes |
| `plan` | An execution plan | Structured plan node |
| `task` | A specific task within a plan | Child of a plan |
| `research` | Research notes or findings | Technical investigation results |

Custom types are also accepted -- the `node_type` field is a free-form string.

---

## Tools

### ensure_project

Get or create a project by name. Call this before storing nodes to get a stable project ID.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `name` | string | Yes | - | Project name (e.g., `Hekate`, `my-feature`) |
| `root_path` | string | No | `""` | Filesystem path for the project root |

**Returns:** `{status: "existing"|"created", id, name, root_path}`

If the project already exists (by name), returns the existing ID without modification.

---

### store_node

Store a node in the context graph. Automatically computes a semantic embedding via Ollama. If Ollama is unavailable, the node is stored without an embedding.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `node_type` | string | Yes | - | Node type (see [Node Types](#node-types)) |
| `name` | string | Yes | - | Short label for the node |
| `value` | string | No | `""` | Full content/description |
| `parent_id` | string | No | `""` | Parent node UUID (creates tree structure) |
| `project_name` | string | No | `""` | Project name (looks up project ID). Ignored if `project_id` is set. |
| `project_id` | string | No | `""` | Direct project UUID. Takes precedence over `project_name`. |
| `attrs` | string | No | `"{}"` | JSON string of key-value attributes (e.g., `'{"status": "open", "priority": "p1"}'`) |

**Returns:** `{id, node_type, name, project_id, parent_id, attribute_count, embedded}`

### Embedding Behavior

- Embedding text is `"{name} {value}"` (or just `name` if no value)
- Uses `search_document:` prefix per nomic-embed-text convention
- Timeout: 2 seconds (configurable via `OLLAMA_EMBED_TIMEOUT`)
- On timeout or error, the node is stored without an embedding (silent failure)
- Embedding is written after the node commit -- the node exists even if embedding fails

---

### query_nodes

Search nodes by text match across name and value fields. Use this for exact/substring matching.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `text` | string | Yes | - | Search text (substring, case-insensitive) |
| `node_types` | string | No | `""` | Comma-separated types to filter (e.g., `"decision,idea"`) |
| `project_name` | string | No | `""` | Filter to a specific project |
| `limit` | int | No | 10 | Max results |

**Returns:** `{count, results: [{id, node_type, name, value, parent_id, created_at, modified_at, project_name, attributes}]}`

---

### semantic_search

Search nodes by meaning using pgvector cosine distance. Use this for conceptual search (e.g., "how do I handle auth?" finds nodes about JWT tokens, login flows, etc.).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `query` | string | Yes | - | Natural language search query |
| `project_name` | string | No | `""` | Filter to a specific project |
| `node_types` | string | No | `""` | Comma-separated types to filter |
| `limit` | int | No | 5 | Max results |

**Returns:** `{count, results: [{id, node_type, name, value, distance, project_name, attributes}], coverage: {embedded, total, pct}}`

- `distance` is cosine distance (lower = more similar)
- `coverage` shows what percentage of matching nodes have embeddings
- Requires Ollama running with `nomic-embed-text` model
- Uses `search_query:` prefix for query embedding (asymmetric search)

---

### get_node

Get a node with its attributes and optionally its direct children.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `node_id` | string | Yes | - | Node UUID |
| `include_children` | bool | No | true | Include direct children |

**Returns:** Full node object with attributes, parent info, and children.

---

### get_children

Get direct children of a node, optionally filtered by type.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `node_id` | string | Yes | - | Parent node UUID |
| `type_filter` | string | No | `""` | Filter by node type (e.g., `"task"`) |

**Returns:** `{parent_id, count, children: [{id, node_type, name, value, sibling_order, modified_at, attributes}]}`

Children are ordered by `sibling_order` (gap-numbered in increments of 100 for easy insertion).

---

### list_projects

List all projects in the context store with node counts and embedding coverage.

*No parameters.*

**Returns:** `{count, projects: [{id, name, root_path, created_at, node_count, embedded_count, last_activity}]}`

---

### list_recent

List recently modified nodes across the context store.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `node_types` | string | No | `""` | Comma-separated types to filter |
| `project_name` | string | No | `""` | Filter to a specific project |
| `limit` | int | No | 20 | Max results |

**Returns:** `{count, nodes: [{id, node_type, name, value, parent_id, modified_at, modified_by, project_name}]}`

Ordered by `modified_at` descending. Good for seeing recent decisions, new ideas, and updated plans.

---

### history

Get temporal edge history for a node from the AGE graph. Shows all graph relationships (incoming and outgoing) including temporal properties.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `node_id` | string | Yes | Node UUID to query history for |

**Returns:** `{node_id, edge_count, edges: [{direction, edge_type, valid_from, valid_to, provenance, other_node_id, other_node_name}]}`

Edges are sorted chronologically by `valid_from`. Requires the AGE extension to be loaded (`init.sql`).

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CODESTORAGE_DB_HOST` | `localhost` | Postgres host |
| `CODESTORAGE_DB_PORT` | `5433` | Postgres port |
| `CODESTORAGE_DB_NAME` | `code_storage` | Database name |
| `CODESTORAGE_DB_USER` | `postgres` | Database user |
| `CODESTORAGE_DB_PASSWORD` | `postgres` | Database password |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API URL |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model |
| `OLLAMA_EMBED_TIMEOUT` | `2.0` | Embedding timeout (seconds) |
| `MCP_PORT` | `0` | SSE port (0 = stdio mode) |
