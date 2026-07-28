"""Alembic environment.

Reads the database URL from application ``Settings`` rather than ``alembic.ini``
so there is exactly one source of truth. The failure mode this prevents is
specific and nasty: an ``alembic.ini`` URL that has drifted from ``DATABASE_URL``
migrates a *different* database than the one the app talks to, and the symptom
is "the migration ran fine but the column still isn't there".
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.config import get_settings
from backend.db.base import Base

# Import for the side effect of registering every table on Base.metadata.
# Without it, autogenerate sees an empty model set and cheerfully generates a
# migration that DROPS every table.
import backend.db.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``).

    Useful when a DBA applies migrations by hand, or in a deploy pipeline that
    wants the SQL reviewed before it runs.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # compare_type: without it, changing VARCHAR(255) to VARCHAR(512)
            # produces an empty migration and the constraint silently stays.
            compare_type=True,
            # compare_server_default: catches default drift, which otherwise
            # only shows up as unexpected NULLs in new rows.
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
