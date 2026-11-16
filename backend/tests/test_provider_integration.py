"""Provider framework against a real database: adapter -> ingestion -> storage."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType, IndicatorFrequency, ObservationType, ProviderHealthStatus
from app.providers.errors import ProviderUnavailableError
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository
from app.repositories.market_data import SqlMarketDataRepository
from app.repositories.provider_health import SqlProviderHealthRepository
from app.repositories.providers import SqlProviderRepository
from app.schemas.normalized import IndicatorValue
from app.services.ingestion import IngestionService
from app.services.provider_health import DOWN_AFTER_FAILURES, ProviderHealthService
from tests.providers_support import ExampleProvider, make_client

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
FAR_PAST = datetime(2000, 1, 1, tzinfo=UTC)
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)

# 1794816000 is 2026-11-16T08:00:00Z.
QUOTES_BODY = b"""{"data": [
    {"symbol": "AAPL.X", "price": 189.9500, "ts": 1794816000},
    {"symbol": "MSFT.X", "price": 415.1000, "ts": 1794816000},
    {"symbol": "NOPE.X", "price": 1.0, "ts": 1794816000}
]}"""


@pytest.fixture
async def provider_id(session: AsyncSession) -> uuid.UUID:
    return await SqlProviderRepository(session).upsert(
        name="example", base_url="https://api.example.test", enabled=True, configuration={}
    )


@pytest.fixture
async def instrument_ids(session: AsyncSession, provider_id: uuid.UUID) -> dict[str, uuid.UUID]:
    repository = SqlInstrumentRepository(session)
    ids = {}
    for symbol, enabled in (("AAPL", True), ("MSFT", False)):
        ids[symbol] = await repository.upsert(
            symbol=symbol,
            name=symbol,
            asset_type=AssetType.STOCK,
            exchange_id=None,
            currency="USD",
            enabled=enabled,
            metadata={},
        )
        await repository.upsert_mapping(
            instrument_id=ids[symbol], provider_id=provider_id, provider_symbol=f"{symbol}.X"
        )
    return ids


def ingestion_service(session: AsyncSession) -> IngestionService:
    return IngestionService(
        instruments=SqlInstrumentRepository(session),
        market_data=SqlMarketDataRepository(session),
        economic_data=SqlEconomicDataRepository(session),
        clock=lambda: NOW,
    )


async def test_adapter_output_is_ingested_and_stored(
    session: AsyncSession, provider_id: uuid.UUID, instrument_ids: dict[str, uuid.UUID]
) -> None:
    provider = ExampleProvider(
        make_client(lambda request: httpx.Response(200, content=QUOTES_BODY))
    )
    service = ingestion_service(session)

    quotes = await provider.fetch_quotes(["AAPL.X", "MSFT.X", "NOPE.X"])
    first = await service.ingest_quotes(provider_id, quotes)
    again = await service.ingest_quotes(provider_id, quotes)

    # MSFT is disabled and NOPE is not tracked: both are rejected, not stored.
    assert (first.received, first.written, first.rejected) == (3, 1, 2)
    assert (again.written, again.unchanged, again.rejected) == (0, 1, 2)
    (stored,) = await SqlMarketDataRepository(session).get_observations(
        instrument_ids["AAPL"], ObservationType.PRICE, start=FAR_PAST, end=FAR_FUTURE
    )
    assert stored.value == Decimal("189.9500")
    assert stored.observed_at == datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
    assert stored.provider_id == provider_id
    assert stored.currency == "USD"


async def test_mapped_instruments_are_keyed_by_provider_symbol(
    session: AsyncSession, provider_id: uuid.UUID, instrument_ids: dict[str, uuid.UUID]
) -> None:
    mapped = await SqlInstrumentRepository(session).get_instruments_by_provider_symbol(provider_id)

    assert {symbol: instrument.id for symbol, instrument in mapped.items()} == {
        "AAPL.X": instrument_ids["AAPL"]
    }


async def test_indicator_values_are_ingested_with_revisions(
    session: AsyncSession, provider_id: uuid.UUID
) -> None:
    economic = SqlEconomicDataRepository(session)
    indicator_id = await economic.upsert_indicator(
        code="EX_CPI",
        name="Example CPI",
        country="US",
        unit="index",
        frequency=IndicatorFrequency.MONTHLY,
        provider_id=provider_id,
        provider_series_code="CPI",
        enabled=True,
        metadata={},
    )
    period = datetime(2026, 10, 1, tzinfo=UTC)

    def value(amount: str) -> IndicatorValue:
        return IndicatorValue(series_code="CPI", observed_at=period, value=Decimal(amount))

    first = await ingestion_service(session).ingest_indicator_values(provider_id, [value("310.3")])
    later = IngestionService(
        instruments=SqlInstrumentRepository(session),
        market_data=SqlMarketDataRepository(session),
        economic_data=economic,
        clock=lambda: NOW + timedelta(days=30),
    )
    revised = await later.ingest_indicator_values(provider_id, [value("310.9")])

    assert (first.written, revised.written) == (1, 1)
    history = await economic.get_observations(
        indicator_id, start=FAR_PAST, end=FAR_FUTURE, include_revisions=True
    )
    assert [row.value for row in history] == [Decimal("310.3"), Decimal("310.9")]
    by_provider = await economic.list_indicators(provider_id=provider_id, limit=None)
    assert [indicator.code for indicator in by_provider] == ["EX_CPI"]
    assert await economic.list_indicators(provider_id=uuid.uuid4()) == []


async def test_provider_health_follows_failures_and_recovery(
    session: AsyncSession, provider_id: uuid.UUID
) -> None:
    repository = SqlProviderHealthRepository(session)
    clock = iter(NOW + timedelta(minutes=i) for i in range(10))
    service = ProviderHealthService(repository, clock=lambda: next(clock))

    async def failing() -> None:
        raise ProviderUnavailableError("example", "request timed out")

    async def working() -> str:
        return "data"

    assert await repository.get(provider_id) is None

    statuses = []
    for _ in range(DOWN_AFTER_FAILURES):
        assert await service.guard(provider_id, failing) is None
        health = await repository.get(provider_id)
        assert health is not None
        statuses.append((health.status, health.consecutive_failures))

    assert statuses == [
        (ProviderHealthStatus.DEGRADED, 1),
        (ProviderHealthStatus.DEGRADED, 2),
        (ProviderHealthStatus.DOWN, 3),
    ]
    assert health.last_error == "ProviderUnavailableError: request timed out"
    assert health.last_error_at == NOW + timedelta(minutes=2)
    assert health.last_success_at is None

    assert await service.guard(provider_id, working) == "data"

    health = await repository.get(provider_id)
    assert health is not None
    assert (health.status, health.consecutive_failures) == (ProviderHealthStatus.HEALTHY, 0)
    assert health.last_success_at == NOW + timedelta(minutes=3)
    # The last error stays visible after recovery.
    assert health.last_error == "ProviderUnavailableError: request timed out"
    assert [row.provider_id for row in await repository.list()] == [provider_id]


async def test_long_error_text_is_truncated(session: AsyncSession, provider_id: uuid.UUID) -> None:
    repository = SqlProviderHealthRepository(session)

    await repository.record_failure(provider_id, NOW, "x" * 5000, down_after=3)

    health = await repository.get(provider_id)
    assert health is not None and health.last_error is not None
    assert len(health.last_error) == 1000


async def test_unknown_candle_interval_is_rejected_by_the_database(
    session: AsyncSession, provider_id: uuid.UUID, instrument_ids: dict[str, uuid.UUID]
) -> None:
    with pytest.raises(IntegrityError, match="ck_ohlcv_interval"):
        await session.execute(
            sa.text(
                "INSERT INTO ohlcv (instrument_id, provider_id, interval, timestamp,"
                " open, high, low, close) VALUES (:i, :p, '1d', now(), 1, 1, 1, 1)"
            ),
            {"i": instrument_ids["AAPL"], "p": provider_id},
        )
