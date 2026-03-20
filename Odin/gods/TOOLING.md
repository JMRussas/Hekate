# Language Tooling Matrix

Each language has analysis tooling that determines how deep planning can go (L1-L5).
L5 = executable spec — a tool writes the code directly, no LLM needed.

## Levels

| Level | Name | What's defined |
|-------|------|---------------|
| L1 | Rough tasks | Title, type, wave |
| L2 | Specs | Description, affected files, deps, complexity |
| L3 | Implementation | Code patterns, test strategy, edge cases |
| L4 | Exact changes | Method signatures, return types, parameter types |
| L5 | Executable spec | Full code body — tool applies directly |

## Languages

### C# — Roslyn (L5)

- **Service**: HekateServer (port 5110)
- **Engine**: HekateMcp.Server.exe (Roslyn-powered)
- **SDK**: .NET 8.0 + 10.0
- **Capabilities**: Full AST, type resolution, find implementations/usages/patterns, contract checking, project dependency graph, LLM consensus, 32 MCP tools
- **Max depth**: L5

### Python — Jedi + Worker (L5)

- **Service**: HekatePythonWorker (port 9200)
- **Engine**: Jedi 0.19.2 + HekateMcp.Worker.Python
- **Runtime**: Python 3.11 (NOT 3.14 — broken FastAPI)
- **Capabilities**: Type inference + completion, find definitions/references/usages, module resolution + import validation, function signatures + return types, AST analysis, pattern matching
- **Max depth**: L5

### TypeScript — TS Compiler + Worker (L5)

- **Service**: HekateTypeScriptWorker (port 9202)
- **Engine**: HekateMcp.Worker.TypeScript
- **Runtime**: Node.js 24+
- **Capabilities**: Full type system via TS compiler API, find definitions/references/implementations, interface + type resolution, import graph + dependency analysis, AST analysis, JSX/TSX React component analysis
- **Max depth**: L5

### C++ — Worker (L3)

- **Service**: HekateCppWorker (port 9201)
- **Engine**: HekateMcp.Worker.Cpp
- **Capabilities**: Basic AST analysis, find definitions + usages, pattern matching, include graph
- **Limitations**: No full type resolution (no Clang integration), L4/L5 requires manual verification
- **Max depth**: L3

## How Planning Uses This

`suggest_target_level()` in `plan_levels.py` checks:
- `has_roslyn=True` → C# project → target L5 for simple/medium
- `has_jedi=True` → Python project → target L5 for simple/medium
- `has_ts_compiler=True` → TypeScript project → target L5 for simple/medium
- Complex tasks → L3 regardless (LLM needs room to explore)
- Research/docs → L2 (no code to analyze)

The pipeline detects language from the project's file extensions and sets the appropriate flag before calling `suggest_target_level()`.

## CLI Provider Wrappers

Each CLI executor is wrapped in a provider class at `gods/providers/`. Providers encapsulate binary paths, flags, env vars, output parsing, and error classification. Hermes uses providers instead of building CLI commands inline.

### Usage

```python
from gods.providers.claude import ClaudeCodeProvider, ClaudeCodeConfig
from gods.providers.gemini import GeminiCLIProvider, GeminiCLIConfig
from gods.providers.base import ProviderRegistry

# Registry discovers all providers
registry = ProviderRegistry()
registry.register_defaults()

# Get provider by name (from dispatch_command)
provider = registry.get("claude_code")

# Override config per task
task_provider = provider.with_config(
    model="opus",
    max_turns=10,
    max_budget_usd=1.0,
    worktree="task-123",
    append_system_prompt="You are working on a FastAPI backend...",
    json_schema={"type": "object", "properties": {"files_changed": {"type": "array"}}},
    effort="high",
)

# Execute
result = await task_provider.execute(prompt="Add health endpoint", cwd="/path/to/project")
# result: StandardResult(output, cost_usd, prompt_tokens, completion_tokens, model, narration, exit_code)
```

### Response Validation

All LLM responses (from any provider or gateway) should be validated with `gods/providers/response_validator.py`:

```python
from gods.providers.response_validator import validate_verdict, validate_review, extract_json

# Verification responses
verdict = validate_verdict(gateway_response_text)
# Always returns: {"verdict": "passed|gaps_found|human_needed", "confidence": float, "feedback": str}
# Handles: clean JSON, markdown fenced, arrays, prose fallback

# Code review responses
review = validate_review(gateway_response_text)
# Always returns: {"verdict": "approved|changes_requested", "feedback": str}

# Raw JSON extraction
data = extract_json(any_llm_text)
# Returns parsed JSON or None — handles fencing, embedding, arrays
```

### Claude Code Provider — All Flags

`ClaudeCodeConfig` supports every `claude` CLI flag:

| Config field | CLI flag | Purpose |
|-------------|----------|---------|
| `model` | `--model` | Model selection (sonnet, opus, full name) |
| `fallback_model` | `--fallback-model` | Auto-fallback on overload |
| `allowed_tools` | `--allowedTools` | Tools that execute without permission |
| `disallowed_tools` | `--disallowedTools` | Tools removed from context |
| `tools` | `--tools` | Restrict available tools |
| `system_prompt` | `--system-prompt` | Replace entire system prompt |
| `system_prompt_file` | `--system-prompt-file` | Replace from file |
| `append_system_prompt` | `--append-system-prompt` | Add to default prompt |
| `append_system_prompt_file` | `--append-system-prompt-file` | Add from file |
| `output_format` | `--output-format` | text, json, stream-json |
| `json_schema` | `--json-schema` | Validated structured output |
| `input_format` | `--input-format` | text, stream-json |
| `max_turns` | `--max-turns` | Limit agentic turns |
| `max_budget_usd` | `--max-budget-usd` | Cost cap per task |
| `permission_mode` | `--permission-mode` | default, plan, bypassPermissions |
| `dangerously_skip_permissions` | `--dangerously-skip-permissions` | Skip all permissions |
| `verbose` | `--verbose` | Required for stream-json |
| `session_id` | `--session-id` | Resume specific session |
| `resume` | `--resume` | Resume by ID or name |
| `continue_session` | `--continue` | Continue most recent session |
| `fork_session` | `--fork-session` | Fork when resuming |
| `name` | `--name` | Session display name |
| `no_session_persistence` | `--no-session-persistence` | Don't save session |
| `worktree` | `--worktree` | Isolated git worktree |
| `mcp_config` | `--mcp-config` | MCP server config |
| `strict_mcp_config` | `--strict-mcp-config` | Only use specified MCP |
| `add_dirs` | `--add-dir` | Additional working directories |
| `effort` | `--effort` | low, medium, high, max (Opus only) |
| `include_partial_messages` | `--include-partial-messages` | Streaming partial events |
| `no_chrome` | `--no-chrome` | Disable browser (default: True) |
| `debug` | `--debug` | Debug mode with category filter |

### Gemini CLI Provider — All Flags

`GeminiCLIConfig` supports every `gemini` CLI flag:

| Config field | CLI flag | Purpose |
|-------------|----------|---------|
| `model` | `--model` | auto, pro, flash, flash-lite |
| `approval_mode` | `--approval-mode` | default, auto_edit, yolo |
| `sandbox` | `--sandbox` | Sandboxed execution |
| `output_format` | `-o` | text, json, stream-json |
| `extensions` | `--extensions` | Extension list |
| `allowed_mcp_server_names` | `--allowed-mcp-server-names` | MCP server filter |
| `resume` | `--resume` | Resume session (latest or ID) |
| `include_directories` | `--include-directories` | Additional workspace dirs |
| `debug` | `--debug` | Verbose logging |
| `screen_reader` | `--screen-reader` | Accessibility mode |
| `experimental_acp` | `--experimental-acp` | Agent Code Pilot mode |

### Environment Variables

Providers manage env vars automatically:

| Provider | Env var | Value | Reason |
|----------|---------|-------|--------|
| Claude Code | `CLAUDECODE` | **removed** | Prevents nested session detection |
| Gemini CLI | `GEMINI_FORCE_FILE_STORAGE` | `true` | File-based OAuth (not Windows Credential Manager) |

### Error Classification

Providers classify CLI errors into categories for diagnosis:

| Category | Meaning | Diagnosis action |
|----------|---------|-----------------|
| `nested_session` | Claude launched inside Claude | Strip CLAUDECODE env var |
| `rate_limit` | API rate limit hit | Wait or switch provider |
| `auth_error` | Authentication failed | Check credentials |
| `context_overflow` | Too many tokens | Reduce prompt/context |
| `overloaded` | Model overloaded | Use fallback_model |
| `quota_exceeded` | Gemini quota hit | Switch provider |
| `model_not_found` | Invalid model name | Fix model config |
| `sandbox_error` | Gemini sandbox issue | Disable sandbox |
