"""Create daily_metrics table.

Stores aggregated daily metrics for analytics and reporting.
Each row is a single metric value for a given date (e.g. tasks_completed,
total_cost, avg_duration). details_json holds optional structured breakdown.

Revision ID: 031
Revises: 030
Create Date: 2026-04-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    is_postgres = conn.dialect.name == "postgresql"

    if "daily_metrics" not in inspector.get_table_names():
        if is_postgres:
            details_type = postgresql.JSONB(astext_type=sa.Text())
        else:
            details_type = sa.Text()

        op.create_table(
            "daily_metrics",
            sa.Column(
                "id",
                sa.BigInteger if is_postgres else sa.Integer,
                primary_key=True,
                autoincrement=True,
            ),
            sa.Column("date", sa.Text(), nullable=False),
            sa.Column("metric_name", sa.Text(), nullable=False),
            sa.Column("metric_value", sa.Float(), nullable=True),
            sa.Column("details_json", details_type, nullable=True),
            sa.Column("extracted_at", sa.Float(), nullable=True),
        )

        op.create_index(
            "ix_daily_metrics_date_metric",
            "daily_metrics",
            ["date", "metric_name"],
        )


def downgrade() -> None:
    op.drop_index("ix_daily_metrics_date_metric", table_name="daily_metrics")
    op.drop_table("daily_metrics")
