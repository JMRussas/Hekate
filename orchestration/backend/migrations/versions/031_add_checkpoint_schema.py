"""Add schema_json column and checkpoint_type index to checkpoints.

schema_json (TEXT, nullable, default NULL) holds a JSON schema definition
for structured checkpoint responses.  NULL means free-text (backward compatible).
Index on checkpoint_type speeds up filtered queries.

Revision ID: 031
Revises: 030
Create Date: 2026-04-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("checkpoints", sa.Column("schema_json", sa.Text, nullable=True))
    op.create_index("idx_checkpoints_type", "checkpoints", ["checkpoint_type"])


def downgrade() -> None:
    op.drop_index("idx_checkpoints_type", table_name="checkpoints")
    op.drop_column("checkpoints", "schema_json")
