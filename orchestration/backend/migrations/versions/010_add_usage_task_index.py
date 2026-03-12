"""Add index on usage_log(task_id) for analytics JOIN performance.

Revision ID: 010
Revises: 009
Create Date: 2026-02-28
"""
from typing import Sequence, Union

from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("idx_usage_task", "usage_log", ["task_id"])


def downgrade() -> None:
    op.drop_index("idx_usage_task")
