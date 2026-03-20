"""Add step_tree_json column to tasks table.

Nullable TEXT/JSONB column that holds the deterministic execution tree
for a task. When present, the tree runner walks the tree instead of
dispatching a single-shot model call.

Tasks without a step tree execute exactly as before — fully backward
compatible.

Revision ID: 026
Revises: 025
Create Date: 2026-03-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    is_postgres = conn.dialect.name == 'postgresql'

    # Check if column already exists (idempotent)
    inspector = sa.inspect(conn)
    existing = [c["name"] for c in inspector.get_columns("tasks")]
    if "step_tree_json" in existing:
        return

    if is_postgres:
        col_type = postgresql.JSONB(astext_type=sa.Text())
    else:
        col_type = sa.Text()

    op.add_column("tasks", sa.Column("step_tree_json", col_type, nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "step_tree_json")
