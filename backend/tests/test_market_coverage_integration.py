"""Phase 8 coverage end to end: the shipped seed, its jobs, the adapters and the views.

The shipped seed file is applied to the test database and the new providers'
jobs are run as the scheduler runs them. Every HTTP request is answered from
the recorded fixtures; nothing calls the network.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.collectors.runner import JobRunner
from app.config.settings import Settings
from app.database import create_session_factory
from app.models.enums import CollectorStatus, ExecutionTrigger
from app.providers.factory import ProviderFactory
from app.repositories.collection_jobs import SqlCollectionJobRepository
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository
from app.repositories.providers import SqlProviderRepository
from app.services.seed import apply_seed, load_seed_file

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

FIXTURES = Path(__file__).parent / "fixtures"
EMPTY_TREASURY_FEED = FIXTURES / "us_treasury" / "yield_curve_empty.xml"


def _fixture(path: str) -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / path).read_bytes())


def recorded_apis(request: httpx.Request) -> httpx.Response:
    """Every provider the new jobs call, answered from recorded responses."""
    host, path = request.url.host, request.url.path
    if host == "data-api.binance.vision":
        return _fixture("binance/ticker_24hr.json")
    if host == "markets.newyorkfed.org":
        for rate in ("sofr", "effr"):
            if f"/{rate}/" in path:
                return _fixture(f"nyfed/{rate}_last.json")
        return httpx.Response(200, json={"refRates": []})
    if host == "home.treasury.gov":
        # The recorded month stands in for the current one; other months are empty.
        month = request.url.params["field_tdr_date_value_month"]
        if month == f"{datetime.now(UTC):%Y%m}":
            return _fixture("us_treasury/yield_curve_202611.xml")
        return httpx.Response(200, content=EMPTY_TREASURY_FEED.read_bytes())
    if host == "data-api.ecb.europa.eu":
        flow = path.split("/")[3]
        return _fixture(
            {
                "EXR": "ecb/exr_latest.json",
                "EST": "ecb/estr_latest.json",
                "FM": "ecb/key_rates.json",
                "YC": "ecb/yield_curve_latest.json",
            }[flow]
        )
    return httpx.Response(404)


@pytest.fixture
async def runner(
    migrated_database: Settings, empty_database: AsyncEngine
) -> AsyncIterator[tuple[JobRunner, async_sessionmaker[AsyncSession]]]:
    sessions = create_session_factory(empty_database)
    async with sessions() as session:
        await apply_seed(
            load_seed_file(migrated_database.seed_file),
            providers=SqlProviderRepository(session),
            instruments=SqlInstrumentRepository(session),
            economic_data=SqlEconomicDataRepository(session),
            jobs=SqlCollectionJobRepository(session),
        )
        await session.commit()
    providers = ProviderFactory(
        migrated_database, http=httpx.AsyncClient(transport=httpx.MockTransport(recorded_apis))
    )
    try:
        yield (
            JobRunner(engine=empty_database, session_factory=sessions, provider_factory=providers),
            sessions,
        )
    finally:
        await providers.aclose()


async def latest(sessions: async_sessionmaker[AsyncSession], symbol: str) -> list[sa.Row]:
    async with sessions() as session:
        result = await session.execute(
            sa.text(
                "SELECT provider, observation_type, observed_at, value, currency, quote_kind, "
                "freshness FROM v_latest_observation WHERE symbol = :symbol ORDER BY provider"
            ),
            {"symbol": symbol},
        )
        return list(result)


async def test_new_jobs_store_their_data_side_by_side_with_existing_providers(
    runner: tuple[JobRunner, async_sessionmaker[AsyncSession]],
) -> None:
    job_runner, sessions = runner

    for job in ("binance_quotes", "nyfed_quotes", "us_treasury_quotes", "ecb_daily_quotes"):
        result = await job_runner.run(job, ExecutionTrigger.MANUAL)
        assert result is not None
        assert (result.status, result.error) == (CollectorStatus.SUCCESS, None), job
        assert result.records_written > 0, job

    # Binance: a Tether-quoted exchange price, its own instrument.
    (btc,) = await latest(sessions, "BTC/USDT")
    assert (btc.provider, btc.observation_type, btc.currency) == ("binance", "price", "USDT")
    assert (btc.value, btc.quote_kind, btc.freshness) == (
        Decimal("64373.44"),
        "traded",
        "real_time",
    )
    assert await latest(sessions, "BTC/USD") == []

    # SOFR from its administrator.
    (sofr,) = await latest(sessions, "SOFR")
    assert (sofr.provider, sofr.observed_at, sofr.value) == (
        "nyfed",
        datetime(2026, 11, 13, tzinfo=UTC),
        Decimal("3.90"),
    )

    # The Treasury curve, on the instruments FRED also maps.
    (ten_year,) = await latest(sessions, "US10Y")
    assert (ten_year.provider, ten_year.observation_type) == ("us_treasury", "yield")
    assert (ten_year.observed_at, ten_year.value) == (
        datetime(2026, 11, 16, tzinfo=UTC),
        Decimal("5.12"),
    )

    # Euro area yields keep every decimal the ECB sends.
    (bund,) = await latest(sessions, "EA-AAA-10Y")
    assert (bund.provider, bund.observation_type, bund.value) == (
        "ecb",
        "yield",
        Decimal("3.4115908883"),
    )


async def test_the_yield_curve_has_one_point_per_maturity(
    runner: tuple[JobRunner, async_sessionmaker[AsyncSession]],
) -> None:
    job_runner, sessions = runner
    await job_runner.run("us_treasury_quotes", ExecutionTrigger.MANUAL)

    async with sessions() as session:
        maturities = (
            await session.execute(
                sa.text(
                    "SELECT instrument_metadata->>'maturity' FROM v_latest_observation "
                    "WHERE instrument_metadata->>'curve' = 'US Treasury' "
                    "AND provider = 'us_treasury'"
                )
            )
        ).scalars()
        assert sorted(maturities) == sorted(
            ["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]
        )
