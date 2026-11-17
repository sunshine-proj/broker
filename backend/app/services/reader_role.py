"""The read-only database role Grafana connects with.

Applied on every startup, after migrations, so that tables and views added by
a migration are readable too. The role can only read: it is denied write
privileges, its transactions are read-only, and each statement is cut off
after a timeout so a heavy dashboard query cannot hold the database.
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


async def ensure_reader_role(connection: AsyncConnection, name: str, password: str) -> None:
    """Create or update the role and grant it read access to the current database.

    The caller commits. The role name and password reach PostgreSQL as bound
    parameters to format(), which quotes them; neither is logged.
    """
    if not _ROLE_NAME.match(name):
        raise ReaderRoleError(f"invalid role name {name!r}: use lower-case letters, digits, _")
    if not password:
        raise ReaderRoleError("the reader role needs a password")

    async def run(template: str, literal: str | None = None) -> None:
        """Run a statement that PostgreSQL's format() builds from the role name.

        %I quotes the role name as an identifier and %L the literal as a string.
        """
        if literal is None:
            query, params = (
                "SELECT format(CAST(:template AS text), CAST(:name AS text))",
                {"template": template, "name": name},
            )
        else:
            query = (
                "SELECT format(CAST(:template AS text), CAST(:name AS text),"
                " CAST(:literal AS text))"
            )
            params = {"template": template, "name": name, "literal": literal}
        statement = await connection.scalar(sa.text(query), params)
        # Raw driver SQL: the built statement may contain characters that
        # SQLAlchemy's text() would read as bind parameters.
        await connection.exec_driver_sql(statement)

    exists = await connection.scalar(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :name"), {"name": name}
    )
    if not exists:
        await run("CREATE ROLE %I")
        logger.info("Read-only role created", extra={"role": name})
    await run(
        "ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L",
        password,
    )
    await run("ALTER ROLE %I SET default_transaction_read_only = on")
    await run("ALTER ROLE %I SET statement_timeout = %L", STATEMENT_TIMEOUT)

    database = await connection.scalar(sa.text("SELECT current_database()"))
    grant_connect = await connection.scalar(
        sa.text(
            "SELECT format('GRANT CONNECT ON DATABASE %I TO %I',"
            " CAST(:db AS text), CAST(:name AS text))"
        ),
        {"db": database, "name": name},
    )
    await connection.exec_driver_sql(grant_connect)
    await run("GRANT USAGE ON SCHEMA public TO %I")
    # Exactly read access: whatever was granted before is replaced.
    await run("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I")
    await run("GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I")
    await run("REVOKE SELECT ON alembic_version FROM %I")
    logger.info("Read-only role granted read access", extra={"role": name, "database": database})
