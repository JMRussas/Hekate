"""Add node_mapping_json to plans table

Revision ID: 019
Revises: 018
Create Date: 2026-03-17 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '019'
down_revision = '018'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('plans', sa.Column('node_mapping_json', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('plans', 'node_mapping_json')
