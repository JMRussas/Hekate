# Context Store Basics

The context store is Hekate's persistent memory -- a shared graph database where agents store decisions, findings, ideas, and plans as nodes. It uses PostgreSQL with Apache AGE (graph queries) and pgvector (semantic search).

---

## Connecting

The Agent Context MCP server runs on port 5213 and provides tools for storing and querying nodes.

### From Claude Code

If the `.mcp.json` in your project root includes the agent-context server, the tools are available automatically:

```json
{
  "mcpServers": {
    "agent-context": {
      "type": "sse",
      "url": "http://localhost:5213/sse"
    }
  }
}
```

### Health check

```bash
curl http://localhost:5213/health
# {"status": "ok", "service": "agent-context", "port": 5213}
```

### Database connection

The MCP server connects directly to PostgreSQL:

| Setting | Default | Env Var |
|---------|---------|---------|
| Host | localhost | `CODESTORAGE_DB_HOST` |
| Port | 5433 | `CODESTORAGE_DB_PORT` |
| Database | code_storage | `CODESTORAGE_DB_NAME` |
| User | postgres | `CODESTORAGE_DB_USER` |
| Password | postgres | `CODESTORAGE_DB_PASSWORD` |

---

## Core Concepts

### Projects

Every node belongs to a project. Projects are top-level containers for organizing knowledge.

```
Use mcp__agent-context__ensure_project with name: "my-app"
```

`ensure_project` is idempotent -- it returns the existing project ID if one with that name already exists, or creates a new one.

### Nodes

Nodes are the fundamental unit of storage. Each node has:

| Field | Description |
|-------|-------------|
| `name` | Short label (e.g., "Use JWT for auth") |
| `value` | Longer description or content |
| `node_type` | Category (see node types below) |
| `project_id` | Which project this belongs to |
| `parent_id` | Optional parent node (for tree structure) |
| `attributes` | Key-value metadata (JSON) |

### Node Types

| Type | Use For |
|------|---------|
| `decision` | Committed architectural or design choices |
| `finding` | Discovered facts, observations, test results |
| `idea` | Proposed approaches, hypotheses |
| `question` | Open questions that need answers |
| `action_item` | Things that need to be done |
| `plan` | High-level plans and objectives |
| `task` | Specific work items within a plan |
| `research` | Research notes and references |

!!! tip "Node types are just strings"
    The context store does not enforce a fixed set of node types. You can use any string value. The types listed above are conventions used by the Hekate pipeline.

---

## Storing Nodes

Use `store_node` to persist a piece of knowledge:

```
Use mcp__agent-context__store_node with:
  project_id: "abc-123"
  name: "Use event sourcing for audit trail"
  value: "After evaluating CRUD vs event sourcing, chose event sourcing because: 1) full audit history, 2) replay capability, 3) natural fit for the pipeline's event-driven architecture."
  node_type: "decision"
  attributes: {"status": "committed", "confidence": "high"}
```

### With parent (tree structure)

```
Use mcp__agent-context__store_node with:
  project_id: "abc-123"
  parent_id: "parent-node-id"
  name: "JWT token expiry"
  value: "Set access token TTL to 15 minutes, refresh token to 7 days."
  node_type: "decision"
```

Child nodes inherit context from their parent. A question under a risk node is a risk-related question. A task under a plan phase is part of that phase.

### Auto-embedding

When you store a node, the MCP server automatically computes a 768-dimensional embedding via Ollama (`nomic-embed-text` model). This embedding enables semantic search later.

- Uses `search_document:` prefix per nomic-embed-text convention
- 2-second timeout -- if Ollama is unavailable, the node stores without an embedding
- Embeddings can be backfilled later: `python tools/backfill_embeddings.py`

---

## Querying Nodes

### Text Search (`query_nodes`)

Searches node names and values using substring matching (ILIKE):

```
Use mcp__agent-context__query_nodes with:
  query: "authentication"
  project_id: "abc-123"     # optional -- omit for cross-project search
  node_type: "decision"      # optional -- filter by type
```

Returns nodes whose `name` or `value` contains the search term.

**Best for:** Finding nodes when you know specific keywords.

### Semantic Search (`semantic_search`)

Finds nodes by meaning using pgvector cosine similarity:

```
Use mcp__agent-context__semantic_search with:
  query: "how does the auth system work?"
  project_id: "abc-123"     # optional
  limit: 10                  # optional, default varies
```

Returns ranked results with similarity scores. Also reports embedding coverage (how many nodes have embeddings vs. total).

**Best for:** Conceptual queries where exact keywords are unknown. "How does X work?" or "What decisions were made about Y?"

!!! note "Requires Ollama"
    Semantic search needs Ollama running to embed the query. If Ollama is down, the tool falls back to text search or returns an error.

### Comparison

| Feature | `query_nodes` | `semantic_search` |
|---------|--------------|-------------------|
| Search method | ILIKE substring | pgvector cosine similarity |
| Requires Ollama | No | Yes |
| Handles typos | No | Yes (semantic matching) |
| Cross-project | Yes | Yes |
| Speed | Fast | Slightly slower (embedding computation) |
| Best for | Exact terms | Conceptual queries |

---

## Navigating the Tree

### Get a single node

```
Use mcp__agent-context__get_node with node_id: "node-uuid"
```

Returns the node with all its attributes and a summary of children.

### List children

```
Use mcp__agent-context__get_children with:
  parent_id: "node-uuid"
  node_type: "task"          # optional filter
```

Returns all direct children, optionally filtered by type.

### Hierarchy

Nodes form trees:

```
Project
├── Plan: "Build auth system"
│   ├── Task: "Implement JWT middleware"
│   ├── Task: "Add user registration"
│   └── Risk: "Token theft via XSS"
│       └── Question: "Do we need CSRF protection?"
├── Decision: "Use bcrypt for passwords"
├── Finding: "FastAPI has built-in OAuth2 support"
└── Idea: "Add social login later"
```

---

## Temporal History

The context store tracks how node relationships change over time using temporal edges in the AGE graph.

```
Use mcp__agent-context__history with node_id: "node-uuid"
```

Returns edges with:

| Field | Description |
|-------|-------------|
| `valid_from` | When the relationship was created |
| `valid_to` | When it was superseded (null = still active) |
| `provenance` | What created the edge (e.g., "plan-to-code", "decomposer") |
| `direction` | Incoming or outgoing |

---

## Listing Projects and Recent Activity

### List all projects

```
Use mcp__agent-context__list_projects
```

Returns projects with node counts and embedding coverage.

### Recent activity

```
Use mcp__agent-context__list_recent
```

Returns recently created or modified nodes across all projects.

---

## Cross-Project Knowledge

Nodes from different projects share the same database. Semantic search across projects is the default -- omit `project_id` to search everything:

```
Use mcp__agent-context__semantic_search with:
  query: "how to handle database migrations"
```

This finds relevant decisions, findings, and patterns from all projects. Useful for:

- Reusing architectural decisions across repos
- Finding past solutions to similar problems
- Building institutional knowledge over time

---

## Embedding Management

### Check coverage

`list_projects` reports embedding counts alongside node counts:

```json
{
  "project_id": "abc-123",
  "name": "my-app",
  "node_count": 45,
  "embedded_count": 42
}
```

### Backfill missing embeddings

If nodes were stored while Ollama was down, backfill them:

```bash
cd context-store
python tools/backfill_embeddings.py              # Embed all un-embedded nodes
python tools/backfill_embeddings.py --dry-run     # Preview what would be embedded
python tools/backfill_embeddings.py --force        # Re-embed all nodes (even existing)
```

!!! warning "Embedding lag"
    Auto-embedding is fire-and-forget. A node stored in message N may not have its embedding ready for a semantic search in message N+1. By message N+2, it will be available. The backfill script closes this gap for offline batch processing.
