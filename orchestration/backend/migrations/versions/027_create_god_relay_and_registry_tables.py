"""Create god_relay_events and god_registry tables.

god_relay_events: Cross-god event bus. Gods write events here and poll
for events matching their subscriptions. Replaces the in-process
SentinelBus with a durable, cross-process event store.

god_registry: Live god status tracking. Gods write their heartbeat
timestamp here so other gods and the dashboard can see who's alive.

Revision ID: 027
Revises: 026
Create Date: 2026-03-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    is_postgres = conn.dialect.name == "postgresql"

    # ---------------------------------------------------------------
    # god_relay_events — inter-god event bus
    # ---------------------------------------------------------------
    if "god_relay_events" not in inspector.get_table_names():
        if is_postgres:
            payload_type = postgresql.JSONB(astext_type=sa.Text())
        else:
            payload_type = sa.Text()

        op.create_table(
            "god_relay_events",
            sa.Column(
                "id",
                sa.BigInteger if is_postgres else sa.Integer,
                primary_key=True,
                autoincrement=True,
            ),
            sa.Column("event_type", sa.Text, nullable=False),
            sa.Column("source", sa.Text, nullable=False),
            sa.Column("payload", payload_type, nullable=True),
            sa.Column(
                "severity", sa.Text, nullable=False, server_default="info"
            ),
            sa.Column("created_at", sa.Float, nullable=False),
        )

        op.create_index(
            "idx_relay_type_created",
            "god_relay_events",
            ["event_type", "created_at"],
        )
        op.create_index(
            "idx_relay_created",
            "god_relay_events",
            ["created_at"],
        )

        # Postgres-only: pruning function (retain 24h by default)
        if is_postgres:
            op.execute(sa.text("""
                CREATE OR REPLACE FUNCTION prune_old_relay_events(
                    retention_seconds DOUBLE PRECISION DEFAULT 86400.0
                )
                RETURNS INTEGER
                LANGUAGE plpgsql
                AS $$
                DECLARE
                    deleted_count INTEGER;
                    cutoff DOUBLE PRECISION;
                BEGIN
                    cutoff := EXTRACT(EPOCH FROM now()) - retention_seconds;
                    DELETE FROM god_relay_events
                    WHERE  created_at < cutoff;
                    GET DIAGNOSTICS deleted_count = ROW_COUNT;
                    RETURN deleted_count;
                END;
                $$;
            """))

    # ---------------------------------------------------------------
    # god_registry — live god status
    # ---------------------------------------------------------------
    if "god_registry" not in inspector.get_table_names():
        if is_postgres:
            config_type = postgresql.JSONB(astext_type=sa.Text())
        else:
            config_type = sa.Text()

        op.create_table(
            "god_registry",
            sa.Column("name", sa.Text, primary_key=True),
            sa.Column("port", sa.Integer, nullable=True),
            sa.Column(
                "status",
                sa.Text,
                nullable=False,
                server_default="unknown",
            ),
            sa.Column("last_heartbeat", sa.Float, nullable=True),
            sa.Column("config", config_type, nullable=True),
            sa.Column("updated_at", sa.Float, nullable=True),
        )


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute(sa.text(
            "DROP FUNCTION IF EXISTS prune_old_relay_events(DOUBLE PRECISION)"
        ))

    op.drop_index("idx_relay_created", table_name="god_relay_events")
    op.drop_index("idx_relay_type_created", table_name="god_relay_events")
    op.drop_table("god_relay_events")
    op.drop_table("god_registry")
