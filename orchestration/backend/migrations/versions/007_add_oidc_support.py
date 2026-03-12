"""Add OIDC support: user_identities table + nullable password_hash.

Revision ID: 007
Revises: 006
Create Date: 2026-02-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_identities",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column(
            "user_id",
            sa.Text,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("provider_user_id", sa.Text, nullable=False),
        sa.Column("provider_email", sa.Text),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("idx_identities_user", "user_identities", ["user_id"])
    op.create_index(
        "idx_identities_provider_uid",
        "user_identities",
        ["provider", "provider_user_id"],
        unique=True,
    )

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "password_hash",
            existing_type=sa.Text,
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "password_hash",
            existing_type=sa.Text,
            nullable=False,
        )
    op.drop_index("idx_identities_provider_uid")
    op.drop_index("idx_identities_user")
    op.drop_table("user_identities")
