"""Rename sentinel_decisions to odin_decisions and expand schema (R9).

Renames the table and columns to reflect Odin ownership, adds new columns,
and enforces the complete R9 decision audit trail schema:

  decision_id (PK), project_id, task_id, decision_type, reasoning,
  confidence, action_taken, outcome, created_at, details_json

Changes from original sentinel_decisions (017):
  - Table renamed: sentinel_decisions -> odin_decisions
  - Column renamed: id -> decision_id (R9 naming)
  - Column renamed: command -> decision_type
  - Column renamed: timestamp -> created_at
  - Column added: task_id (nullable, for task-level decisions)
  - Column added: action_taken (nullable, concrete action executed)
  - Existing columns preserved: project_id, reasoning, confidence, outcome,
    details_json (constraints re-enforced per R9)

Revision ID: 024
Revises: 023
Create Date: 2026-03-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    # If odin_decisions already exists, skip the entire migration
    if 'odin_decisions' in tables:
        return

    # If sentinel_decisions doesn't exist either, create odin_decisions from scratch
    if 'sentinel_decisions' not in tables:
        op.create_table(
            'odin_decisions',
            sa.Column('decision_id', sa.Text(), primary_key=True),
            sa.Column('project_id', sa.Text(), nullable=False),
            sa.Column('task_id', sa.Text(), nullable=True),
            sa.Column('decision_type', sa.Text(), nullable=False),
            sa.Column('params_json', sa.Text(), nullable=True),
            sa.Column('reasoning', sa.Text(), nullable=False),
            sa.Column('confidence', sa.Float(), nullable=False),
            sa.Column('outcome', sa.Text(), nullable=True),
            sa.Column('action_taken', sa.Text(), nullable=True),
            sa.Column('details_json', sa.Text(), nullable=True),
            sa.Column('created_at', sa.Float(), nullable=False),
        )
        op.create_index('idx_odin_dec_project', 'odin_decisions', ['project_id'])
        op.create_index('idx_odin_dec_created_at', 'odin_decisions', ['created_at'])
        op.create_index('idx_odin_dec_type', 'odin_decisions', ['decision_type'])
        return

    # Drop old indexes before rename
    op.drop_index("idx_sentinel_dec_timestamp", table_name="sentinel_decisions")
    op.drop_index("idx_sentinel_dec_project", table_name="sentinel_decisions")

    # Rename table: sentinel_decisions -> odin_decisions
    op.rename_table("sentinel_decisions", "odin_decisions")

    with op.batch_alter_table("odin_decisions") as batch_op:
        # -- Renamed columns --
        # R9: id -> decision_id (R9 specifies decision_id as PK name)
        batch_op.alter_column("id", new_column_name="decision_id")
        # R9: command -> decision_type (dispatch, retry, skip, reassign_tier, etc.)
        batch_op.alter_column("command", new_column_name="decision_type")
        # R9: timestamp -> created_at
        batch_op.alter_column("timestamp", new_column_name="created_at")

        # -- New columns --
        # R9: task_id — nullable because some decisions are project-level
        batch_op.add_column(sa.Column("task_id", sa.Text, nullable=True))
        # R9: action_taken — the concrete action executed after the decision
        batch_op.add_column(sa.Column("action_taken", sa.Text, nullable=True))
        # params_json — serialized tool arguments for the decision
        batch_op.add_column(sa.Column("params_json", sa.Text, nullable=True))

        # -- Pre-existing columns: enforce R9 schema constraints --
        # R9: reasoning — LLM reasoning output, must not be null
        batch_op.alter_column(
            "reasoning",
            existing_type=sa.Text(),
            nullable=False,
        )
        # R9: confidence — float score, must not be null
        batch_op.alter_column(
            "confidence",
            existing_type=sa.Float(),
            nullable=False,
        )
        # R9: outcome — result of the action, nullable until resolved
        batch_op.alter_column(
            "outcome",
            existing_type=sa.Text(),
            nullable=True,
        )

    # New indexes for Odin queries
    op.create_index("idx_odin_dec_project", "odin_decisions", ["project_id"])
    op.create_index("idx_odin_dec_created_at", "odin_decisions", ["created_at"])
    op.create_index("idx_odin_dec_type", "odin_decisions", ["decision_type"])


def downgrade() -> None:
    op.drop_index("idx_odin_dec_type", table_name="odin_decisions")
    op.drop_index("idx_odin_dec_created_at", table_name="odin_decisions")
    op.drop_index("idx_odin_dec_project", table_name="odin_decisions")

    with op.batch_alter_table("odin_decisions") as batch_op:
        # Revert new columns
        batch_op.drop_column("action_taken")
        batch_op.drop_column("task_id")
        # Revert pre-existing column constraints
        batch_op.alter_column(
            "outcome",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch_op.alter_column(
            "confidence",
            existing_type=sa.Float(),
            nullable=False,
        )
        batch_op.alter_column(
            "reasoning",
            existing_type=sa.Text(),
            nullable=False,
        )
        # Revert renamed columns
        batch_op.alter_column("created_at", new_column_name="timestamp")
        batch_op.alter_column("decision_type", new_column_name="command")
        batch_op.alter_column("decision_id", new_column_name="id")

    op.rename_table("odin_decisions", "sentinel_decisions")

    op.create_index("idx_sentinel_dec_project", "sentinel_decisions", ["project_id"])
    op.create_index("idx_sentinel_dec_timestamp", "sentinel_decisions", ["timestamp"])
