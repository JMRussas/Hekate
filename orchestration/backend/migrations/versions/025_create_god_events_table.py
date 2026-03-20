"""Create god_events table for cross-god event system (Postgres).

All gods write events (heartbeats, tool calls, observations, decisions)
to this shared Postgres table. Odin polls it each tick to correlate state.

Schema:
  id (BIGSERIAL PK), god_name, event_type, payload (JSONB), severity,
  created_at (TIMESTAMPTZ, server-default now())

Indexes:
  (god_name, created_at) — primary Odin polling path (NFR5)
  (created_at)           — pruning deletes and time-range scans
  (event_type)           — filter by heartbeat/tool_call/observation/etc.

Pruning (R8):
  A stored procedure `prune_old_god_events(retention INTERVAL)` deletes
  rows older than the given retention (default 7 days).  Designed to be
  called on a schedule — either via pg_cron or the application's
  background scheduler — to avoid trigger-induced latency on inserts.

Revision ID: 025
Revises: 024
Create Date: 2026-03-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Idempotent: skip if table already exists
    if 'god_events' in inspector.get_table_names():
        return

    is_postgres = conn.dialect.name == 'postgresql'

    if is_postgres:
        payload_type = postgresql.JSONB(astext_type=sa.Text())
        created_at_type = sa.DateTime(timezone=True)
        created_at_default = sa.text("now()")
    else:
        # SQLite fallback
        payload_type = sa.Text()
        created_at_type = sa.Float()
        created_at_default = None

    columns = [
        sa.Column("id", sa.BigInteger if is_postgres else sa.Integer,
                  primary_key=True, autoincrement=True),
        sa.Column("god_name", sa.Text, nullable=False),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload", payload_type, nullable=True),
        sa.Column("severity", sa.Text, nullable=False, server_default="info"),
    ]
    if created_at_default is not None:
        columns.append(sa.Column("created_at", created_at_type, nullable=False,
                                 server_default=created_at_default))
    else:
        columns.append(sa.Column("created_at", created_at_type, nullable=False))

    op.create_table("god_events", *columns)

    op.create_index("idx_god_events_name_created", "god_events",
                    ["god_name", "created_at"])
    op.create_index("idx_god_events_created", "god_events", ["created_at"])
    op.create_index("idx_god_events_type", "god_events", ["event_type"])

    # Postgres-only: pruning stored procedure
    if is_postgres:
        op.execute(sa.text("""
            CREATE OR REPLACE FUNCTION prune_old_god_events(
                retention INTERVAL DEFAULT INTERVAL '7 days'
            )
            RETURNS INTEGER
            LANGUAGE plpgsql
            AS $$
            DECLARE
                deleted_count INTEGER;
            BEGIN
                DELETE FROM god_events
                WHERE  created_at < now() - retention;
                GET DIAGNOSTICS deleted_count = ROW_COUNT;
                RETURN deleted_count;
            END;
            $$;
        """))


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == 'postgresql':
        op.execute(sa.text(
            "DROP FUNCTION IF EXISTS prune_old_god_events(INTERVAL)"
        ))
    op.drop_index("idx_god_events_type")
    op.drop_index("idx_god_events_created")
    op.drop_index("idx_god_events_name_created")
    op.drop_table("god_events")
