#  Orchestration Engine - SQLAlchemy Table Metadata
#
#  Declarative Table definitions for Alembic autogenerate.
#  These mirror the SQLite schema but are NOT used at runtime —
#  the app still uses raw SQL via aiosqlite.
#
#  Depends on: (none)
#  Used by:    migrations/env.py (Alembic autogenerate)

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
)

metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", Text, primary_key=True),
    Column("email", Text, nullable=False, unique=True),
    Column("password_hash", Text, nullable=True),
    Column("display_name", Text, nullable=False, server_default=""),
    Column("role", Text, nullable=False, server_default="user"),
    Column("is_active", Integer, nullable=False, server_default="1"),
    Column("created_at", Float, nullable=False),
    Column("last_login_at", Float),
)

projects = Table(
    "projects",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("requirements", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="draft"),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("completed_at", Float),
    Column("config_json", Text, server_default="{}"),
    Column("owner_id", Text, ForeignKey("users.id", ondelete="SET NULL")),
    Column("repo_path", Text),
    Column("git_base_branch", Text),
    Column("git_project_branch", Text),
    Column("git_worktree_path", Text),
    Column("git_state_json", Text, server_default="{}"),
)

plans = Table(
    "plans",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("model_used", Text, nullable=False),
    Column("prompt_tokens", Integer, nullable=False, server_default="0"),
    Column("completion_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=False, server_default="0.0"),
    Column("plan_json", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="draft"),
    Column("created_at", Float, nullable=False),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("plan_id", Text, ForeignKey("plans.id", ondelete="CASCADE"), nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("task_type", Text, nullable=False),
    Column("priority", Integer, nullable=False, server_default="50"),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("model_tier", Text, nullable=False, server_default="haiku"),
    Column("model_used", Text),
    Column("context_json", Text, server_default="[]"),
    Column("tools_json", Text, server_default="[]"),
    Column("system_prompt", Text, server_default=""),
    Column("output_text", Text),
    Column("output_artifacts_json", Text, server_default="[]"),
    Column("prompt_tokens", Integer, nullable=False, server_default="0"),
    Column("completion_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=False, server_default="0.0"),
    Column("max_tokens", Integer, nullable=False, server_default="4096"),
    Column("retry_count", Integer, nullable=False, server_default="0"),
    Column("max_retries", Integer, nullable=False, server_default="2"),
    Column("wave", Integer, nullable=False, server_default="0"),
    Column("phase", Text),
    Column("verification_status", Text),
    Column("verification_notes", Text),
    Column("requirement_ids_json", Text, server_default="[]"),
    Column("error", Text),
    Column("started_at", Float),
    Column("completed_at", Float),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("git_branch", Text),
    Column("git_commit_sha", Text),
    Column("claimed_by", Text),
    Column("claimed_at", Float),
)

task_deps = Table(
    "task_deps",
    metadata,
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, primary_key=True),
    Column("depends_on", Text, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, primary_key=True),
)

usage_log = Table(
    "usage_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Text, ForeignKey("projects.id")),
    Column("task_id", Text, ForeignKey("tasks.id")),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_tokens", Integer, nullable=False),
    Column("completion_tokens", Integer, nullable=False),
    Column("cost_usd", Float, nullable=False),
    Column("purpose", Text, nullable=False, server_default=""),
    Column("timestamp", Float, nullable=False),
)

budget_periods = Table(
    "budget_periods",
    metadata,
    Column("period_key", Text, primary_key=True),
    Column("period_type", Text, nullable=False),
    Column("total_cost_usd", Float, nullable=False, server_default="0.0"),
    Column("total_prompt_tokens", Integer, nullable=False, server_default="0"),
    Column("total_completion_tokens", Integer, nullable=False, server_default="0"),
    Column("api_call_count", Integer, nullable=False, server_default="0"),
)

task_events = Table(
    "task_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="SET NULL")),
    Column("event_type", Text, nullable=False),
    Column("message", Text),
    Column("data_json", Text),
    Column("timestamp", Float, nullable=False),
)

checkpoints = Table(
    "checkpoints",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="CASCADE")),
    Column("checkpoint_type", Text, nullable=False),
    Column("summary", Text, nullable=False),
    Column("attempts_json", Text, server_default="[]"),
    Column("question", Text, nullable=False),
    Column("response", Text),
    Column("resolved_at", Float),
    Column("created_at", Float, nullable=False),
)

refresh_token_families = Table(
    "refresh_token_families",
    metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("family_id", Text, nullable=False),
    Column("token_hash", Text, nullable=False),
    Column("is_revoked", Integer, server_default="0"),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
)

user_identities = Table(
    "user_identities",
    metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("provider", Text, nullable=False),
    Column("provider_user_id", Text, nullable=False),
    Column("provider_email", Text),
    Column("created_at", Float, nullable=False),
)

project_knowledge = Table(
    "project_knowledge",
    metadata,
    Column("id", Text, primary_key=True),
    Column("project_id", Text, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("task_id", Text, ForeignKey("tasks.id", ondelete="SET NULL")),
    Column("category", Text, nullable=False, server_default="discovery"),
    Column("content", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("source_task_title", Text),
    Column("created_at", Float, nullable=False),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", Text, primary_key=True),
    Column("key_hash", Text, nullable=False, unique=True),
    Column("key_prefix", Text, nullable=False),
    Column("user_id", Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("name", Text, nullable=False),
    Column("is_active", Integer, nullable=False, server_default="1"),
    Column("created_at", Float, nullable=False),
    Column("last_used_at", Float),
)

# Indexes
Index("idx_api_keys_hash", api_keys.c.key_hash)
Index("idx_api_keys_user", api_keys.c.user_id)
Index("idx_identities_user", user_identities.c.user_id)
Index("idx_identities_provider_uid", user_identities.c.provider, user_identities.c.provider_user_id, unique=True)
Index("idx_checkpoints_project", checkpoints.c.project_id)
Index("idx_plans_project", plans.c.project_id)
Index("idx_tasks_project", tasks.c.project_id)
Index("idx_tasks_status", tasks.c.status)
Index("idx_tasks_priority", tasks.c.priority)
Index("idx_tasks_wave", tasks.c.wave)
Index("idx_deps_depends", task_deps.c.depends_on)
Index("idx_usage_project", usage_log.c.project_id)
Index("idx_usage_task", usage_log.c.task_id)
Index("idx_usage_timestamp", usage_log.c.timestamp)
Index("idx_budget_type", budget_periods.c.period_type)
Index("idx_events_project", task_events.c.project_id)
Index("idx_events_task", task_events.c.task_id)

# Composite indexes for common query patterns
Index("idx_tasks_project_status", tasks.c.project_id, tasks.c.status)
Index("idx_tasks_project_wave", tasks.c.project_id, tasks.c.wave)
Index("idx_events_project_task", task_events.c.project_id, task_events.c.task_id)
Index("idx_deps_task_id", task_deps.c.task_id)
Index("idx_usage_project_timestamp", usage_log.c.project_id, usage_log.c.timestamp)
Index("idx_rtf_token_hash", refresh_token_families.c.token_hash, unique=True)
Index("idx_rtf_family_id", refresh_token_families.c.family_id)
Index("idx_rtf_user_id", refresh_token_families.c.user_id)
Index("idx_knowledge_project", project_knowledge.c.project_id)
Index("idx_knowledge_dedup", project_knowledge.c.project_id, project_knowledge.c.content_hash, unique=True)
