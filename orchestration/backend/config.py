#  Orchestration Engine - Configuration
#
#  Loads config.json and provides typed access to all settings.
#  Dot-notation path lookup: cfg("anthropic.models.haiku")
#
#  Depends on: config.json
#  Used by:    all backend modules

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "orchestration.db"

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------

_config: dict = {}


def _load_config(path: Path | None = None):
    """Load configuration from JSON file (internal — called once at import time).

    Module-level constants below are snapshots from _config.
    Do not call this function after import — constants won't update.
    """
    global _config
    config_path = path or CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. "
            "Copy config.example.json to config.json."
        )
    with open(config_path) as f:
        _config = json.load(f)


# Auto-load if config exists at import time
if CONFIG_PATH.exists():
    _load_config()


def cfg(path: str, default=None):
    """Get a config value by dot-notation path.

    Example: cfg("anthropic.models.haiku") -> "claude-haiku-4-5-20251001"
    """
    keys = path.split(".")
    val = _config
    for key in keys:
        if isinstance(val, dict) and key in val:
            val = val[key]
        else:
            return default
    return val


# ---------------------------------------------------------------------------
# Convenience constants
# ---------------------------------------------------------------------------

HOST = cfg("server.host", "0.0.0.0")
PORT = cfg("server.port", 5200)
CORS_ORIGINS = cfg("server.cors_origins", [
    "http://localhost:5173",
    f"http://localhost:{PORT}",
    "http://127.0.0.1:5173",
    f"http://127.0.0.1:{PORT}",
])

# Anthropic
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PLANNING_MODEL = cfg("anthropic.planning_model", "claude-sonnet-4-6")
MAX_CONCURRENT = cfg("anthropic.max_concurrent", 3)
API_TIMEOUT = cfg("anthropic.timeout", 120)

# Ollama
OLLAMA_HOSTS = cfg("ollama.hosts", {"local": "http://localhost:11434"})
OLLAMA_DEFAULT_MODEL = cfg("ollama.default_model", "qwen2.5-coder:14b")
OLLAMA_EMBED_MODEL = cfg("ollama.embed_model", "nomic-embed-text")
OLLAMA_EMBED_TIMEOUT = cfg("ollama.embed_timeout", 30.0)
OLLAMA_GENERATE_TIMEOUT = cfg("ollama.generate_timeout", 120.0)

# ComfyUI
COMFYUI_HOSTS = cfg("comfyui.hosts", {"local": "http://localhost:8188"})
COMFYUI_DEFAULT_CHECKPOINT = cfg("comfyui.default_checkpoint", "sd_xl_base_1.0.safetensors")

# RAG
RAG_DATABASES = cfg("rag.databases", {})
RAG_EMBED_DIMENSIONS = cfg("rag.embed_dimensions", 768)
RAG_DIAGNOSTIC_INGEST_PATH = cfg("rag.diagnostic_ingest_path", "")

# Diagnostic RAG
DIAGNOSTIC_RAG_ENABLED = cfg("execution.diagnostic_rag_enabled", False)

# Budget
BUDGET_DAILY = cfg("budget.daily_limit_usd", 5.0)
BUDGET_MONTHLY = cfg("budget.monthly_limit_usd", 50.0)
BUDGET_PER_PROJECT = cfg("budget.per_project_limit_usd", 10.0)
BUDGET_WARN_PCT = cfg("budget.warn_at_pct", 80)

# Execution
MAX_CONCURRENT_TASKS = cfg("execution.max_concurrent_tasks", 3)
TICK_INTERVAL = cfg("execution.tick_interval_sec", 2.0)
MAX_TOOL_ROUNDS = cfg("execution.max_tool_rounds", 10)
MAX_HISTORY_ROUNDS = cfg("execution.max_history_rounds", 4)
DEFAULT_MAX_TOKENS = cfg("execution.default_max_tokens", 4096)
MAX_TASK_RETRIES = cfg("execution.max_task_retries", 5)
WAVE_CHECKPOINTS = cfg("execution.wave_checkpoints", False)
CONTEXT_FORWARD_MAX_CHARS = cfg("execution.context_forward_max_chars", 2000)
VERIFICATION_ENABLED = cfg("execution.verification_enabled", False)
VERIFICATION_MODEL = cfg("execution.verification_model", "claude-haiku-4-5-20251001")
VERIFICATION_MAX_TOKENS = cfg("execution.verification_max_tokens", 1024)
CHECKPOINT_ON_RETRY_EXHAUSTED = cfg("execution.checkpoint_on_retry_exhausted", True)
SHUTDOWN_GRACE_SECONDS = cfg("execution.shutdown_grace_seconds", 30)
RESOURCE_SKIP_SECONDS = cfg("execution.resource_skip_seconds", 30)
KNOWLEDGE_EXTRACTION_ENABLED = cfg("execution.knowledge_extraction_enabled", True)
KNOWLEDGE_EXTRACTION_MODEL = cfg("execution.knowledge_extraction_model", "claude-haiku-4-5-20251001")
KNOWLEDGE_EXTRACTION_MAX_TOKENS = cfg("execution.knowledge_extraction_max_tokens", 1024)
KNOWLEDGE_INJECTION_MAX_CHARS = cfg("execution.knowledge_injection_max_chars", 3000)
KNOWLEDGE_MIN_OUTPUT_LENGTH = cfg("execution.knowledge_min_output_length", 200)
EXTERNAL_CLAIM_TIMEOUT_SECONDS = cfg("execution.external_claim_timeout_seconds", 3600)

# Model pricing
MODEL_PRICING = cfg("model_pricing", {})

# Resource check
RESOURCE_CHECK_INTERVAL = cfg("resource_check_interval_sec", 30)

# Auth
AUTH_SECRET_KEY = cfg("auth.secret_key", "")
AUTH_ALGORITHM = cfg("auth.algorithm", "HS256")
AUTH_ACCESS_TOKEN_EXPIRE_MINUTES = cfg("auth.access_token_expire_minutes", 30)
AUTH_REFRESH_TOKEN_EXPIRE_DAYS = cfg("auth.refresh_token_expire_days", 7)
AUTH_ALLOW_REGISTRATION = cfg("auth.allow_registration", True)
AUTH_SSE_TOKEN_EXPIRE_SECONDS = cfg("auth.sse_token_expire_seconds", 60)
AUTH_LOGIN_LOCKOUT_THRESHOLD = cfg("auth.login_lockout_threshold", 5)
AUTH_LOGIN_LOCKOUT_WINDOW_SEC = cfg("auth.login_lockout_window_seconds", 300)
AUTH_LOGIN_MAX_TRACKED = 10_000
AUTH_OIDC_PROVIDERS: list[dict] = cfg("auth.oidc_providers", [])
AUTH_OIDC_REDIRECT_URIS: list[str] = cfg("auth.oidc_redirect_uris", [])

# Git integration
GIT_ENABLED = cfg("git.enabled", True)
GIT_COMMIT_AUTHOR = cfg("git.commit_author", "Orchestration Engine <orchestration@local>")
GIT_BRANCH_PREFIX = cfg("git.branch_prefix", "orch")
GIT_NON_CODE_OUTPUT_PATH = cfg("git.non_code_output_path", ".orchestration")
GIT_AUTO_PR = cfg("git.auto_pr", True)
GIT_PR_REMOTE = cfg("git.pr_remote", "origin")
GIT_COMMAND_TIMEOUT = cfg("git.command_timeout", 30)


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def validate_config():
    """Validate critical config values. Call during app startup (not at import time).

    Raises ConfigError for fatal issues, logs warnings for non-fatal ones.
    """
    import logging
    _logger = logging.getLogger("orchestration.config")

    # Fatal: JWT secret must be non-empty and at least 32 characters
    if not AUTH_SECRET_KEY or len(AUTH_SECRET_KEY) < 32:
        raise ConfigError(
            "FATAL: auth.secret_key is missing or too short in config.json "
            "(must be at least 32 characters)"
        )

    # Fatal: reject known placeholder secret keys
    _KNOWN_PLACEHOLDERS = {
        "CHANGE-ME-generate-a-random-64-char-string-here",
        "change-me",
        "secret",
        "your-secret-key-here",
    }
    if AUTH_SECRET_KEY in _KNOWN_PLACEHOLDERS:
        raise ConfigError(
            "FATAL: auth.secret_key is still a placeholder value. "
            "Generate a unique random key (e.g. `python -c \"import secrets; print(secrets.token_hex(32))\"`)"
        )

    # Fatal: port must be valid
    if not isinstance(PORT, int) or not (1 <= PORT <= 65535):
        raise ConfigError(f"server.port must be 1-65535, got {PORT}")

    # Fatal: budget limits must be non-negative
    for label, val in [("budget.daily_limit_usd", BUDGET_DAILY),
                       ("budget.monthly_limit_usd", BUDGET_MONTHLY),
                       ("budget.per_project_limit_usd", BUDGET_PER_PROJECT)]:
        if not isinstance(val, (int, float)) or val < 0:
            raise ConfigError(f"{label} must be >= 0, got {val}")

    # Fatal: timeouts must be positive
    for label, val in [("anthropic.timeout", API_TIMEOUT),
                       ("ollama.generate_timeout", OLLAMA_GENERATE_TIMEOUT)]:
        if not isinstance(val, (int, float)) or val <= 0:
            raise ConfigError(f"{label} must be > 0, got {val}")

    # Fatal: OIDC providers must have all required fields if configured
    for i, prov in enumerate(AUTH_OIDC_PROVIDERS):
        name = prov.get("name", f"<index {i}>")
        if not prov.get("name"):
            raise ConfigError(f"OIDC provider at index {i} is missing required 'name' field")
        if not prov.get("issuer"):
            raise ConfigError(f"OIDC provider '{name}' is missing required 'issuer' field")
        if not prov.get("client_id"):
            raise ConfigError(f"OIDC provider '{name}' is missing required 'client_id' field")
        if not prov.get("client_secret"):
            raise ConfigError(f"OIDC provider '{name}' is missing required 'client_secret' field")

    # Fatal: lockout config must be valid
    if not isinstance(AUTH_LOGIN_LOCKOUT_THRESHOLD, int) or AUTH_LOGIN_LOCKOUT_THRESHOLD < 1:
        raise ConfigError(
            f"auth.login_lockout_threshold must be >= 1, got {AUTH_LOGIN_LOCKOUT_THRESHOLD}"
        )
    if not isinstance(AUTH_LOGIN_LOCKOUT_WINDOW_SEC, (int, float)) or AUTH_LOGIN_LOCKOUT_WINDOW_SEC <= 0:
        raise ConfigError(
            f"auth.login_lockout_window_seconds must be > 0, got {AUTH_LOGIN_LOCKOUT_WINDOW_SEC}"
        )

    # Warning: OIDC redirect URIs not configured when providers exist
    if AUTH_OIDC_PROVIDERS and not AUTH_OIDC_REDIRECT_URIS:
        _logger.warning(
            "OIDC providers configured but auth.oidc_redirect_uris is empty — "
            "redirect URI validation is disabled"
        )

    # Fatal: CORS origins must be valid URLs
    for origin in CORS_ORIGINS:
        if not isinstance(origin, str):
            raise ConfigError(f"CORS origin must be a string, got {type(origin).__name__}")
        if origin == "*":
            _logger.warning("CORS origin '*' allows all origins — not recommended for production")
        elif not origin.startswith(("http://", "https://")):
            raise ConfigError(
                f"CORS origin must start with http:// or https://, got '{origin}'"
            )

    # Fatal: git command timeout must be positive
    if GIT_ENABLED and (not isinstance(GIT_COMMAND_TIMEOUT, (int, float)) or GIT_COMMAND_TIMEOUT <= 0):
        raise ConfigError(f"git.command_timeout must be > 0, got {GIT_COMMAND_TIMEOUT}")

    # Warning: git binary not found
    if GIT_ENABLED:
        import shutil
        if not shutil.which("git"):
            _logger.warning(
                "git.enabled is true but 'git' binary not found on PATH — "
                "git operations will fail"
            )

    # Warning: Anthropic API key not set (Ollama-only usage is valid)
    if not ANTHROPIC_API_KEY:
        _logger.warning(
            "ANTHROPIC_API_KEY is not set. Claude API calls will fail. "
            "Set the env var or use Ollama-only mode."
        )

    # Warning: configured models without pricing entries
    pricing = cfg("model_pricing", {})
    models_to_check = []
    planning_model = cfg("anthropic.planning_model")
    if planning_model:
        models_to_check.append(("anthropic.planning_model", planning_model))
    for tier, model_id in (cfg("anthropic.models", {}) or {}).items():
        models_to_check.append((f"anthropic.models.{tier}", model_id))
    for label, model_id in models_to_check:
        if model_id not in pricing:
            _logger.warning(
                "Model '%s' (%s) has no entry in model_pricing — "
                "costs will be recorded as $0.00",
                model_id, label,
            )


class ConfigError(Exception):
    """Raised when critical configuration is invalid."""
