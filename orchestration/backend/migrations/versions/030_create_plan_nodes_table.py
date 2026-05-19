"""Create plan_nodes table.

Stores the parallel plan node tree for the new Athena architecture.
Each node has a dotted index path (e.g. "1.2.3") indicating its position
in the plan tree. Level equals number of dots + 1 (L0 = epics, L1 = tasks, etc).

Revision ID: 030
Revises: 029
Create Date: 2026-03-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "plan_nodes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("plan_id", sa.Text(), sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=True),
        sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("index_path", sa.Text(), nullable=False),       # "1", "1.2", "1.2.3" etc.
        sa.Column("level", sa.Integer(), nullable=False),         # 0=epic, 1=task stub, 2=spec, 3=detail, 4=exact, 5=executable
        sa.Column("status", sa.Text(), nullable=False, server_default="stub"),  # stub|planning|gap_check|complete|failed
        sa.Column("title", sa.Text()),
        sa.Column("content_json", sa.Text(), server_default="{}"),  # level-specific structured content
        sa.Column("project_context", sa.Text()),                  # full project requirements, propagated to all children
        sa.Column("parent_index", sa.Text()),                     # "1.2" for node "1.2.3", NULL for root L0 nodes
        sa.Column("conversation_id", sa.Text()),                  # gateway conversation thread ID (for audit/debug)
        sa.Column("error", sa.Text()),                            # failure reason if status=failed
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    # Primary lookup: project + exact path (unique)
    op.create_index(
        "ix_plan_nodes_project_index",
        "plan_nodes",
        ["project_id", "index_path"],
        unique=True,
    )
    # Sibling queries and completion checks: all children of a given parent
    op.create_index(
        "ix_plan_nodes_parent",
        "plan_nodes",
        ["project_id", "parent_index", "status"],
    )
    # Status sweep: find all stub/planning nodes for a project
    op.create_index(
        "ix_plan_nodes_status",
        "plan_nodes",
        ["project_id", "status"],
    )
    # Level sweep: find all nodes at a given level for a project
    op.create_index(
        "ix_plan_nodes_level",
        "plan_nodes",
        ["project_id", "level", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_plan_nodes_level", table_name="plan_nodes")
    op.drop_index("ix_plan_nodes_status", table_name="plan_nodes")
    op.drop_index("ix_plan_nodes_parent", table_name="plan_nodes")
    op.drop_index("ix_plan_nodes_project_index", table_name="plan_nodes")
    op.drop_table("plan_nodes")

