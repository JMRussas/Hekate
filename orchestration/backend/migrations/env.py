#  Orchestration Engine - Alembic Environment
#
#  Configures Alembic with the project's SQLAlchemy metadata.
#  Uses SYNC SQLAlchemy engine (not async) to avoid nested event loop issues.
#
#  Depends on: backend/db/models_metadata.py
#  Used by:    alembic CLI, backend/db/migrate.py

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.db.models_metadata import metadata as target_metadata

# Alembic Config object
config = context.config

# Logging setup from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — generate SQL without connecting."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # Required for SQLite ALTER TABLE support
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connect and apply."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # Required for SQLite ALTER TABLE support
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
