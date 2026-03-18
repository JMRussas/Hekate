"""add security_findings table for Ares pre-plan security review

Revision ID: 022
Revises: 021
Create Date: 2026-03-17 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '022'
down_revision: Union[str, None] = '021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'security_findings' in inspector.get_table_names():
        return
    op.create_table(
        'security_findings',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('plan_id', sa.Text(), nullable=False),
        sa.Column('project_id', sa.Text(), nullable=False),
        sa.Column('task_index', sa.Integer(), nullable=False),
        sa.Column('task_title', sa.Text(), nullable=False),
        sa.Column('category', sa.Text(), nullable=False),
        sa.Column('severity', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('recommended_mitigation', sa.Text(), nullable=False),
        sa.Column('affected_files_json', sa.Text(), nullable=False),
        sa.Column('context_store_node_id', sa.Text(), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False),
    )
    op.create_index('idx_security_findings_plan', 'security_findings', ['plan_id'])
    op.create_index('idx_security_findings_project', 'security_findings', ['project_id'])


def downgrade() -> None:
    op.drop_index('idx_security_findings_project')
    op.drop_index('idx_security_findings_plan')
    op.drop_table('security_findings')
