# Sentinel: New Capabilities, API Surface & MCP

This document specifies the new capabilities of the Sentinel as the orchestration brain, its API surface for external communication, and the MCP tools for direct interaction.

## 1. New Capabilities

The Sentinel's new role as the primary orchestrator introduces a range of capabilities for intelligent, dynamic control over the project lifecycle.

### 1.1. Planning Control

The Sentinel will have the authority to dynamically modify the project plan in response to real-time events, such as task failures, new information, or user directives.

-   **Re-planning:** Upon significant task failure or deviation, the Sentinel can trigger a re-planning phase. It will use the original plan, the current project state, and the failure context to generate a revised plan.
-   **Rigor Adjustment:** The Sentinel can adjust the rigor of task verification. For instance, if a set of tasks is consistently failing, it can increase the rigor for subsequent tasks in that wave, requiring more stringent verification or even human-in-the-loop (HITL) approval. Conversely, it can decrease rigor for tasks that are consistently succeeding.
-   **Task Splitting/Merging:** The Sentinel can decompose a complex, failing task into smaller, more manageable sub-tasks. It can also merge simple, related tasks into a single unit of work to optimize overhead.

### 1.2. Model Selection Control

The Sentinel will manage model assignments for tasks, allowing for dynamic adjustments based on performance and cost.

-   **Tier Reassignment:** If a task fails due to model limitations (e.g., context window, reasoning capability), the Sentinel can automatically re-dispatch the task to a higher-tier model.
-   **Cost Optimization:** For simple, repetitive tasks, the Sentinel can default to a lower-cost model. It will monitor success rates and switch to a higher-tier model only if necessary.
-   **Failure Pattern Analysis:** The Sentinel will track failure patterns associated with specific models and tasks. This data will inform its model selection strategy over time, building a heuristic for which model is best suited for a given task type.

### 1.3. Parallelism Control

The Sentinel will manage the concurrency of the executor worker pool to optimize for speed, cost, and resource utilization.

-   **Dynamic Concurrency:** The Sentinel will monitor the performance of the worker pool and the underlying system resources. It can increase or decrease the number of concurrent tasks to maintain optimal throughput without overloading the system.
-   **Resource-based Throttling:** If tasks require access to a shared, limited resource (e.g., a specific API, a database connection pool), the Sentinel can limit the number of concurrent tasks that access that resource.
-   **Dependency-aware Dispatch:** The Sentinel will analyze the task dependency graph to dispatch independent tasks in parallel, maximizing utilization of the worker pool.

### 1.4. Debug & Control Flags

The Sentinel will expose a set of flags for fine-grained control and debugging of the orchestration process. These flags can be set via the API or MCP tools.

-   **Pause/Resume:** Halt all orchestration activities, allowing for inspection of the current state. The system can be resumed from the point it was paused.
-   **Step-Through:** Execute one task or one decision loop iteration at a time, requiring explicit user approval to proceed to the next step.
-   **Event Injection:** Manually inject events into the Sentinel's event bus to test its response to specific scenarios (e.g., a simulated task failure, a new user request).
-   **Decision Override:** Intercept a decision from the Sentinel's reasoner and provide a different outcome. For example, forcing the selection of a specific model for a task.

### 1.5. State Persistence & Auditability

Every decision made by the Sentinel will be persisted to the database for auditability and traceability.

-   **Decision Log:** A new table, `sentinel_decisions`, will store a log of every decision made by the Sentinel, including the input state, the reasoning process, and the resulting command.
-   **State Snapshots:** The Sentinel will periodically snapshot the entire project state, providing a historical record that can be used for debugging and analysis.
-   **Audit Trail:** The decision log will serve as an audit trail, allowing developers and users to understand why the Sentinel took a specific action at a given point in time.

## 2. API Surface

The Sentinel's new role requires a dedicated API surface for communication with the extension and other external systems. The primary communication channel will be a set of RESTful endpoints.

### 2.1. Endpoints

-   `POST /sentinel/command`: The main endpoint for interacting with the Sentinel. The request body will contain the command and its parameters.
    -   **Commands:** `pause`, `resume`, `step`, `set_flag`, `inject_event`, `override_decision`.
    -   **Example:** `POST /sentinel/command` with body `{"command": "set_flag", "payload": {"name": "pause_on_failure", "value": true}}`

-   `GET /sentinel/state`: Retrieve the current state of the orchestration process, including the project plan, task status, and active flags.

-   `GET /sentinel/decisions`: Query the decision log, with support for filtering by time, task, and decision type.

-   `GET /sentinel/history`: Retrieve historical state snapshots.

### 2.2. Extension Communication

The VS Code extension will communicate directly with the Sentinel via these new endpoints. It will no longer interact with the Executor's API for orchestration control. The extension will use the `/sentinel/state` endpoint to update its UI and will send user actions (e.g., pausing the process, overriding a decision) to the `/sentinel/command` endpoint.

## 3. MCP Tools

A new set of MCP (Mission Control Protocol) tools will be created for direct, command-line interaction with the Sentinel.

-   `sentinel-cli`: A command-line interface for interacting with the Sentinel's API.
    -   `sentinel-cli state`: Get the current state.
    -   `sentinel-cli pause`: Pause the orchestration process.
    -   `sentinel-cli resume`: Resume the orchestration process.
    -   `sentinel-cli step`: Execute the next step.
    -   `sentinel-cli set-flag <flag_name> <value>`: Set a debug/control flag.
    -   `sentinel-cli inject-event <event_type> <payload>`: Inject an event.
    -   `sentinel-cli decisions --since=1h`: View recent decisions.

This comprehensive set of capabilities, APIs, and tools will empower the Sentinel to act as the intelligent core of the Hekate orchestration system, providing robust, flexible, and observable control over the entire project lifecycle.
