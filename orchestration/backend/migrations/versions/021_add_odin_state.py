"""add odin_state table for persistent world model

Revision ID: 021
Revises: 020
Create Date: 2026-03-17 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '021'
down_revision: Union[str, None] = '020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'odin_state',
        sa.Column('id', sa.Text(), primary_key=True, server_default='singleton'),
        sa.Column('world_model_json', sa.Text(), nullable=False),
        sa.Column('last_tick_at', sa.Float(), nullable=False),
        sa.Column('decisions_count', sa.Integer(), server_default='0'),
        sa.Column('updated_at', sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('odin_state')
