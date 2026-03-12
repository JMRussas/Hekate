# 008 — Role-Based Agent Architecture

## The Insight

The current pipeline hardcodes behavior at each step: IntentClassifier uses regex patterns, ContextAssembler runs fixed queries per intent, PromptBuilder applies one persona. But each step is really a **role** — a bundle of skills, context scope, and behavioral guidance. Formalizing roles as first-class concepts lets us:

1. Swap implementations (regex classifier → model-based interpreter) without rewiring the pipeline
2. Give each role exactly the tools and context it needs — no more, no less
3. Store role definitions as nodes in the DB — versionable, discoverable, hot-swappable
4. Let agents self-describe what role they're playing, making the system observable

## The Roles

### Interpreter

**The hardest role.** Takes ambiguous user input and resolves it into structured intent + relevant node references.

Current implementation: `IntentClassifier` with regex patterns ("what did we" → Recalling, "let's plan" → Planning). This works for demos but breaks on edge cases: "I was thinking about what we discussed regarding the voice thing" could be Recalling, Deepening, or Ideation depending on context.

The Interpreter role should:
- Classify intent via a model call (not regex), with the conversation's recent turns as context
- Resolve entity references: "the voice thing" → node ID of the voice input idea
- Detect compound intents: "recall what we said about auth and then plan the next step" → Recalling + Planning
- Output a structured `InterpretedInput` (intent, confidence, resolved_nodes[], sub_intents[])

**Why it's special:** Every other role has a clear contract — "take these nodes and produce a plan" or "take this plan and execute it." The Interpreter has to handle the full ambiguity of natural language. It's the only role where a model call is essential, not optional.

### Translator (Bidirectional)

Converts between the DB's node/edge world and the model's text world. Two directions:

**Inbound (model output → DB):**
- Response text → extracted nodes (ideas, decisions, questions, action_items)
- Plan text → structured plan nodes with attributes
- Code suggestions → decomposed code nodes
- Currently: `ExtractionService` handles response → extracted nodes

**Outbound (DB → model input):**
- Context nodes → XML prompt sections
- Plan tree → readable plan summary
- Code nodes → source text
- Currently: `PromptBuilder` + `PlanRenderer` + `CSharpGenerator`

The Translator doesn't decide WHAT to translate — it just does the conversion faithfully. The Router decides what context to pull; the Translator turns it into text the model can consume.

### Planner

Takes a goal + context and produces a structured plan (phases, steps, tasks, risks).

- **Skills:** create_plan_node, create_edge, query_related_plans
- **Context:** existing plans (avoid duplication), related decisions, relevant code structure
- **Persona:** methodical, risk-aware, breaks large work into atomic tasks
- **Output:** plan node tree with typed attributes (status, priority, dependencies)

### Executor

Takes a task node and produces the deliverable (code, config, documentation).

- **Skills:** read_node, write_node, decompose_code, materialize_code, run_build
- **Context:** task requirements, related code nodes, conventions, test specs
- **Persona:** focused, follows the plan exactly, flags blockers instead of improvising
- **Output:** completed deliverable + status update on the task node

### Reviewer

Takes a deliverable + its requirements and evaluates quality.

- **Skills:** read_node, query_related, create_finding_node
- **Context:** the deliverable, its spec, related patterns/conventions, previous review findings
- **Persona:** critical, looks for what's wrong, checks edge cases
- **Output:** findings (issues, suggestions, approvals) as nodes linked to the reviewed item

### Extractor

A specialized Translator (inbound only) that watches conversation output and pulls out structured knowledge.

- **Skills:** create_node, embed_node, create_edge
- **Context:** the response text, conversation history, existing nodes (to avoid duplicates)
- **Persona:** conservative — only extract clearly stated ideas/decisions, don't hallucinate structure
- **Output:** idea, question, decision, action_item nodes with embeddings

Currently: `ExtractionService` runs Haiku after each response. This role formalizes what it does and opens the door to richer extraction (conventions, gotchas, architecture_decisions — deferred from Plan 009).

## Role Definition as a Contract

Each role is defined by a typed contract:

```
Role {
  name: string                    // "interpreter", "planner", etc.
  allowed_skills: string[]        // which tools this role can use
  context_query: ContextStrategy  // how to assemble context for this role
  model_preference: string?       // "fast" (haiku), "capable" (sonnet), "best" (opus), or null (caller decides)
  persona_prompt: string          // system prompt fragment that sets behavior
  input_schema: NodeType[]        // what node types this role accepts as input
  output_schema: NodeType[]       // what node types this role produces
}
```

**`context_query`** is key — it determines what information the role sees. The Interpreter gets recent turns + intent history. The Planner gets related plans + decisions + code structure. The Executor gets the task spec + code nodes + conventions. This is the context router's job, but parameterized by role instead of hardcoded per intent.

**`allowed_skills`** creates a security boundary. The Reviewer can't write code. The Executor can't create plans. The Extractor can't call models. Each role has minimum necessary access.

## Roles as DB Nodes

Store role definitions as `role` nodes in the DB:

```
role "interpreter"
├── attr: allowed_skills = ["classify_intent", "resolve_entity", "semantic_search"]
├── attr: model_preference = "fast"
├── attr: persona_prompt = "You classify user intent..."
├── attr: context_strategy = "recent_turns:3,intent_history:5"
└── edge: INFORMED → role "interpreter" (temporal: v1 → v2 after tuning)
```

Benefits:
- **Versionable:** temporal edges track when a role's definition changed and why
- **Discoverable:** agents can query "what roles exist?" and "what can the Reviewer do?"
- **Hot-swappable:** change a persona prompt or skill set without redeploying code
- **Observable:** "which role handled this message?" is a graph query
- **Composable:** a "Senior Reviewer" role could extend "Reviewer" with additional skills

## Pipeline with Roles

Current pipeline:
```
User input → IntentClassifier → ContextAssembler → PromptBuilder → Model → ExtractionService → DB
```

Role-based pipeline:
```
User input → Interpreter → Router → Translator(outbound) → Specialist(role) → Translator(inbound) → DB
                │                        │                        │
                ├─ resolves intent       ├─ context → XML         ├─ Planner / Executor / Reviewer
                ├─ resolves entities     └─ per-role context      └─ role determines skills + scope
                └─ model call (fast)
```

The Router reads the Interpreter's output and picks the right Specialist role. The Translator wraps context assembly + prompt building (outbound) and extraction + storage (inbound). Each Specialist role has its own skill set and persona.

## Migration Path

This doesn't require a rewrite. Each step is incremental:

1. **Define role contracts in code** — TypeScript/C# interfaces matching the schema above
2. **Wrap existing components as roles** — IntentClassifier becomes the Interpreter role's implementation, ExtractionService becomes the Extractor, etc.
3. **Store role definitions as nodes** — seed the initial roles from code, then allow DB-driven overrides
4. **Replace regex classifier with model-based Interpreter** — the biggest behavioral change
5. **Add role metadata to debug events** — "this message was handled by Interpreter v2 → Planner v1"

Steps 1-3 are structural (no behavior change). Step 4 is the real upgrade. Step 5 makes it observable.

## What This Enables (Future)

- **Custom roles per project:** A game project might have a "Level Designer" role with access to game-specific tools. A backend project might have a "DBA" role that can suggest migrations.
- **Role-based access control:** Agents authenticate with a role, and the system enforces what they can do. No agent can accidentally delete production data if its role doesn't include `delete_node`.
- **Multi-agent handoff:** Agent A (Interpreter) produces structured intent → Agent B (Planner) produces plan → Agent C (Executor) implements. Each agent only needs the skills and context for its role.
- **Role performance tracking:** "The Interpreter classified correctly 94% of the time in v2 vs 78% in v1." Temporal edges on role nodes make this queryable.

## The Operator Role — Self-Healing Pipeline

The system needs a watchdog that monitors pipeline health and performs bounded self-repair. This is the **Operator** role — an SRE agent that observes, diagnoses, and fixes what's safe to fix.

### What the Operator Watches

- **Active pipelines**: current phase, elapsed time, expected duration
- **Service health**: Ollama reachable? CLI processes alive? DB connections available?
- **Metric trends**: embedding backlog growing? average generate time increasing?

### Self-Repair Actions (Safe, Idempotent, Reversible)

| Failure | Detection | Repair Action |
|---------|-----------|---------------|
| CLI process hung | generate phase > 90s | Kill process, retry pipeline step |
| Ollama unresponsive | embedding timeout 3x in 60s | Restart Ollama container |
| Ollama down (not restartable) | health check fails | Degrade to ILIKE fallback, warn user |
| Embedding backlog | >20 nodes with NULL embedding | Trigger backfill script |
| Context assembly empty | 0 nodes returned when DB has >100 embedded | Diagnostic: check coverage, report |
| Extraction failed | ExtractionService returned empty | Retry once, then skip gracefully |
| DB connection exhaustion | connection timeout | Wait 5s, retry, warn if persistent |

### Guardrails — What the Operator Cannot Do

- **No code modification** — cannot change source files
- **No schema changes** — cannot ALTER tables
- **No node deletion** — cannot remove knowledge from the DB
- **No external calls** — limited to local stack (Ollama, Docker, CLI processes)
- **No git operations** — cannot commit, push, or modify branches

### Action Budget + Escalation Chain

```
Severity    | Auto-repair budget           | Escalation
------------|-----------------------------|-----------
transient   | 3 retries, then warn user   | system message (yellow)
degraded    | 1 repair action per 5 min   | system message (orange)
broken      | 0 auto-repair, diagnose only | system message (red) + block pipeline
```

### Audit Trail

Every repair action is stored as a `watchdog_action` node in the DB:
```
watchdog_action "killed_hung_cli"
├── attr: trigger = "generate_phase_exceeded_90s"
├── attr: action = "kill_process"
├── attr: outcome = "retry_succeeded"
├── attr: duration_ms = 1200
├── attr: severity = "degraded"
└── edge: OBSERVED → pipeline_run node
```

This creates a queryable history: "What did the watchdog do this week?" and "What's the success rate of Ollama restarts?"

### Rule-Based vs Model-Based

Most Operator work is **rule-based** — it doesn't need an LLM to know "Ollama timeout → restart Ollama." The rules are stored as `watchdog_rule` nodes in the DB, hot-reloadable like trigger rules.

The **AI layer** activates when:
- A failure pattern doesn't match any rule → ask a model to diagnose
- Correlation analysis across sessions → "Ollama fails every time context assembly pulls >10 nodes — possible memory pressure"
- Suggesting new rules based on observed patterns → "I've seen this failure 8 times and always fixed it the same way — should I add a rule?"

### Role Contract

```
Role {
  name: "operator"
  allowed_skills: [kill_process, restart_service, retry_pipeline_step,
                   query_metrics, push_system_message, store_diagnostic,
                   check_service_health, trigger_backfill]
  context_query: active_pipelines + recent_failures + service_health + watchdog_rules
  model_preference: null  // rule-based by default, "fast" model for novel diagnostics
  persona: "Minimize user disruption. Fix what's safe to fix. Escalate what isn't.
            Never take an action you haven't taken successfully before without user approval."
}
```

### Implementation Layers

1. **Pipeline observability** — explicit phase SSE events with elapsed time + heartbeats during long waits
2. **Pipeline registry** — `ConcurrentDictionary<streamId, PipelineState>` tracking active pipelines, exposed via `/api/pipeline/active`
3. **Watchdog BackgroundService** — checks active pipelines every 5s, applies rules, pushes system messages
4. **Repair actions** — safe operations (kill, restart, retry) triggered by watchdog rules
5. **Metrics collection** — store pipeline_run nodes with timing data after each completion
6. **AI diagnostics** — model-based diagnosis for novel failures (future, needs metrics data first)

Layers 1-3 are the immediate implementation. Layers 4-6 evolve as we collect data.

## Open Questions

**Q1: Should the Interpreter use a dedicated model call or piggyback on the main model call?**
Proposed answer: Dedicated fast model call (Haiku). The Interpreter runs BEFORE the main call — its output determines which Specialist role handles the request. Cost: ~100 tokens per classification. Worth it for accuracy over regex.

**Q2: How do compound intents work? ("Recall X and then plan Y")**
Proposed answer: The Interpreter outputs a list of sub-intents. The Router sequences them: Recalling first (to get context), then Planning (with recalled context as input). Each sub-intent maps to a Specialist role.

**Q3: Should roles be project-scoped or global?**
Proposed answer: Global defaults with project-level overrides. The core roles (Interpreter, Translator, Planner, Executor, Reviewer, Extractor) exist globally. Projects can add custom roles or override persona prompts.

**Q4: When should role-based pipeline replace the current one?**
Proposed answer: Not yet. The current pipeline works and Plan 009 just proved the context assembly layer. Roles should be the focus of a future plan (010 or 011) after the current system is dog-fooded enough to identify specific classification failures that regex can't handle.

**Q5: Should the Operator role use a model for diagnosis?**
Proposed answer: Start rule-based only (no model calls). After collecting 50+ pipeline_run metrics, evaluate whether a model-based diagnostic layer adds value. The rules handle 90% of cases. The model handles the long tail — novel failures, correlation across sessions, and suggesting new rules.
