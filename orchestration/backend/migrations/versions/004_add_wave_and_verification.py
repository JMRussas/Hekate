"""Add wave and verification columns to tasks table.

Revision ID: 004
Revises: 003
Create Date: 2026-02-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("wave", sa.Integer, nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("verification_status", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("verification_notes", sa.Text, nullable=True))

    op.create_index("idx_tasks_wave", "tasks", ["wave"])


def downgrade() -> None:
    op.drop_index("idx_tasks_wave")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("verification_notes")
        batch_op.drop_column("verification_status")
        batch_op.drop_column("wave")
