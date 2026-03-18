"""add plan_comments table for human feedback on plans

Revision ID: 023
Revises: 022
Create Date: 2026-03-17 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '023'
down_revision: Union[str, None] = '022'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'plan_comments' in inspector.get_table_names():
        return
    op.create_table(
        'plan_comments',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('plan_id', sa.Text(), nullable=False),
        sa.Column('project_id', sa.Text(), nullable=False),
        sa.Column('author', sa.Text(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
    )
    op.create_index('idx_plan_comments_plan', 'plan_comments', ['plan_id'])
    op.create_index('idx_plan_comments_project', 'plan_comments', ['project_id'])


def downgrade() -> None:
    op.drop_index('idx_plan_comments_project')
    op.drop_index('idx_plan_comments_plan')
    op.drop_table('plan_comments')
