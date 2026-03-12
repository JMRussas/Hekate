# Prompt Engineering — Orchestration Engine

Implementation details for how this project builds and parses LLM prompts.

## Planner Prompts (`backend/services/planner.py`)

### Prompt Structure

The planner system prompt is composed of two parts:

1. **Preamble** (`_PLANNING_PREAMBLE`) — shared across all rigor levels
   - Persona: "project planner for an AI orchestration engine"
   - `<task_guidelines>` — rules for task decomposition, types, dependencies
   - `<available_tools>` — tool catalog each task can request
   - Traceability: requirements numbered `[R1]`, `[R2]`, mapped to `requirement_ids` on tasks

2. **Rigor suffix** (`_RIGOR_SUFFIX_L1/L2/L3`) — varies by planning rigor
   - L1: flat `{summary, tasks[]}`, 4096 max_tokens, strict "ONLY JSON"
   - L2: phased `{summary, phases[], open_questions[]}`, 6144 max_tokens, strict "ONLY JSON"
   - L3: phased + risk/test `{summary, phases[], open_questions[], risk_assessment[], test_strategy{}}`, 8192 max_tokens, allows `<thinking>` block before JSON

### Why L3 Gets a Thinking Block

Complex plans benefit from the model reasoning through dependencies, risks, and trade-offs before committing to JSON structure. L1/L2 use "ONLY JSON" to save tokens since simpler plans don't need pre-reasoning. The JSON parser (`_extract_json_object`) naturally skips any `<thinking>` text because it scans for the first `{`.

### JSON Parsing Pipeline

LLM JSON output goes through a multi-stage parsing pipeline in `_extract_json_object`:

1. **Direct parse** — `json.loads(response_text)` (succeeds when output is clean JSON)
2. **Brace-balanced extraction** — scans for first `{`, counts depth, extracts the balanced object (handles markdown fences, preamble text, thinking blocks)
3. **Trailing comma cleanup** — `_strip_trailing_commas()` regex removes `,]` and `,}` patterns, then retries `json.loads` (handles LLM's common trailing comma habit)
4. **Failure** — raises `PlanParseError` if all stages fail

### Task Schema

Every task in the plan uses this schema (defined in `_TASK_SCHEMA`):

| Field | Type | Purpose |
|-------|------|---------|
| `title` | string | Short task title |
| `description` | string | Self-contained description for a fresh AI instance |
| `task_type` | enum | `code\|research\|analysis\|asset\|integration\|documentation` |
| `complexity` | enum | `simple\|medium\|complex` — drives model selection |
| `depends_on` | int[] | 0-based global task indices (across all phases) |
| `tools_needed` | string[] | Subset of available tools |
| `requirement_ids` | string[] | Which `[R1]`, `[R2]` etc. this task satisfies |
| `verification_criteria` | string | Concrete check for task completion |
| `affected_files` | string[] | Files this task will create or modify |

## Other LLM-Calling Code

| Service | File | Model | Purpose |
|---------|------|-------|---------|
| Verifier | `services/verifier.py` | Haiku | Post-completion output quality check |
| Knowledge Extractor | `services/knowledge_extractor.py` | Haiku | Extract reusable findings from completed tasks |
| Claude Agent | `services/claude_agent.py` | Sonnet/Haiku | Multi-turn task execution with tool use |
| Ollama Agent | `services/ollama_agent.py` | Local models | Free task execution for simple/research tasks |

## Gotchas

- **f-string brace escaping**: Literal JSON braces in f-strings must be doubled (`{{` and `}}`). Variable interpolation uses single braces (`{_TASK_SCHEMA}`). Easy to mix up.
- **Escaped backslashes in JSON parser**: The brace-counter handles `\"` correctly (escape flag skips next char). `\\"` also works: first `\` sets escape, second `\` is skipped, `"` processes normally as delimiter.
- **`_strip_trailing_commas` is regex-based**: It doesn't track string boundaries, so it could theoretically corrupt a string value containing `,]`. In practice this is extremely rare in plan JSON and the strict `json.loads` before it catches clean JSON without needing the strip.
