"""Add ON DELETE SET NULL to projects.owner_id foreign key.

Revision ID: 003
Revises: 002
Create Date: 2026-02-26
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite doesn't support ALTER CONSTRAINT — batch mode recreates the table.
    # Migration 002 added owner_id without a FK, so we only need to create
    # the constraint (no drop needed).  If an older DB already has an unnamed
    # FK, batch mode silently replaces it during table recreation.
    with op.batch_alter_table("projects") as batch_op:
        batch_op.create_foreign_key(
            "fk_projects_owner_id",
            "users",
            ["owner_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint("fk_projects_owner_id", type_="foreignkey")
        batch_op.create_foreign_key(
            None,
            "users",
            ["owner_id"],
            ["id"],
        )
