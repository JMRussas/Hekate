# Docker

Docker is used for infrastructure services only -- specifically the PostgreSQL database that backs the Context Store. Application services (orchestration, pipeline, gateways) run natively on Windows via NSSM.

## When Docker Is Needed

| Component | Docker Required? | Notes |
|-----------|-----------------|-------|
| Context Store | **Yes** | Requires Postgres with AGE + pgvector extensions |
| Orchestration (Postgres mode) | **Yes** | When `ORCHESTRATION_DSN` points to the Docker Postgres instance |
| Orchestration (SQLite mode) | No | Default dev mode, no Docker needed |
| LLM Gateway | No | Pure Python, no database |
| Gods Pipeline | No | Uses orchestration's database |
| Hades Admin | No | Pure Python |

!!! tip "SQLite for development"
    If you are only working on the orchestration backend, pipeline handlers, or LLM gateway, you do not need Docker at all. SQLite mode is the default and requires zero infrastructure setup. See [Local Development](local-dev.md) for details.

## Docker Compose

The compose file lives at `context-store/docker-compose.yml`:

```yaml
services:
  postgres:
    build:
      context: .
      dockerfile: Dockerfile.postgres
    container_name: code-storage-db
    environment:
      POSTGRES_DB: code_storage
      POSTGRES_USER: ${POSTGRES_USER:-postgres}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-postgres}
    ports:
      - "5433:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 2s
      timeout: 5s
      retries: 10

volumes:
  pgdata:
```

### Key details

- **Port**: 5433 (host) maps to 5432 (container). The non-standard host port avoids conflicts with any local Postgres installation.
- **Container name**: `code-storage-db` -- referenced by the Hades deploy process for readiness checks.
- **Health check**: `pg_isready` runs every 2 seconds with up to 10 retries, ensuring the database is accepting connections before dependent services start.
- **Volume**: Named volume `pgdata` persists data across container restarts and image rebuilds.

## Custom Docker Image

The Context Store requires two PostgreSQL extensions not available in the standard Postgres image:

- **Apache AGE** -- Graph database extension (knowledge graph storage)
- **pgvector** -- Vector similarity search (semantic search for agent memory)

The custom image is built from `context-store/Dockerfile.postgres`:

```dockerfile
FROM apache/age:release_PG16_1.6.0

# Install build dependencies for pgvector
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential postgresql-server-dev-16 \
    git ca-certificates && \
    cd /tmp && \
    git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git && \
    cd pgvector && \
    make && make install && \
    apt-get purge -y build-essential postgresql-server-dev-16 git && \
    apt-get autoremove -y && \
    rm -rf /tmp/pgvector /var/lib/apt/lists/*

COPY 00-create-databases.sh /docker-entrypoint-initdb.d/
COPY init.sql /docker-entrypoint-initdb.d/
```

The image starts from Apache AGE (which includes PostgreSQL 16), then builds and installs pgvector v0.8.0 from source. Init scripts in `/docker-entrypoint-initdb.d/` create the required databases and extensions on first run.

## Starting and Stopping

```bash
# Start the database
cd context-store
docker compose up -d

# Check status
docker compose ps

# View logs
docker compose logs -f postgres

# Stop (preserves data)
docker compose down

# Stop and destroy data volume
docker compose down -v
```

## Volume Management

The `pgdata` named volume stores all PostgreSQL data. This volume survives `docker compose down` but is destroyed by `docker compose down -v`.

### Inspecting the volume

```bash
docker volume inspect context-store_pgdata
```

### Backing up the database

```bash
docker exec code-storage-db pg_dump -U postgres code_storage > backup.sql
```

### Restoring from backup

```bash
docker exec -i code-storage-db psql -U postgres code_storage < backup.sql
```

### Full reset (destroy and recreate)

```bash
cd context-store
docker compose down -v
docker compose up -d
```

!!! warning "Data loss"
    Running `docker compose down -v` destroys the volume and all stored data. The Context Store will reinitialize with empty databases on next startup. Agent memory, knowledge graphs, and stored context will be lost.

## Health Check Configuration

The compose health check is used by Docker to determine container readiness. The deploy script (`scripts/deploy.sh`) and Hades admin service also check Postgres readiness before starting dependent services.

| Parameter | Value | Notes |
|-----------|-------|-------|
| `test` | `pg_isready -U postgres` | Standard Postgres readiness probe |
| `interval` | 2s | How often to check |
| `timeout` | 5s | Max wait per check |
| `retries` | 10 | Checks before marking unhealthy |

The Hades admin service has its own startup timeout for Docker (configurable in `services.json`):

```json
{
  "infra": {
    "docker_desktop": "C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe",
    "compose_file": "...",
    "postgres_container": "code-storage-db",
    "postgres_port": 5433,
    "startup_timeout": 90
  }
}
```

If Docker Desktop is not running, the deploy process will attempt to launch it and wait up to 90 seconds for readiness.

## Connecting to the Database

### From the host

```bash
# psql (if installed locally)
psql -h localhost -p 5433 -U postgres -d code_storage

# Or via docker exec
docker exec -it code-storage-db psql -U postgres -d code_storage
```

### Connection string for services

The Context Store uses the `CODESTORAGE_CONNSTR` environment variable:

```
Host=localhost;Port=5433;Database=code_storage;Username=postgres;Password=postgres
```

## Troubleshooting

### Docker Desktop not starting

The Hades deploy process tries to launch Docker Desktop automatically. If it fails:

1. Check that Docker Desktop is installed at `C:\Program Files\Docker\Docker\Docker Desktop.exe`
2. Start Docker Desktop manually and wait for it to fully initialize
3. Verify the daemon is responsive: `docker info`

### Container starts but Context Store cannot connect

- Verify the port mapping: `docker port code-storage-db`
- Check the container health: `docker inspect --format='{{.State.Health.Status}}' code-storage-db`
- Ensure `CODESTORAGE_CONNSTR` uses port 5433, not 5432

### "extension age does not exist" errors

The AGE extension must be created during database initialization. If you reset the volume, the init scripts run automatically. If you see this error on an existing database, connect and run:

```sql
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;
```

### Build fails during pgvector compilation

The Dockerfile builds pgvector from source, which requires network access to clone from GitHub. Ensure Docker has internet access and the pgvector repository is reachable.
