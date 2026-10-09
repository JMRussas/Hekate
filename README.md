# Hekate — AI project orchestration

Hekate is a local engineering prototype for planning and executing software work
with AI roles. Its development workflow combines **multi-pass plan review**,
**test-first development**, and **cross-model code review**. It brings planning,
CLI execution, task coordination, code context and operator interfaces into one
repository.

Built by [Justin M Russas](https://github.com/JMRussas). The source on `main` includes
the current committed implementation. Deployment configuration and runtime data
are local to each installation.

## Start here

- [Run the supported local plan-only profile](scripts/local/README.md).
- [Understand the roles](GODS.md) and [plan-to-code model](PLAN_TO_CODE.md).
- [Inspect the October 6 execution-ledger assessment](context-store/plans/015-execution-ledger-inventory.md).
- [Review managed plan contracts](context-store/plans/012-plan-node-contracts-v1.md)
  and [open issues](context-store/plans/037-open-issues-register.md).
- [Read the ChatAgent integration boundary](CHATAGENT-INTEGRATION-HANDOFF.md).

## What the system does

Athena develops plans through successive levels of detail, requests review by
another model and can generate test specifications. Odin coordinates dependencies
and execution waves. Hermes runs configured model CLIs. Mimir reviews output and
code through the configured model gateway. Hephaestus handles Git operations;
Tyche records reported usage. These are implementation roles, described in
[GODS.md](GODS.md), rather than separate claims of autonomous human judgment.

The repository also contains a managed PlanStore with explicit dependencies,
claim receipts, attempt identities and acceptance decisions, a read-only plan
browser, and supervised local task tooling. That managed execution path and the
legacy gods engine have different ledgers and completion semantics.

The October 6 inventory found that the gods engine overwrites task output on
retries, has no dedicated durable per-attempt claim history in its inspected
paths, and can pass output when the model review service is unavailable. Code
review can be advisory. A completed task or a model's assessment therefore does
not establish independently verified correctness. The inventory is a dated source
assessment; it does not identify the database or configuration of a running
installation. Newer managed-plan validation is documented separately under
[context-store/plans](context-store/plans).

## Repository map

| Directory                  | Purpose                                                      |
| -------------------------- | ------------------------------------------------------------ |
| `Odin/`                    | Event-driven roles, planning and execution pipeline          |
| `orchestration/`           | Python API, legacy workflow services and React dashboard     |
| `context-store/`           | .NET code/context services, PostgreSQL/AGE and managed plans |
| `hades/`                   | Windows service administration and deployment                |
| `llm-gateway/`             | Model/provider gateway                                       |
| `extension/`               | Editor integration                                           |
| `scripts/local/`           | Owned local plan-store launcher and supervised experiments   |
| `prototypes/`, `superset/` | Exploratory interfaces and supporting experiments            |

[CLAUDE.md](CLAUDE.md) provides development and deployment notes;
[ANALYSIS.md](ANALYSIS.md) records an earlier architecture review. Historical
validation fixtures retain their original host paths and measured bytes. They
are evidence, not installation defaults.

## Run locally

The supported plan-only profile requires Windows, PowerShell 7.5+, Docker with
Compose, and the .NET 10 SDK. Node/npm is needed for the optional browser UI.

```powershell
git clone https://github.com/JMRussas/Hekate.git
cd Hekate
$env:HEKATE_LOCAL_PLAN_CONTRACT = '1'
pwsh scripts/local/hekate-local.ps1 start
pwsh scripts/local/hekate-local.ps1 status
```

This starts an owned PostgreSQL/AGE container and a loopback context-store API.
The agent dispatcher is disabled; browsing or editing plans does not start model
workers. The launcher preserves data on stop:

```powershell
pwsh scripts/local/hekate-local.ps1 stop
```

For the optional UI, open another terminal:

```powershell
cd context-store/ui
npm ci
$env:HEKATE_UI_API_TARGET = 'http://127.0.0.1:5103'
npm run dev
```

The UI uses `http://localhost:5179`. See the [local runbook](scripts/local/README.md)
for backups, API checks, ownership rules and exact prerequisites. Full gods-engine
and NSSM deployment is a separate configured environment; it is not started by
this quickstart.

## Checks and configuration

Pure managed-plan rule tests require the .NET 10 SDK and do not start services:

```powershell
dotnet test context-store/PlanContracts.Tests/PlanContracts.Tests.csproj
```

Python tooling uses `uv` with each component's maintained dependency inputs. The
[supervised tooling runbook](scripts/local/supervisor_e1/README.md) distinguishes
pure fixtures, live API checks and opt-in model execution.

Deployment helpers resolve the checkout or use `HEKATE_SOURCE`; set it explicitly
when source and deployment directories differ. `HEKATE_ROOT` selects the engine
deployment destination. `HEKATE_PYTHON` selects the configured Python executable
for deployment/restart helpers and Python service entries; otherwise helpers use
`python` on PATH and Hades uses its current interpreter. Set `HEKATE_NSSM` when
NSSM is absent from the service's PATH. Optional local conversation mining reads
only directories explicitly listed in `HEKATE_CLAUDE_LOG_DIRS`, separated by the
platform's path separator. Machine-local settings, generated plans, SQLite runtime
data and learned conversation exports are ignored by Git.

## License

[GNU Affero General Public License v3.0 or later](LICENSE), matching Agent Insights,
Orchestration Engine and Tiered Moderation Agent.

The copyright notice and AGPL-3.0-or-later grant are preserved in [NOTICE](NOTICE).
