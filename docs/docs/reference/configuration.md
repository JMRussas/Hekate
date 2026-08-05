# Configuration Reference

Runtime configuration is loaded from `orchestration/config.json` at startup. Values can be overridden by environment variables (dot path converted to uppercase with underscores, e.g., `ollama.url` checks `OLLAMA_URL`).

Configuration is accessed via `cfg("path.to.key", default)` from `orchestration/backend/config.py`.

---

## server

HTTP server settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `server.host` | `string` | `0.0.0.0` | Bind address |
| `server.port` | `int` | `5200` | Listen port. Must be 1-65535. |
| `server.cors_origins` | `string[]` | `["http://localhost:5173", ...]` | Allowed CORS origins. Must start with `http://` or `https://`. Use `*` with caution. |

---

## anthropic

Anthropic API and model configuration.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `anthropic.planning_model` | `string` | `claude-sonnet-4-6` | Model used for plan generation |
| `anthropic.max_concurrent` | `int` | `3` | Max concurrent API calls |
| `anthropic.timeout` | `float` | `120` | API request timeout in seconds. Must be > 0. |
| `anthropic.models` | `object` | `{}` | Model ID map by tier (e.g., `{"haiku": "claude-haiku-4-5-20251001"}`) |

The `ANTHROPIC_API_KEY` environment variable enables direct API access. Without it, all LLM calls route through CLI providers.

---

## ollama

Local Ollama model server.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `ollama.url` | `string` | `http://localhost:11434` | Primary Ollama URL |
| `ollama.hosts` | `object` | `{"local": "http://localhost:11434"}` | Named host map for multi-GPU setups |
| `ollama.default_model` | `string` | `qwen3.5:latest` | Default generation model |
| `ollama.embed_model` | `string` | `nomic-embed-text` | Embedding model |
| `ollama.embed_timeout` | `float` | `30.0` | Embedding request timeout (seconds) |
| `ollama.generate_timeout` | `float` | `120.0` | Generation request timeout (seconds). Must be > 0. |

---

## comfyui

ComfyUI image generation.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `comfyui.hosts` | `object` | `{"local": "http://localhost:8188"}` | Named ComfyUI host map |
| `comfyui.default_checkpoint` | `string` | `sd_xl_base_1.0.safetensors` | Default model checkpoint |

---

## llm_gateway

LLM Gateway proxy (port 5210). Routes LLM calls through CLI OAuth tokens.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `llm_gateway.url` | `string` | `http://localhost:5210` | Gateway base URL |

---

## execution

Task execution engine settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `execution.max_concurrent_tasks` | `int` | `3` | Max tasks running in parallel |
| `execution.tick_interval_sec` | `float` | `2.0` | Pipeline tick interval (seconds) |
| `execution.max_tool_rounds` | `int` | `10` | Max tool-use rounds per LLM call |
| `execution.ollama_max_tool_rounds` | `int` | `5` | Max tool rounds for Ollama specifically |
| `execution.max_history_rounds` | `int` | `4` | Max conversation history rounds to retain |
| `execution.default_max_tokens` | `int` | `4096` | Default max output tokens |
| `execution.max_task_retries` | `int` | `5` | Max retry attempts per task |
| `execution.staleness_timeout_seconds` | `int` | `900` | Seconds before a running task is considered stuck (CLI timeout + grace) |
| `execution.external_claim_timeout_seconds` | `int` | `3600` | External executor claim expiry (seconds) |
| `execution.verification_enabled` | `bool` | `false` | Enable LLM-based verification (legacy, gods pipeline always verifies) |
| `execution.verification_model` | `string` | `claude-haiku-4-5-20251001` | Model for verification |
| `execution.verification_max_tokens` | `int` | `1024` | Max tokens for verification response |
| `execution.wave_checkpoints` | `bool` | `false` | Checkpoint between waves |
| `execution.context_forward_max_chars` | `int` | `2000` | Max chars of prior task output forwarded as context |
| `execution.checkpoint_on_retry_exhausted` | `bool` | `true` | Create checkpoint when retries exhausted |
| `execution.shutdown_grace_seconds` | `int` | `30` | Grace period for in-flight tasks on shutdown |
| `execution.resource_skip_seconds` | `int` | `30` | Skip duration when resources unavailable |
| `execution.knowledge_extraction_enabled` | `bool` | `true` | Extract reusable findings from task output |
| `execution.knowledge_extraction_model` | `string` | `claude-haiku-4-5-20251001` | Model for knowledge extraction |
| `execution.knowledge_extraction_max_tokens` | `int` | `1024` | Max tokens for extraction response |
| `execution.knowledge_injection_max_chars` | `int` | `3000` | Max chars of knowledge injected into task context |
| `execution.knowledge_min_output_length` | `int` | `200` | Min output length to trigger extraction |
| `execution.diagnostic_rag_enabled` | `bool` | `false` | Enable RAG for failure diagnosis |

---

## budget

Cost control limits.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `budget.daily_limit_usd` | `float` | `5.0` | Daily spending cap. Must be >= 0. |
| `budget.monthly_limit_usd` | `float` | `50.0` | Monthly spending cap. Must be >= 0. |
| `budget.per_project_limit_usd` | `float` | `10.0` | Per-project spending cap. Must be >= 0. |
| `budget.warn_at_pct` | `int` | `80` | Warning threshold as percentage of limit |

---

## model_pricing

Model cost lookup table. Keys are model identifiers, values are pricing objects.

```json
{
  "model_pricing": {
    "claude-sonnet-4-6": {
      "input_per_1m": 3.0,
      "output_per_1m": 15.0
    }
  }
}
```

Models without a pricing entry log a warning at startup and record costs as $0.00.

---

## auth

Authentication and authorization.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `auth.secret_key` | `string` | -- | JWT signing key. **Required**, min 32 chars. Env: `AUTH_SECRET_KEY`. |
| `auth.algorithm` | `string` | `HS256` | JWT algorithm |
| `auth.access_token_expire_minutes` | `int` | `30` | Access token TTL |
| `auth.refresh_token_expire_days` | `int` | `7` | Refresh token TTL |
| `auth.allow_registration` | `bool` | `true` | Allow new user registration |
| `auth.sse_token_expire_seconds` | `int` | `60` | SSE connection token TTL |
| `auth.login_lockout_threshold` | `int` | `5` | Failed login attempts before lockout. Must be >= 1. |
| `auth.login_lockout_window_seconds` | `float` | `300` | Lockout window duration. Must be > 0. |
| `auth.oidc_providers` | `object[]` | `[]` | OIDC provider configurations |
| `auth.oidc_redirect_uris` | `string[]` | `[]` | Allowed OIDC redirect URIs |

### OIDC Provider Configuration

Each entry in `auth.oidc_providers` requires:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Provider display name |
| `issuer` | Yes | OIDC issuer URL |
| `client_id` | Yes | OAuth client ID |
| `client_secret` | Yes | OAuth client secret |

---

## context_enrichment

Inject context store knowledge into task prompts.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `context_enrichment.enabled` | `bool` | `true` | Enable context injection |
| `context_enrichment.context_store_url` | `string` | `http://localhost:5102` | Context store API URL. Must be valid HTTP URL. |
| `context_enrichment.max_context_tokens` | `int` | `2000` | Max tokens of context to inject. Must be > 0. |
| `context_enrichment.include_prior_outcomes` | `bool` | `true` | Include prior task outcomes in context |

---

## telemetry_feedback

Store execution outcomes in the context store for learning.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `telemetry_feedback.enabled` | `bool` | `true` | Enable telemetry feedback loop |
| `telemetry_feedback.context_store_url` | `string` | `http://localhost:5102` | Context store API URL |
| `telemetry_feedback.embed_outcomes` | `bool` | `true` | Generate embeddings for outcomes |

---

## interrogation

Structured self-interrogation quality gate at decision points.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `interrogation.enabled` | `bool` | `false` | Enable 6-question quality gate |
| `interrogation.model` | `string` | `null` | Model to use (null = default) |

---

## review_cycle

Post-execution review loop.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `review_cycle.enabled` | `bool` | `false` | Enable execute/review/iterate cycle |
| `review_cycle.max_iterations` | `int` | `2` | Max review iterations |
| `review_cycle.auto_commit` | `bool` | `true` | Auto-commit after review passes |
| `review_cycle.pr_on_wave_complete` | `bool` | `true` | Create PR when wave completes |

---

## git

Git integration settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `git.enabled` | `bool` | `true` | Enable git operations |
| `git.commit_author` | `string` | `Orchestration Engine <orchestration@local>` | Git commit author |
| `git.branch_prefix` | `string` | `orch` | Branch name prefix |
| `git.non_code_output_path` | `string` | `.orchestration` | Directory for non-code outputs |
| `git.auto_pr` | `bool` | `true` | Auto-create PRs on completion |
| `git.pr_remote` | `string` | `origin` | Remote for PR creation |
| `git.command_timeout` | `int` | `30` | Git command timeout (seconds). Must be > 0. |

---

## hekate_mcp

Hekate code analysis MCP server.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `hekate_mcp.url` | `string` | `http://192.168.1.164:5110` | MCP server URL |
| `hekate_mcp.timeout` | `float` | `60.0` | Request timeout (seconds) |
| `hekate_mcp.default_project` | `string` | `""` | Default project for analysis |

---

## sentinel

Legacy sentinel configuration.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `sentinel.orchestrator_enabled` | `bool` | `false` | Enable sentinel orchestrator mode (legacy) |

---

## rag

RAG database configuration.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `rag.databases` | `object` | `{}` | Named SQLite DB paths for RAG (relative to project root) |
| `rag.embed_dimensions` | `int` | `768` | Embedding vector dimensions |
| `rag.diagnostic_ingest_path` | `string` | `""` | Path for diagnostic RAG ingestion |

---

## Startup Validation

`validate_config()` runs at application startup and checks for fatal configuration errors:

| Check | Severity | Condition |
|-------|----------|-----------|
| JWT secret key | Fatal | Missing, < 32 chars, or known placeholder |
| Server port | Fatal | Not 1-65535 |
| Budget limits | Fatal | Negative values |
| Timeouts | Fatal | Zero or negative |
| OIDC providers | Fatal | Missing required fields (name, issuer, client_id, client_secret) |
| Lockout config | Fatal | Threshold < 1 or window <= 0 |
| CORS origins | Fatal | Not valid HTTP(S) URLs |
| Git timeout | Fatal | Zero or negative when git enabled |
| Context enrichment URL | Fatal | Invalid URL when enabled |
| Telemetry URL | Fatal | Invalid URL when enabled |
| Anthropic API key | Warning | Not set (Ollama-only is valid) |
| Model pricing | Warning | Configured models without pricing entries |
| OIDC redirect URIs | Warning | Empty when providers exist |
| CORS wildcard | Warning | `*` origin configured |
| Git binary | Warning | Not found on PATH when git enabled |

---

## Environment Variable Overrides

Any config key can be overridden by an environment variable. The dot-notation path is converted to uppercase with underscores:

| Config Key | Environment Variable |
|------------|---------------------|
| `server.port` | `SERVER_PORT` |
| `ollama.url` | `OLLAMA_URL` |
| `budget.daily_limit_usd` | `BUDGET_DAILY_LIMIT_USD` |
| `auth.secret_key` | `AUTH_SECRET_KEY` |

The `AUTH_SECRET_KEY` environment variable is checked explicitly and takes precedence over `auth.secret_key` in config.json.
