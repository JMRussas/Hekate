#  Orchestration Engine - Migration 011: Add Git Columns
#
#  Adds git integration columns to projects and tasks tables.
#
#  Depends on: 010 (add_usage_task_index)
#  Used by:    Alembic migration chain

"""Add git integration columns to projects and tasks

Revision ID: 011
Revises: 010
Create Date: 2026-02-28
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Projects: git integration columns
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("repo_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("git_base_branch", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("git_project_branch", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("git_worktree_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("git_state_json", sa.Text(), server_default="{}", nullable=True))

    # Tasks: git branch and commit tracking
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("git_branch", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("git_commit_sha", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("git_commit_sha")
        batch_op.drop_column("git_branch")

    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("git_state_json")
        batch_op.drop_column("git_worktree_path")
        batch_op.drop_column("git_project_branch")
        batch_op.drop_column("git_base_branch")
        batch_op.drop_column("repo_path")
