"""The restricted database roles the running services connect with.

- The reader role (Grafana) can only read: write privileges are revoked, its
  transactions are read-only, and each statement is cut off after a timeout
  so a heavy dashboard query cannot hold the database.
- The application role (the backend) can read, insert and update, which is
  all the running backend does. It cannot delete, truncate or change the
  schema; migrations, the seed file and the command line run as the owner.

Both are applied on every startup, after migrations, so that tables and views
added by a migration are covered too.
"""

import logging
import re

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
STATEMENT_TIMEOUT = "30s"


class ReaderRoleError(Exception):
    pass


class _RoleBuilder:
    """Runs statements built by PostgreSQL's format() from the role name.

    The role name and password reach PostgreSQL as bound parameters to
    format(), which quotes them (%I as an identifier, %L as a literal);
    neither is logged.
    """

    def __init__(self, connection: AsyncConnection, name: str) -> None:
        self._connection = connection
        self.name = name

    async def run(self, template: str, literal: str | None = None) -> None:
        if literal is None:
            query, params = (
                "SELECT format(CAST(:template AS text), CAST(:name AS text))",
                {"template": template, "name": self.name},
            )
        else:
            query = (
                "SELECT format(CAST(:template AS text), CAST(:name AS text),"
                " CAST(:literal AS text))"
            )
            params = {"template": template, "name": self.name, "literal": literal}
        statement = await self._connection.scalar(sa.text(query), params)
        # Raw driver SQL: the built statement may contain characters that
        # SQLAlchemy's text() would read as bind parameters.
        await self._connection.exec_driver_sql(statement)

    async def login(self, password: str) -> str:
        """Create the role if needed, set its password and connect privilege; returns the db."""
        exists = await self._connection.scalar(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": self.name}
        )
        if not exists:
            await self.run("CREATE ROLE %I")
            logger.info("Database role created", extra={"role": self.name})
        await self.run(
            "ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "NOBYPASSRLS PASSWORD %L",
            password,
        )
        database = await self._connection.scalar(sa.text("SELECT current_database()"))
        grant_connect = await self._connection.scalar(
            sa.text(
                "SELECT format('GRANT CONNECT ON DATABASE %I TO %I',"
                " CAST(:db AS text), CAST(:name AS text))"
            ),
            {"db": database, "name": self.name},
        )
        await self._connection.exec_driver_sql(grant_connect)
        await self.run("GRANT USAGE ON SCHEMA public TO %I")
        # Exactly the privileges granted below: whatever was granted before is replaced.
        await self.run("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I")
        return str(database)


def _check(name: str, password: str) -> None:
    if not _ROLE_NAME.match(name):
        raise ReaderRoleError(f"invalid role name {name!r}: use lower-case letters, digits, _")
    if not password:
        raise ReaderRoleError(f"the role {name} needs a password")


async def ensure_reader_role(connection: AsyncConnection, name: str, password: str) -> None:
    """Create or update the read-only role and grant it read access to the current database.

    The caller commits.
    """
    _check(name, password)
    role = _RoleBuilder(connection, name)
    database = await role.login(password)
    await role.run("ALTER ROLE %I SET default_transaction_read_only = on")
    await role.run("ALTER ROLE %I SET statement_timeout = %L", STATEMENT_TIMEOUT)
    await role.run("GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I")
    await role.run("REVOKE SELECT ON alembic_version FROM %I")
    logger.info("Read-only role granted read access", extra={"role": name, "database": database})


async def ensure_app_role(connection: AsyncConnection, name: str, password: str) -> None:
    """Create or update the backend's role: read, insert and update, nothing else.

    The caller commits. TimescaleDB passes the privileges of a hypertable on
    to its chunks, compressed ones included.
    """
    _check(name, password)
    role = _RoleBuilder(connection, name)
    database = await role.login(password)
    await role.run("ALTER ROLE %I RESET default_transaction_read_only")
    await role.run("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO %I")
    # The schema version is the migrations' business.
    await role.run("REVOKE INSERT, UPDATE ON alembic_version FROM %I")
    logger.info("Application role granted access", extra={"role": name, "database": database})
