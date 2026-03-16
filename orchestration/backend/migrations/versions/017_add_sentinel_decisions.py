"""Add sentinel_decisions table for orchestrator command logging.

Revision ID: 017
Revises: 016
Create Date: 2026-03-15
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sentinel_decisions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "project_id", sa.Text,
            sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("timestamp", sa.Float, nullable=False),
        sa.Column("command", sa.Text, nullable=False),
        sa.Column("reasoning", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("outcome", sa.Text, nullable=True),
        sa.Column("details_json", sa.Text, nullable=True),
    )
    op.create_index("idx_sentinel_dec_project", "sentinel_decisions", ["project_id"])
    op.create_index("idx_sentinel_dec_timestamp", "sentinel_decisions", ["timestamp"])


def downgrade() -> None:
    op.drop_index("idx_sentinel_dec_timestamp")
    op.drop_index("idx_sentinel_dec_project")
    op.drop_table("sentinel_decisions")
