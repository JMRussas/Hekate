"""Add checkpoints table for human-in-the-loop escalation.

Revision ID: 005
Revises: 004
Create Date: 2026-02-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("project_id", sa.Text, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Text, sa.ForeignKey("tasks.id", ondelete="CASCADE")),
        sa.Column("checkpoint_type", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("attempts_json", sa.Text, server_default="[]"),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("response", sa.Text),
        sa.Column("resolved_at", sa.Float),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("idx_checkpoints_project", "checkpoints", ["project_id"])


def downgrade() -> None:
    op.drop_index("idx_checkpoints_project")
    op.drop_table("checkpoints")
