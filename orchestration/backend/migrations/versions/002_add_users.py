"""Add users table and owner_id to projects.

Revision ID: 002
Revises: 001
Create Date: 2026-02-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("display_name", sa.Text, nullable=False, server_default=""),
        sa.Column("role", sa.Text, nullable=False, server_default="user"),
        sa.Column("is_active", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("last_login_at", sa.Float),
    )

    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.Text, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("owner_id")

    op.drop_table("users")
