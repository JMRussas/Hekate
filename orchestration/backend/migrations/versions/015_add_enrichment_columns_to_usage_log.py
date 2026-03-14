#  Orchestration Engine - Migration 015
#
#  Add context enrichment metadata columns to usage_log so that
#  enrichment passes can be analyzed alongside task execution costs.
#
#  Depends on: 014_add_api_keys_and_claim_tracking
#  Used by:    services/task_lifecycle.py (enrichment metadata logging)

"""Add context enrichment metadata columns to usage_log.

Revision ID: 015
Revises: 014
Create Date: 2026-03-14
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("usage_log") as batch_op:
        batch_op.add_column(sa.Column("context_tokens_injected", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("source_node_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("enrichment_latency_ms", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("usage_log") as batch_op:
        batch_op.drop_column("enrichment_latency_ms")
        batch_op.drop_column("source_node_count")
        batch_op.drop_column("context_tokens_injected")
