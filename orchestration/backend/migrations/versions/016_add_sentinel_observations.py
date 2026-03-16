"""Add sentinel_observations table for durable monitoring state.

Revision ID: 016
Revises: 015
Create Date: 2026-03-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sentinel_observations",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "project_id", sa.Text,
            sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("task_id", sa.Text, nullable=True),
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("details_json", sa.Text, nullable=True),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("idx_sentinel_obs_project", "sentinel_observations", ["project_id"])
    op.create_index("idx_sentinel_obs_severity", "sentinel_observations", ["severity"])
    op.create_index("idx_sentinel_obs_category", "sentinel_observations", ["category"])
    op.create_index("idx_sentinel_obs_created", "sentinel_observations", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_sentinel_obs_created")
    op.drop_index("idx_sentinel_obs_category")
    op.drop_index("idx_sentinel_obs_severity")
    op.drop_index("idx_sentinel_obs_project")
    op.drop_table("sentinel_observations")
