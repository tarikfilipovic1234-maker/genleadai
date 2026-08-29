"""Alembic environment.

Two deliberate choices:

  * The database URL comes from ``app.config``, not from alembic.ini, so the
    application and its migrations can never disagree about which database
    they are pointed at.
  * Migrations run through the async engine, because that is the driver the
    application uses; running them on a second, synchronous driver is a good
    way to have migrations succeed against a database the app cannot open.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection

from alembic import context
from app.config import get_settings

# Importing the models module is what populates Base.metadata. Without this
# import, autogenerate cheerfully produces an empty migration.
from app.db import models  # noqa: F401
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting - `alembic upgrade head --sql`."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type changes and server-default changes during
        # autogenerate; both are silently ignored by default, which produces
        # migrations that quietly omit real schema drift.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
