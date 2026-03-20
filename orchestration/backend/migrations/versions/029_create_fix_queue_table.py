"""Create fix_queue table.

Unified queue for failures that need resolution — any subsystem
(learner, sentinel, odin, task lifecycle, human) can file a fix item.
Items track the problem, evidence, proposed fix, and resolution status.

Revision ID: 029
Revises: 028
Create Date: 2026-03-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fix_queue",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),            # "learner", "sentinel", "odin", "lifecycle", "human"
        sa.Column("category", sa.Text(), nullable=False),          # "model_failure", "infra_bug", "code_bug", "config", "pattern"
        sa.Column("severity", sa.Text(), nullable=False, server_default="medium"),  # "low", "medium", "high", "critical"
        sa.Column("title", sa.Text(), nullable=False),             # One-line summary
        sa.Column("description", sa.Text(), nullable=False),       # Full problem description
        sa.Column("evidence_json", sa.Text(), server_default="[]"),  # Array of {type, content} evidence blocks
        sa.Column("proposed_fix", sa.Text()),                      # Suggested resolution (nullable until diagnosed)
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),  # "open", "claimed", "in_progress", "resolved", "wont_fix"
        sa.Column("project_id", sa.Text(), sa.ForeignKey("projects.id", ondelete="SET NULL")),  # Optional project link
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.id", ondelete="SET NULL")),        # Optional task link
        sa.Column("affected_component", sa.Text()),                # "model_router", "gemini_cli", "sentinel", etc.
        sa.Column("resolution", sa.Text()),                        # What was actually done (filled on resolve)
        sa.Column("resolved_by", sa.Text()),                       # "human", "odin", "learner", "auto"
        sa.Column("claimed_by", sa.Text()),                        # Who/what is working on it
        sa.Column("dedupe_key", sa.Text()),                        # Prevents duplicate filings for same issue
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("resolved_at", sa.Float()),
    )
    op.create_index("ix_fix_queue_status", "fix_queue", ["status"])
    op.create_index("ix_fix_queue_severity", "fix_queue", ["severity"])
    op.create_index("ix_fix_queue_category", "fix_queue", ["category"])
    op.create_index("ix_fix_queue_dedupe", "fix_queue", ["dedupe_key"], unique=True)
    op.create_index("ix_fix_queue_created", "fix_queue", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_fix_queue_created", table_name="fix_queue")
    op.drop_index("ix_fix_queue_dedupe", table_name="fix_queue")
    op.drop_index("ix_fix_queue_category", table_name="fix_queue")
    op.drop_index("ix_fix_queue_severity", table_name="fix_queue")
    op.drop_index("ix_fix_queue_status", table_name="fix_queue")
    op.drop_table("fix_queue")
