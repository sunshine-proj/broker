"""Alembic environment: runs migrations over the application's async engine."""

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config.logging import configure_logging
from app.config.settings import get_settings
from app.models import Base  # importing the package registers every table

config = context.config
target_metadata = Base.metadata

# Only when run from the command line: tests leave logging to pytest.
if config.cmd_opts is not None:
    _settings = get_settings()
    configure_logging(_settings.log_level, _settings.log_format)


def _database_url() -> str:
    # Tests pass a URL through `Config.attributes` to target the test database.
    url = config.attributes.get("database_url") or get_settings().database_url()
    return url.render_as_string(hide_password=False) if not isinstance(url, str) else url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of connecting (alembic upgrade --sql)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(_run_migrations_online())
