#  Orchestration Engine - Migration Runner
#
#  Programmatic Alembic runner for applying migrations at startup.
#  Handles pre-Alembic databases by stamping them at revision 001.
#  Fast-paths when already at head (avoids SQLAlchemy connection overhead).
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
    """Get the head revision by scanning migration filenames directly.

    Avoids importing Alembic's ScriptDirectory which triggers env.py
    and creates a SQLAlchemy engine — hanging on Windows with locked DBs.
    """
    # Migration files are named NNN_description.py (e.g., 015_add_foo.py)
    max_rev = "000"
    for f in _VERSIONS_DIR.glob("*.py"):
        match = re.match(r"^(\d+)_", f.name)
        if match:
            rev = match.group(1)
            if rev > max_rev:
                max_rev = rev
    return max_rev


def _get_current_revision(db_path: Path) -> str | None:
    """Get the current DB revision using raw sqlite3 (fast, no SQLAlchemy)."""
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


def run_migrations(db_path: str | Path) -> None:
    """Apply pending Alembic migrations to the database.

    Fast-paths when already at head — avoids creating a SQLAlchemy engine
    entirely, which can hang on Windows due to SQLite locking.

    Handles three cases:
    1. Fresh database — runs all migrations from scratch.
    2. Pre-Alembic database (has tables but no alembic_version) — stamps at 001, then upgrades.
    3. Already-migrated database — runs only pending migrations.
    """
    db_path = Path(db_path)

    # Fast-path: skip Alembic entirely when already at head.
    head = _get_head_revision()
    current = _get_current_revision(db_path)
    if current and current == head:
        logger.info("Database already at head (%s), skipping migrations", head)
        return

    logger.info("Database at %s, head is %s — running migrations", current, head)

    # Only import Alembic when we actually need to run migrations.
    # This avoids env.py triggering SQLAlchemy engine creation on the fast path.
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{db_path}"
    alembic_cfg = Config(str(_MIGRATIONS_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    alembic_cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))

    # Check if this is a pre-Alembic database (has schema but no version table)
    if current is None:
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='projects'"
            )
            has_schema = cur.fetchone() is not None
            conn.close()

            if has_schema:
                logger.info("Pre-Alembic database detected, stamping at revision 001")
                command.stamp(alembic_cfg, "001")
            else:
                logger.info("Fresh database, running all migrations")
        except Exception as e:
            logger.warning("Pre-flight check failed: %s", e)

    # Apply pending migrations
    try:
        command.upgrade(alembic_cfg, "head")
        logger.info("Migrations complete (head)")
    except Exception as e:
        logger.critical("Migration failed: %s", e, exc_info=True)
        raise RuntimeError(f"Database migration failed: {e}") from e
