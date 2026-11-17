"""The backend's restricted database role (Phase 11): everything the backend does works
with it, and nothing else does.

The jobs, quality checks and metrics run here connect as the role, the way the
backend container does in Docker Compose.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.collectors.runner import JobRunner
from app.config.settings import Settings
from app.database import create_engine, create_session_factory
from app.main import create_app
from app.models.enums import CollectorStatus, ExecutionTrigger
from app.providers.factory import ADAPTERS, ProviderFactory
from app.repositories.collection_jobs import SqlCollectionJobRepository
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository
from app.repositories.providers import SqlProviderRepository
from app.schemas.seed import SeedFile
from app.services.reader_role import ReaderRoleError, ensure_app_role
from app.services.seed import apply_seed
from tests.test_quality_integration import MARKET, NOW, SEED, FakeProvider

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Roles are shared by every database on the server: not the application's own name.
ROLE = "marketdata_app_test"
PASSWORD = "test-only: not a secret"


@pytest.fixture
async def app_settings(
    migrated_database: Settings, empty_database: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Settings]:
    """The seed applied by the owner, then settings that connect as the application role."""
    monkeypatch.setitem(ADAPTERS, "fake", FakeProvider)
    for name in ("other", "nohistory"):
        monkeypatch.setitem(ADAPTERS, name, type(name, (FakeProvider,), {"name": name}))
    MARKET.prices.clear()
    MARKET.down = False
    async with create_session_factory(empty_database)() as session:
        await apply_seed(
            SeedFile.model_validate(SEED),
            providers=SqlProviderRepository(session),
            instruments=SqlInstrumentRepository(session),
            economic_data=SqlEconomicDataRepository(session),
            jobs=SqlCollectionJobRepository(session),
        )
        await session.commit()
    async with empty_database.begin() as connection:
        await ensure_app_role(connection, ROLE, PASSWORD)
    try:
        yield migrated_database.model_copy(
            update={"postgres_user": ROLE, "postgres_password": SecretStr(PASSWORD)}
        )
    finally:
        async with empty_database.begin() as admin:
            await admin.execute(sa.text(f"DROP OWNED BY {ROLE}"))
            await admin.execute(sa.text(f"DROP ROLE IF EXISTS {ROLE}"))


async def connect(settings: Settings) -> asyncpg.Connection:
    return await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=ROLE,
        password=PASSWORD,
        database=settings.postgres_db,
    )


async def test_the_backend_collects_checks_and_backfills_as_the_role(
    app_settings: Settings,
) -> None:
    engine = create_engine(app_settings)
    providers = ProviderFactory(app_settings, http=httpx.AsyncClient())
    clock = [NOW - timedelta(hours=4)]
    runner = JobRunner(
        engine=engine,
        session_factory=create_session_factory(engine),
        provider_factory=providers,
        clock=lambda: clock[0],
    )
    try:
        MARKET.prices["BTC"] = [(clock[0], Decimal(100))]
        assert (await runner.run("crypto_quotes", ExecutionTrigger.SCHEDULED)).status is (
            CollectorStatus.SUCCESS
        )
        # Two hours missed, then a gap found and backfilled, all as the role.
        MARKET.prices["BTC"] += [(clock[0] + timedelta(minutes=m), Decimal(101)) for m in (30, 60)]
        clock[0] += timedelta(hours=2)
        MARKET.prices["BTC"].append((clock[0], Decimal(100)))
        await runner.run("crypto_quotes", ExecutionTrigger.SCHEDULED)
        assert (
            await runner.run("quality", ExecutionTrigger.MANUAL)
        ).status is CollectorStatus.SUCCESS
        backfill = await runner.run("fake_backfill", ExecutionTrigger.MANUAL)
        assert (backfill.status, backfill.records_written) == (CollectorStatus.SUCCESS, 2)
        # An interrupted run is closed at start-up, which is an update.
        assert await runner.mark_interrupted() == 0
    finally:
        await providers.aclose()
        await engine.dispose()


async def test_the_api_and_metrics_work_as_the_role(app_settings: Settings) -> None:
    with TestClient(create_app(app_settings)) as client:
        assert client.get("/api/v1/health").json()["database"] == "ok"
        assert client.get("/api/v1/instruments").status_code == 200
        assert "marketdata_database_up 1.0" in client.get("/metrics").text


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM market_observation",
        "TRUNCATE collector_execution",
        "DROP VIEW v_observation",
        "CREATE TABLE intruder (id int)",
        "UPDATE alembic_version SET version_num = '0001'",
        "ALTER TABLE instrument ADD COLUMN x int",
        "CREATE ROLE intruder",
    ],
)
async def test_the_role_cannot_delete_or_change_the_schema(
    app_settings: Settings, statement: str
) -> None:
    connection = await connect(app_settings)
    try:
        with pytest.raises(asyncpg.PostgresError):
            await connection.execute(statement)
    finally:
        await connection.close()


async def test_the_role_writes_into_compressed_chunks(
    app_settings: Settings, empty_database: AsyncEngine
) -> None:
    old = datetime(2025, 1, 6, 12, tzinfo=UTC)
    async with empty_database.begin() as admin:
        ids = (
            await admin.execute(
                sa.text(
                    "SELECT i.id, p.id FROM instrument i, provider p "
                    "WHERE i.symbol = 'BTC/USD' AND p.name = 'fake'"
                )
            )
        ).one()
        await admin.execute(
            sa.text(
                "INSERT INTO market_observation (instrument_id, provider_id, observation_type, "
                "observed_at, value, currency, unit, freshness, quote_kind) VALUES "
                "(:i, :p, 'price', :at, 100, 'USD', 'currency', 'real_time', 'traded')"
            ),
            {"i": ids[0], "p": ids[1], "at": old},
        )
        await admin.execute(
            sa.text(
                "SELECT compress_chunk(c) FROM show_chunks('market_observation', "
                "older_than => TIMESTAMPTZ '2025-02-01') c"
            )
        )

    connection = await connect(app_settings)
    try:
        await connection.execute(
            "INSERT INTO market_observation (instrument_id, provider_id, observation_type, "
            "observed_at, value, currency, unit, freshness, quote_kind) VALUES "
            "($1, $2, 'price', $3, 101, 'USD', 'currency', 'real_time', 'traded') "
            "ON CONFLICT DO NOTHING",
            ids[0],
            ids[1],
            old + timedelta(hours=1),
        )
        updated = await connection.execute(
            "UPDATE market_observation SET quality_status = 'suspect' WHERE observed_at = $1", old
        )
    finally:
        await connection.close()

    assert updated == "UPDATE 1"


async def test_the_role_setup_is_repeatable_and_validated(
    app_settings: Settings, empty_database: AsyncEngine
) -> None:
    async with empty_database.begin() as connection:
        await ensure_app_role(connection, ROLE, PASSWORD)
        with pytest.raises(ReaderRoleError, match="needs a password"):
            await ensure_app_role(connection, ROLE, "")
        with pytest.raises(ReaderRoleError, match="invalid role name"):
            await ensure_app_role(connection, "x; DROP TABLE y", "pw")

    connection = await connect(app_settings)
    try:
        assert (
            await connection.fetchval(
                "SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = $1", ROLE
            )
            is False
        )
    finally:
        await connection.close()
