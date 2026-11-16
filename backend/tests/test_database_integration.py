import asyncio

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.main import create_app

pytestmark = pytest.mark.integration


def _fetchval(settings: Settings, query: str) -> object:
    async def run() -> object:
        connection = await asyncpg.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
            database=settings.postgres_db,
        )
        try:
            return await connection.fetchval(query)
        finally:
            await connection.close()

    return asyncio.run(run())


def test_migrations_upgrade_to_head(migrated_database: Settings) -> None:
    version = _fetchval(migrated_database, "SELECT version_num FROM alembic_version")
    extension = _fetchval(
        migrated_database, "SELECT extname FROM pg_extension WHERE extname = 'timescaledb'"
    )

    assert version is not None
    assert extension == "timescaledb"


def test_migrations_downgrade_and_upgrade_again(
    migrated_database: Settings, alembic_config: Config
) -> None:
    command.downgrade(alembic_config, "base")
    assert _fetchval(migrated_database, "SELECT count(*) FROM alembic_version") == 0

    command.upgrade(alembic_config, "head")
    assert _fetchval(migrated_database, "SELECT count(*) FROM alembic_version") == 1


def test_health_with_real_database(migrated_database: Settings) -> None:
    with TestClient(create_app(migrated_database)) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_health_when_database_is_unreachable(migrated_database: Settings) -> None:
    # Port 1 on the database host refuses connections.
    unreachable = migrated_database.model_copy(
        update={"postgres_port": 1, "database_connect_timeout_seconds": 2.0}
    )

    with TestClient(create_app(unreachable)) as client:
        response = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
