# Local plan-only profile: live validation record

**Date:** 2026-10-06
**Host:** fenrir (Windows 11; Sisyphus offline)
**Run by:** claude-hekate. codex-hekate independently ran some steps; those are marked *(codex)*.

This records what was actually run against a real container runtime. It is evidence for the increment 1 live gate. It is not a general support claim.

## Environment

| Item | Value |
|---|---|
| PowerShell | 7.6.6 (scripts require 7.5 or later) |
| .NET SDK | 10.0.302 (Api targets `net10.0`) |
| Docker Desktop | 29.8.1 client/server, `linux/amd64`, per-user install at `%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin` |
| Docker Compose | v5.5.1 |
| Image | `hekate-local-postgres:latest` (`49f671908f99`, 871 MB), built from `context-store/Dockerfile.postgres`: `apache/age:release_PG16_1.6.0` plus pgvector v0.8.0 |
| Compose project / volume | `hekate-local` / `hekate-local_pgdata` |
| Ports | DB `127.0.0.1:5434`, Api `127.0.0.1:5103` |

Docker's bin directory was prepended to `PATH` **for the launcher's child process only**, because the session started before Docker was installed. *(codex)* For codex-hekate's child shell, a normal Windows `PATHEXT` was also required: with an inherited `PATHEXT=.CPL`, `docker` resolved to the extensionless shell script instead of `docker.exe`.

## Gates

| Step | Result | Evidence |
|---|---|---|
| S0 runtime | PASS | `docker version` / `compose version` / `info` all exit 0; OSType `linux`; engine had no containers, images or volumes beforehand |
| S1 unit tests | PASS | Pester 3.4, `HekateLocal.Tests.ps1`: 45 tests at start, 49 after the fixes below, all passing |
| S2 status before start | PASS | `stopped`, exit 1 |
| S3 start | **FAIL, then PASS** | Attempt 1: readiness reported `age_graph: false` (`InvalidCastException`). Rollback worked: the Api was stopped gracefully, the container stopped, the volume kept, and the state kept the container with `api: null`. **Fix:** the readiness probe now reads `v::text` (Npgsql has no `agtype` mapping). Attempt 2: `started`, every readiness check true, including `dispatcher_disabled` |
| S4 status | PASS | Fresh process `status -Json` → `ready`, exit 0. *(codex)* independent `GET /api/health/ready` returned 200 with every check true |
| S5 plan round-trip | PASS *(codex)* | Disposable project `codex-local-validation-20261006`: plan `a2746612-…` → phase → T1/T2, plus a `DEPENDS_ON` T2→T1 edge read back through the API |
| S6 dispatcher off | PASS | Disposable project `claude-hekate-s6-dispatcher-check`: task status set to `completed`. After 10 s there were 0 new claude/gemini/codex processes, and the log's only dispatcher line is `[INFO] Agent dispatcher disabled` |
| S7 graceful stop | PASS | `stopped Api pid 62124 (graceful), stopped database container 9fedf9a14ef9 (volume kept)`, exit 0 |
| S8 persistence | PASS | Plan tree, edges and the S6 node are identical key by key after **two** stop/start cycles. *(codex)* independent identical readback |
| S9 idempotent start | PASS | `already-running`; same Api PID; one container |
| S10 backup | PASS | `.hekate-local/backups/code_storage-20261006-135530.dump`, 62,453 bytes, SHA-256 `22441579FF9FCAA26EE88F24C1DC336B994555819C39826786FE1A8DD6EF101A`. *(codex)* separate backup, 62,063 bytes, SHA-256 `3C523A0C…76DA8E` |
| S11 restore | **FAIL, then PASS** | *(codex)* first restore: relational rows restored, but Cypher failed with `graph with oid … does not exist` (AGE logical-restore OID bug, apache/age#2503). **Fix:** a target-only catalog repair, described below. Then a restore into the new database `hekate_restore_check` gave `restored`, `AgeRepair=repaired`, 8 nodes (= live), Cypher 8 vertices and 1 edge, no missing labels. Rerunning the repair → `already-consistent`; `graphid = namespace::oid`; FK validated. The source `code_storage` was untouched |
| S12a password mismatch | PASS *(codex)* | `start` refused with "differs from the password last verified … Nothing was started", exit 1 |
| S12a existing restore target | PASS *(codex)* | Restore into an existing database refused before any change |
| S12b port conflict | PASS | With `127.0.0.1:5434` held by another process: `start` refused ("already in use … nothing else was stopped"), exit 1. The holder survived and no container started |
| S13 final stop | PASS | `stopped`; status `stopped (volume kept)`; container exited; volume and image kept |

No production database, NSSM service or Sisyphus host was touched. No model CLI or provider call was made. The whole stack used its own Compose project, volume and loopback ports.

## Defects found and fixed during validation

1. **Readiness probe cast.** `ExecuteScalarAsync` on an `agtype` column threw `InvalidCastException`. Fixed in `context-store/Api/Program.cs` by casting to `::text`.
2. **AGE restore OIDs.** A logical restore keeps the source schema OID in `ag_graph.graphid` and `ag_label.graph`, as described in [Apache AGE issue 2503](https://github.com/apache/age/issues/2503).
   - `Restore-HekateLocal` now runs `Repair-HLAgeCatalog` on the **new target only**.
   - It is one atomic statement: guard the supported shape (exactly one graph; `fk_graph_oid` as AGE defines it), capture `pg_get_constraintdef`, drop the FK, remap the label and graph OIDs, re-add the exact definition, post-check.
   - It fails closed: any error or a missing outcome gives `restored-with-errors`.
   - It is idempotent.
3. **Pipe handle inheritance.** The Api inherited the launcher's standard handles, so any caller that captured `start` output through a pipe hung until the Api exited.
   - Fixed: the launcher clears `HANDLE_FLAG_INHERIT` on its own standard handles before launching, and gives the Api an empty stdin file.
   - Live regression: `start … | cat` returned in 12 s with the Api still running.
4. **`$LASTEXITCODE` under StrictMode.** Docker and dotnet calls now go through `Invoke-HLNative`. It clears the code first and reports 127 when a command can't run or sets no exit code, instead of throwing.

## Known exclusions and limitations

- **Windows only:** the launcher uses `Get-CimInstance Win32_Process`, `Get-NetTCPConnection` and kernel32. It needs PowerShell 7.5+ and a normal `PATHEXT`.
- **Pre-existing Api bug, not the launcher:** `POST /api/projects` without `rootPath` returns 500 (`23502`, `projects.root_path` is NOT NULL while the request field is optional). Validation passed a `rootPath`.
- **Restore repair scope:** only single-graph databases are repaired. Anything else fails closed.
- **Disposable leftovers kept for inspection:** the databases `hekate_restore_check`, `codex_restore_check_20261006` (the original failed restore, repaired in place by codex as a prototype), and `codex_restore_verified_20261006` (an independent fresh restore through the repaired wrapper). Both test projects remain in the volume. The volume and image were preserved.
- **Odin suite not fully green:** this gate does not cover the gods pipeline. A partial full run showed 18 failures in `test_reliability.py` and `test_integration.py`, unrelated to this profile and not investigated.

## Final independent review

codex-hekate reran all 49 Pester tests successfully. With stdout and stderr captured by a Python subprocess, `start` returned successfully in 11.3 seconds while the API remained running; `status` reported ready in 0.7 seconds; `stop` returned successfully in 1.3 seconds and reported graceful API shutdown and volume preservation.

An independent fresh restore of the earlier 62,063-byte archive into `codex_restore_verified_20261006` returned `restored`, `AgeRepair=repaired`, six relational nodes, six Cypher vertices and one edge, matching that archive's baseline. The source database was not changed by the repair. A supplementary live multi-graph rejection probe was not executed because the lifecycle owner had stopped the container; unsupported-shape rejection is covered by the mocked tests.

The API changes also built successfully over a clean archive of committed source, without the unrelated rebuild-edges, CodeService or SemanticEdgeResolver working-tree changes. Final runtime state: stopped, with the local volume and image retained.
