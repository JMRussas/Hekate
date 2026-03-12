"""Initial schema — all 7 tables and 10 indexes.

Revision ID: 001
Revises: None
Create Date: 2026-02-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # If tables already exist (pre-Alembic database), skip creation.
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='projects'")
    )
    if result.fetchone() is not None:
        return  # Schema already applied before Alembic was introduced

    op.create_table(
        "projects",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("requirements", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="draft"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("completed_at", sa.Float),
        sa.Column("config_json", sa.Text, server_default="{}"),
    )

    op.create_table(
        "plans",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("model_used", sa.Text, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("plan_json", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="draft"),
        sa.Column("created_at", sa.Float, nullable=False),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", sa.Text, sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("task_type", sa.Text, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False, server_default="50"),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("model_tier", sa.Text, nullable=False, server_default="haiku"),
        sa.Column("model_used", sa.Text),
        sa.Column("context_json", sa.Text, server_default="[]"),
        sa.Column("tools_json", sa.Text, server_default="[]"),
        sa.Column("system_prompt", sa.Text, server_default=""),
        sa.Column("output_text", sa.Text),
        sa.Column("output_artifacts_json", sa.Text, server_default="[]"),
        sa.Column("prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("max_tokens", sa.Integer, nullable=False, server_default="4096"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="2"),
        sa.Column("error", sa.Text),
        sa.Column("started_at", sa.Float),
        sa.Column("completed_at", sa.Float),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )

    op.create_table(
        "task_deps",
        sa.Column("task_id", sa.Text, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("depends_on", sa.Text, sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.PrimaryKeyConstraint("task_id", "depends_on"),
    )

    op.create_table(
        "usage_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id")),
        sa.Column("task_id", sa.Text, sa.ForeignKey("tasks.id")),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False),
        sa.Column("completion_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Float, nullable=False),
        sa.Column("purpose", sa.Text, nullable=False, server_default=""),
        sa.Column("timestamp", sa.Float, nullable=False),
    )

    op.create_table(
        "budget_periods",
        sa.Column("period_key", sa.Text, primary_key=True),
        sa.Column("period_type", sa.Text, nullable=False),
        sa.Column("total_cost_usd", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("total_prompt_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_completion_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("api_call_count", sa.Integer, nullable=False, server_default="0"),
    )

    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Text, nullable=False),
        sa.Column("task_id", sa.Text),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("message", sa.Text),
        sa.Column("data_json", sa.Text),
        sa.Column("timestamp", sa.Float, nullable=False),
    )

    # Indexes
    op.create_index("idx_plans_project", "plans", ["project_id"])
    op.create_index("idx_tasks_project", "tasks", ["project_id"])
    op.create_index("idx_tasks_status", "tasks", ["status"])
    op.create_index("idx_tasks_priority", "tasks", ["priority"])
    op.create_index("idx_deps_depends", "task_deps", ["depends_on"])
    op.create_index("idx_usage_project", "usage_log", ["project_id"])
    op.create_index("idx_usage_timestamp", "usage_log", ["timestamp"])
    op.create_index("idx_budget_type", "budget_periods", ["period_type"])
    op.create_index("idx_events_project", "task_events", ["project_id"])
    op.create_index("idx_events_task", "task_events", ["task_id"])


def downgrade() -> None:
    op.drop_table("task_events")
    op.drop_table("budget_periods")
    op.drop_table("usage_log")
    op.drop_table("task_deps")
    op.drop_table("tasks")
    op.drop_table("plans")
    op.drop_table("projects")
