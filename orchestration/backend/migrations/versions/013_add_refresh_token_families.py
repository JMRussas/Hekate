#  Orchestration Engine - Migration 013: Add Refresh Token Families
#
#  Adds refresh_token_families table for token rotation and reuse detection.
#  Requires PR 1 (migration 012) to be merged first.
#
#  Depends on: 012 (add_project_knowledge)
#  Used by:    Alembic migration chain

"""Add refresh token families for token rotation tracking

Revision ID: 013
Revises: 012
"""

from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "refresh_token_families",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", sa.Text, nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False),
        sa.Column("is_revoked", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
    )
    op.create_index("idx_rtf_token_hash", "refresh_token_families", ["token_hash"], unique=True)
    op.create_index("idx_rtf_family_id", "refresh_token_families", ["family_id"])
    op.create_index("idx_rtf_user_id", "refresh_token_families", ["user_id"])


def downgrade():
    op.drop_index("idx_rtf_user_id")
    op.drop_index("idx_rtf_family_id")
    op.drop_index("idx_rtf_token_hash")
    op.drop_table("refresh_token_families")
