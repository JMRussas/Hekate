#  Orchestration Engine - Migration 014
#
#  Add API keys table for MCP/external executor authentication,
#  and claim tracking columns on tasks for external task claiming.
#
#  Depends on: 013_add_refresh_token_families
#  Used by:    services/auth.py, routes/external.py

"""Add API keys table and claim tracking columns on tasks.

Revision ID: 014
Revises: 013
Create Date: 2026-03-04
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # API keys table for long-lived MCP/external executor authentication
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("key_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("key_prefix", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_used_at", sa.Float()),
    )
    op.create_index("idx_api_keys_hash", "api_keys", ["key_hash"])
    op.create_index("idx_api_keys_user", "api_keys", ["user_id"])

    # Claim tracking on tasks for external executors
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("claimed_by", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("claimed_at", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("claimed_by")
    op.drop_index("idx_api_keys_user", table_name="api_keys")
    op.drop_index("idx_api_keys_hash", table_name="api_keys")
    op.drop_table("api_keys")
