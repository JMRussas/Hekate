"""Add project_knowledge table for persisting task findings.

Revision ID: 012
Revises: 011
Create Date: 2026-03-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_knowledge",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "project_id", sa.Text,
            sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "task_id", sa.Text,
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
        ),
        sa.Column("category", sa.Text, nullable=False, server_default="discovery"),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("source_task_title", sa.Text),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("idx_knowledge_project", "project_knowledge", ["project_id"])
    op.create_index(
        "idx_knowledge_dedup", "project_knowledge",
        ["project_id", "content_hash"], unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_knowledge_dedup")
    op.drop_index("idx_knowledge_project")
    op.drop_table("project_knowledge")
