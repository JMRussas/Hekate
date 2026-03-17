#  Orchestration Engine - Migration Runner
#
#  Programmatic Alembic runner for applying migrations at startup.
#  Supports both Postgres (DSN) and SQLite (file path).
#
#  Depends on: backend/migrations/
#  Used by:    backend/db/connection.py

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger("orchestration.migrate")

_MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"
_VERSIONS_DIR = _MIGRATIONS_DIR / "versions"


def _get_head_revision() -> str:
    """Get the head revision by scanning migration filenames."""
    max_rev = "000"
    for f in _VERSIONS_DIR.glob("*.py"):
        match = re.match(r"^(\d+)_", f.name)
        if match:
            rev = match.group(1)
            if rev > max_rev:
                max_rev = rev
    return max_rev


def _is_postgres(target: str) -> bool:
    return str(target).startswith("postgresql://") or str(target).startswith("postgres://")


def _get_current_revision_sqlite(db_path: Path) -> str | None:
    """Get the current DB revision from SQLite."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        )
        if not cur.fetchone():
            conn.close()
            return None
        cur = conn.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _get_current_revision_postgres(dsn: str) -> str | None:
    """Get the current DB revision from Postgres using synchronous connection."""
    try:
        # Use synchronous SQLAlchemy to avoid asyncio.run() issues in to_thread()
        from sqlalchemy import create_engine, text
        url = dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
        engine = create_engine(url)
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'alembic_version')"
            ))
            if not result.scalar():
                return None
            result = conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _has_schema_sqlite(db_path: Path) -> bool:
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='projects'"
        )
        result = cur.fetchone() is not None
        conn.close()
        return result
    except Exception:
        return False


def _has_schema_postgres(dsn: str) -> bool:
    try:
        from sqlalchemy import create_engine, text
        url = dsn.replace("postgresql://", "postgresql+psycopg2://", 1)
        engine = create_engine(url)
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'projects')"
            ))
            return result.scalar() or False
    except Exception:
        return False


def run_migrations(target) -> None:
    """Apply pending Alembic migrations.

    Args:
        target: Postgres DSN string or SQLite file Path.
    """
    target_str = str(target)
    is_pg = _is_postgres(target_str)

    head = _get_head_revision()
    current = (
        _get_current_revision_postgres(target_str) if is_pg
        else _get_current_revision_sqlite(Path(target_str))
    )

    if current and current == head:
        logger.info("Database already at head (%s), skipping migrations", head)
        return

    logger.info("Database at %s, head is %s — running migrations", current, head)

    from alembic import command
    from alembic.config import Config

    if is_pg:
        # Alembic runs synchronously — use psycopg2, not asyncpg
        url = target_str.replace("postgresql://", "postgresql+psycopg2://", 1)
    else:
        url = f"sqlite:///{target_str}"

    alembic_cfg = Config(str(_MIGRATIONS_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    alembic_cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))

    if current is None:
        has_schema = (
            _has_schema_postgres(target_str) if is_pg
            else _has_schema_sqlite(Path(target_str))
        )
        if has_schema:
            logger.info("Pre-Alembic database detected, stamping at revision 001")
            command.stamp(alembic_cfg, "001")
        else:
            logger.info("Fresh database, running all migrations")

    try:
        command.upgrade(alembic_cfg, "head")
        logger.info("Migrations complete (head)")
    except Exception as e:
        logger.critical("Migration failed: %s", e, exc_info=True)
        raise RuntimeError(f"Database migration failed: {e}") from e
