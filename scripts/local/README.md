# Local Hekate: plan-only profile

Run Hekate's plan store on this machine when you need it, without the Sisyphus
deployment. It starts two things: the Postgres+AGE container and the
context-store Api. That is enough to browse and edit plan nodes through
`/api/plans`, `/api/plan/{id}`, `/api/node/*` and the context-store UI.

```powershell
pwsh scripts/local/hekate-local.ps1 start      # preflight, start, wait for readiness
pwsh scripts/local/hekate-local.ps1 status     # exit 0 only when ready
pwsh scripts/local/hekate-local.ps1 stop       # stops only what start started; data kept
pwsh scripts/local/hekate-local.ps1 backup     # -> .hekate-local/backups/code_storage-<time>.dump
pwsh scripts/local/hekate-local.ps1 restore -BackupFile <file> -TargetDatabase restore_check
```

Add `-Json` to any command for machine-readable output.

## Requirements

- **Windows.** The launcher uses `Get-CimInstance Win32_Process`, `Get-NetTCPConnection`
  and kernel32 for process ownership, port checks and handle inheritance. It was
  validated live on Windows 11 with Docker Desktop 29.8.1 / Compose v5.5.1; see
  [VALIDATION.md](VALIDATION.md).
- **PowerShell 7.5 or later** (`pwsh`). The scripts declare `#Requires -Version 7.5`,
  because `ConvertFrom-Json -DateKind String` keeps recorded process start times as
  strings for the ownership checks. `Start-Process -Environment` needs 7.4 or later.
- **A Docker-compatible container runtime** with Compose v2 that provides the
  `docker` command (Docker Desktop, for example). Postgres+AGE runs from
  `context-store/Dockerfile.postgres`, and the first start builds that image.
  Without a runtime, `start` fails preflight with an actionable message and starts nothing.
  `docker` must resolve to the real executable. A shell started before Docker
  Desktop was installed may not have its bin directory on `PATH`. A non-standard
  `PATHEXT` (for example `.CPL` only) can make `docker` resolve to Docker Desktop's
  extensionless shell script instead of `docker.exe`. In either case, launch the
  launcher from a fresh shell, or set `PATH` / `PATHEXT` for that child process.
- **The .NET 10 SDK**, because the Api targets `net10.0`.

## What it does not run

The gods pipeline, LLM gateway, Hades, MCP servers and NSSM services are not
started. The context-store **agent dispatcher is disabled**
(`HEKATE_DISABLE_DISPATCHER=1`), so editing a node never spawns a claude or
gemini run.

## Isolation from the Sisyphus deployment

| | Local profile | NSSM / `docker-compose.yml` |
|---|---|---|
| DB | `127.0.0.1:5434`, compose project `hekate-local`, volume `hekate-local_pgdata` | `0.0.0.0:5433`, container `code-storage-db`, volume `pgdata` |
| Api | `http://127.0.0.1:5103`, built to `.hekate-local/api-bin` | `0.0.0.0:5102` from `C:\Hekate` |

- Override the ports with `HEKATE_LOCAL_DB_PORT` and `HEKATE_LOCAL_API_PORT`.
- A non-loopback connection string is refused.
- If a port is already in use, `start` reports it and stops nothing.
- The Api's environment (`CODESTORAGE_CONNSTR`, `HEKATE_API_URLS`, ...) is
  passed to that child process only. Nothing is written to user or machine
  environment, so existing services are unaffected.

## Ownership and shutdown

`.hekate-local/state.json` records exactly what `start` created. Each record is
checked again before it is acted on:

- **Api process:** the PID must match the recorded process start time, and the
  command line must contain this workspace's `.hekate-local/api-bin/Api.dll`.
  A reused PID is never killed.
- **Container:** the compose project label must be `hekate-local`, and
  `com.hekate.local.workspace` must be this checkout.

`stop` first asks the Api to shut down through `POST /api/local/shutdown`. That
endpoint is loopback-only, needs the per-start token, and exists only when the
launcher started the Api. If the Api is still running after 20 seconds, `stop`
force-kills it and reports `forced (...)` instead of `graceful`.

Anything that cannot be stopped stays in the ledger and `stop` reports
`partial` with exit code 1. Run `stop` again to retry. The tool never runs
`docker compose down` and never removes volumes.

If `start` fails part-way, it rolls back only what that run started, records
any container compose created, and keeps the volume.

Operations are serialized by `.hekate-local/lock`, and state writes are atomic.
Only a sharing violation is reported as "another operation in progress"; any
other failure to open the lock file is reported as itself.

`status` does not take the lock. While a `start` is running it can briefly
report `degraded`, because the container is up before the Api is ready.

## Readiness

`GET /api/health/ready` returns 200 only when all of these pass:

- Postgres answers
- the `age` and `vector` extensions exist
- a real Cypher `MATCH (n) RETURN count(n)` against `code_graph` succeeds
- `nodes` and `node_attributes` exist

Ollama and other inference are not checked. The older `/api/health` is unchanged.

On failure the endpoint returns 503 with a stable category: `failed_step`, plus
`failure` set to `auth_failed` or `<step>_unavailable`. It never returns raw
driver messages, because the route is also reachable on the production bind.
The exception type and SQLSTATE are written to the Api console log.

The compose health check uses TCP (`pg_isready -h 127.0.0.1`) with a 120 s
`start_period`. A socket check would pass too early, while first-time
initialization is still running a temporary server.

## Backup and restore

- `backup` dumps the `code_storage` database: plans, nodes and the AGE graph.
  The `orchestration` database in the same container is not included, because
  this profile does not use it.
  - The archive is checked with `pg_restore -l`.
  - It is copied to a unique `<file>.<guid>.partial`, then moved into place
    without overwriting, so an interrupted backup never looks finished. An
    existing target is refused.
  - The result reports `Bytes` and `Sha256` for the finished file.
- `restore` only creates a **new** database. `-TargetDatabase` is required.
  `code_storage`, `orchestration`, `postgres` and `template*` are refused, as is
  any name that already exists.
- **AGE catalog repair (target only).** A logical dump/restore of an AGE database
  keeps the *source* schema OID in `ag_graph.graphid` and `ag_label.graph`, so
  Cypher fails with "graph with oid N does not exist" (apache/age#2503).
  - After `pg_restore`, `restore` repairs the **new** target database in one
    atomic statement. It drops `fk_graph_oid`, remaps the OIDs, then re-adds the
    exact `pg_get_constraintdef` definition.
  - Only the supported shape is repaired: exactly one graph, and the FK as AGE
    defines it. Anything else fails closed.
  - The repair is idempotent. The result's `AgeRepair` field says `repaired`,
    `already-consistent` or `failed: …`.
  - The live `code_storage` database is never modified.
- After restoring, it checks that the graph is actually usable: in a session
  with `LOAD 'age'` and the AGE search path, it runs Cypher vertex and edge
  counts against `code_graph`, and compares the graph's labels with the live
  `code_storage`. Missing labels, a failed Cypher query, a failed node count or
  a `pg_restore` error make the result `restored-with-errors` with exit 1.
- The disposable database is left in place for inspection. Every result and
  every error after `createdb` names it and gives the `dropdb` command.

## Known limits

- `HEKATE_LOCAL_PG_PASSWORD` applies only when the volume is first initialized.
  It may only contain letters, digits and `_ . @ % + : -`. After the first
  successful start, its SHA-256 fingerprint is stored in the state file:
  - A later `start` with a different value is refused before anything starts.
  - If the Api itself logs a `28P01` login failure, `start` says the password
    is the cause instead of timing out.
  - If you deliberately recreate the volume with a new password, remove
    `pgPasswordSha256` from `.hekate-local/state.json`.
- Single instance per checkout. Two checkouts cannot run at the same time,
  because the compose project name is fixed.
- Logs are kept per start in `.hekate-local/logs/api-<time>.{out,err}.log`.
- `start` returns promptly even when its output is captured through a pipe.
  Before launching the Api, the launcher marks its own standard handles as
  non-inheritable, and the Api gets explicit stdin/stdout/stderr files. Without
  this, the Api would hold the caller's pipe open until it exits.
- Separate Api bug, not the launcher: `POST /api/projects` without `rootPath`
  returns 500, because `projects.root_path` is NOT NULL. Pass a `rootPath`.

## Tests

The tests use Pester 3.4, which ships with Windows. Docker, dotnet, processes
and HTTP are mocked; one test runs the real wrapper against a fake `docker.cmd`
to verify the exact arguments passed.

```powershell
pwsh -NoProfile -Command "Import-Module Pester -RequiredVersion 3.4.0; Invoke-Pester scripts/local/HekateLocal.Tests.ps1"
```
