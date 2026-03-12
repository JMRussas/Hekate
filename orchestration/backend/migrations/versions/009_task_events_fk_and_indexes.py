"""Add FK constraints to task_events and composite indexes for query performance.

Revision ID: 009
Revises: 008
Create Date: 2026-02-27
"""
from typing import Sequence, Union

from alembic import op

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Clean orphaned task_events before adding FK constraints —
    # batch_alter_table copies data to a temp table WITH constraints,
    # so orphaned rows would cause INSERT failures during the copy.
    op.execute(
        "DELETE FROM task_events WHERE project_id NOT IN (SELECT id FROM projects)"
    )
    op.execute(
        "DELETE FROM task_events WHERE task_id IS NOT NULL "
        "AND task_id NOT IN (SELECT id FROM tasks)"
    )

    # Add FK constraints to task_events (batch mode required for SQLite)
    with op.batch_alter_table("task_events") as batch_op:
        batch_op.create_foreign_key(
            "fk_events_project", "projects",
            ["project_id"], ["id"], ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_events_task", "tasks",
            ["task_id"], ["id"], ondelete="SET NULL",
        )

    # Composite indexes for common query patterns
    op.create_index("idx_tasks_project_status", "tasks", ["project_id", "status"])
    op.create_index("idx_tasks_project_wave", "tasks", ["project_id", "wave"])
    op.create_index("idx_events_project_task", "task_events", ["project_id", "task_id"])
    op.create_index("idx_deps_task_id", "task_deps", ["task_id"])
    op.create_index("idx_usage_project_timestamp", "usage_log", ["project_id", "timestamp"])


def downgrade() -> None:
    op.drop_index("idx_usage_project_timestamp")
    op.drop_index("idx_deps_task_id")
    op.drop_index("idx_events_project_task")
    op.drop_index("idx_tasks_project_wave")
    op.drop_index("idx_tasks_project_status")

    with op.batch_alter_table("task_events") as batch_op:
        batch_op.drop_constraint("fk_events_task", type_="foreignkey")
        batch_op.drop_constraint("fk_events_project", type_="foreignkey")
