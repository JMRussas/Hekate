#  Orchestration Engine - Migration Runner
#
#  Programmatic Alembic runner for applying migrations at startup.
#  Handles pre-Alembic databases by stamping them at revision 001.
#
#  Depends on: backend/migrations/
#  Used by:    backend/db/connection.py

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

logger = logging.getLogger("orchestration.migrate")

_MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def run_migrations(db_path: str | Path) -> None:
    """Apply pending Alembic migrations to the database.

    Handles three cases:
    1. Fresh database — runs all migrations from scratch.
    2. Pre-Alembic database (has tables but no alembic_version) — stamps at 001, then upgrades.
    3. Already-migrated database — runs only pending migrations.
    """
    db_path = Path(db_path)
    url = f"sqlite:///{db_path}"

    alembic_cfg = Config(str(_MIGRATIONS_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    alembic_cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))

    engine = create_engine(url)
    try:
        with engine.connect():
            inspector = inspect(engine)
            tables = inspector.get_table_names()

            has_schema = "projects" in tables
            has_alembic = "alembic_version" in tables

            if has_schema and not has_alembic:
                # Pre-Alembic database: stamp at 001 (schema already applied)
                logger.info("Pre-Alembic database detected, stamping at revision 001")
                command.stamp(alembic_cfg, "001")
            elif not has_schema and not has_alembic:
                logger.info("Fresh database, running all migrations")

        # Apply any pending migrations
        try:
            command.upgrade(alembic_cfg, "head")
            logger.info("Migrations complete (head)")
        except Exception as e:
            logger.critical("Migration failed: %s", e, exc_info=True)
            raise RuntimeError(f"Database migration failed: {e}") from e
    finally:
        engine.dispose()
