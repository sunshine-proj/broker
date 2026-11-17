import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.config.settings import Settings
from app.database import create_engine, get_session
from app.main import create_app
from tests.sample_data import ids  # noqa: F401  (registers the fixture)

BACKEND_DIR = Path(__file__).resolve().parents[1]


class FakeSession:
    """Stands in for a database session in unit tests."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def execute(self, *args: object, **kwargs: object) -> None:
        if self.error is not None:
            raise self.error


# --- Unit test fixtures: no network, no database -----------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        postgres_db="unit",
        postgres_user="unit",
        postgres_password="unit",
        log_format="text",
    )


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()


@pytest.fixture
def app(settings: Settings, fake_session: FakeSession) -> FastAPI:
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: fake_session
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # raise_server_exceptions=False lets the global 500 handler answer, as in production.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


# --- Integration test fixtures: real TimescaleDB, separate test database ------


def _unavailable(reason: str) -> None:
    """Skip locally; fail where a database is expected (REQUIRE_DATABASE=1, as in CI)."""
    if os.environ.get("REQUIRE_DATABASE") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


async def _ensure_database_exists(settings: Settings) -> None:
    connection = await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        database=settings.postgres_db,
        timeout=5,
    )
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", settings.postgres_test_db
        )
        if not exists:
            await connection.execute(f'CREATE DATABASE "{settings.postgres_test_db}"')
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    """Settings pointing at the test database, created on first use."""
    try:
        base = Settings()
    except ValidationError:
        _unavailable("database settings (POSTGRES_*) are not configured")
    if base.postgres_test_db == base.postgres_db:
        pytest.fail("POSTGRES_TEST_DB must differ from POSTGRES_DB")
    try:
        asyncio.run(_ensure_database_exists(base))
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        _unavailable(f"test database is not reachable: {exc}")
    return base.model_copy(update={"postgres_db": base.postgres_test_db})


@pytest.fixture(scope="session")
def alembic_config(integration_settings: Settings) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["database_url"] = integration_settings.database_url()
    return config


@pytest.fixture(scope="session")
def migrated_database(alembic_config: Config, integration_settings: Settings) -> Settings:
    """The test database, migrated to the latest revision."""
    command.upgrade(alembic_config, "head")
    return integration_settings


@pytest.fixture
def anyio_backend() -> str:
    """Async tests (marked `anyio`) run on asyncio."""
    return "asyncio"


@pytest.fixture
async def session(migrated_database: Settings) -> AsyncIterator[AsyncSession]:
    """A session whose work is rolled back after the test, including commits."""
    engine = create_engine(migrated_database)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        async with AsyncSession(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ) as test_session:
            yield test_session
        await transaction.rollback()
    await engine.dispose()


# Every table holding data; truncated by `empty_database`.
DATA_TABLES = (
    "data_quality_issue",
    "collector_execution",
    "collection_job",
    "provider_health",
    "market_observation",
    "ohlcv",
    "economic_observation",
    "economic_indicator",
    "instrument_provider_mapping",
    "instrument",
    "exchange",
    "provider",
)


async def truncate_data(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(sa.text(f"TRUNCATE {', '.join(DATA_TABLES)} CASCADE"))


@pytest.fixture
async def empty_database(migrated_database: Settings) -> AsyncIterator[AsyncEngine]:
    """For tests that must commit: the test database emptied before and after."""
    engine = create_engine(migrated_database)
    await truncate_data(engine)
    try:
        yield engine
    finally:
        await truncate_data(engine)
        await engine.dispose()
