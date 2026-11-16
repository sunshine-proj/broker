"""The data model against a real TimescaleDB (PRD section 7)."""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.database import create_engine
from app.models import Base
from app.models.enums import (
    AssetType,
    CandleInterval,
    Freshness,
    IndicatorFrequency,
    ObservationType,
    QuoteKind,
)
from app.repositories.collection_jobs import SqlCollectionJobRepository
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository
from app.repositories.market_data import SqlMarketDataRepository
from app.repositories.providers import SqlProviderRepository
from app.schemas.market_data import EconomicObservationIn, MarketObservationIn, OHLCVIn
from app.schemas.seed import SeedFile
from app.services.seed import apply_seed

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

T0 = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
FAR_PAST = datetime(2000, 1, 1, tzinfo=UTC)
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)


@dataclass
class World:
    """Reference rows most tests need."""

    provider_a: uuid.UUID
    provider_b: uuid.UUID
    instrument: uuid.UUID
    indicator: uuid.UUID


@pytest.fixture
async def world(session: AsyncSession) -> World:
    providers = SqlProviderRepository(session)
    instruments = SqlInstrumentRepository(session)
    economic = SqlEconomicDataRepository(session)
    provider_a = await providers.upsert(
        name="provider_a", base_url="https://a.test", enabled=True, configuration={}
    )
    provider_b = await providers.upsert(
        name="provider_b", base_url="https://b.test", enabled=True, configuration={}
    )
    instrument = await instruments.upsert(
        symbol="EUR/USD",
        name="Euro / US Dollar",
        asset_type=AssetType.FOREX,
        exchange_id=None,
        currency="USD",
        enabled=True,
        metadata={},
    )
    indicator = await economic.upsert_indicator(
        code="US_GDP",
        name="US Gross Domestic Product",
        country="US",
        unit="usd_billions",
        frequency=IndicatorFrequency.QUARTERLY,
        provider_id=provider_a,
        provider_series_code="GDP",
        enabled=True,
        metadata={},
    )
    return World(provider_a, provider_b, instrument, indicator)


def observation(
    world: World,
    value: str,
    *,
    at: datetime = T0,
    provider: uuid.UUID | None = None,
    kind: ObservationType = ObservationType.RATE,
) -> MarketObservationIn:
    return MarketObservationIn(
        instrument_id=world.instrument,
        provider_id=provider or world.provider_a,
        observation_type=kind,
        observed_at=at,
        value=Decimal(value),
        currency="USD",
        unit="currency",
        freshness=Freshness.DELAYED,
        quote_kind=QuoteKind.INDICATIVE,
    )


def candle(world: World, close: str, *, at: datetime = T0) -> OHLCVIn:
    return OHLCVIn(
        instrument_id=world.instrument,
        provider_id=world.provider_a,
        interval=CandleInterval.DAY_1,
        timestamp=at,
        open=Decimal("1.0800"),
        high=Decimal("1.0900"),
        low=Decimal("1.0700"),
        close=Decimal(close),
        currency="USD",
    )


async def history(session: AsyncSession, world: World) -> list[tuple[uuid.UUID, Decimal]]:
    rows = await SqlMarketDataRepository(session).get_observations(
        world.instrument, ObservationType.RATE, start=FAR_PAST, end=FAR_FUTURE
    )
    return [(row.provider_id, row.value) for row in rows]


# --- Schema ------------------------------------------------------------------


async def test_models_match_the_migrated_schema(migrated_database: Settings) -> None:
    engine = create_engine(migrated_database)
    async with engine.connect() as connection:
        differences = await connection.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
        )
    await engine.dispose()

    assert differences == []


async def test_time_series_tables_are_hypertables(session: AsyncSession) -> None:
    result = await session.execute(
        sa.text("SELECT hypertable_name FROM timescaledb_information.hypertables")
    )

    assert set(result.scalars()) == {"market_observation", "ohlcv"}


async def test_constraints_have_the_names_the_models_declare(session: AsyncSession) -> None:
    """Alembic does not compare check constraints, so their names are checked here."""
    result = await session.execute(
        sa.text(
            "SELECT conname FROM pg_constraint"
            " WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
        )
    )
    declared = {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }

    assert set(result.scalars()) == declared
    assert "ck_instrument_asset_type" in declared
    assert "ck_ohlcv_interval" in declared


async def test_unknown_enumeration_value_is_rejected(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError, match="ck_instrument_asset_type"):
        await session.execute(
            sa.text("INSERT INTO instrument (symbol, name, asset_type) VALUES ('X', 'X', 'share')")
        )


# --- Market observations ------------------------------------------------------


async def test_observations_are_stored_and_returned_in_time_order(
    session: AsyncSession, world: World
) -> None:
    repository = SqlMarketDataRepository(session)
    later = observation(world, "1.0850", at=T0 + timedelta(minutes=5))
    earlier = observation(world, "1.0800")

    result = await repository.add_observations([later, earlier])

    assert (result.inserted, result.unchanged, result.conflicting) == (2, 0, 0)
    rows = await repository.get_observations(
        world.instrument, ObservationType.RATE, start=FAR_PAST, end=FAR_FUTURE
    )
    assert [row.value for row in rows] == [Decimal("1.0800"), Decimal("1.0850")]
    stored = rows[0]
    assert stored.observed_at == T0
    assert stored.ingested_at is not None
    assert (stored.currency, stored.unit) == ("USD", "currency")
    assert (stored.freshness, stored.quote_kind) == (Freshness.DELAYED, QuoteKind.INDICATIVE)
    assert stored.quality_status == "ok"
    assert stored.metadata_ is None


async def test_history_range_includes_start_and_excludes_end(
    session: AsyncSession, world: World
) -> None:
    repository = SqlMarketDataRepository(session)
    await repository.add_observations(
        [observation(world, str(i), at=T0 + timedelta(minutes=i)) for i in range(4)]
    )

    rows = await repository.get_observations(
        world.instrument,
        ObservationType.RATE,
        start=T0 + timedelta(minutes=1),
        end=T0 + timedelta(minutes=3),
    )

    assert [row.value for row in rows] == [Decimal("1"), Decimal("2")]


async def test_history_is_paginated(session: AsyncSession, world: World) -> None:
    repository = SqlMarketDataRepository(session)
    await repository.add_observations(
        [observation(world, str(i), at=T0 + timedelta(minutes=i)) for i in range(5)]
    )

    page = await repository.get_observations(
        world.instrument, ObservationType.RATE, start=FAR_PAST, end=FAR_FUTURE, limit=2, offset=2
    )

    assert [row.value for row in page] == [Decimal("2"), Decimal("3")]


async def test_reinserting_an_observation_writes_nothing(
    session: AsyncSession, world: World
) -> None:
    repository = SqlMarketDataRepository(session)
    await repository.add_observations([observation(world, "1.0800")])

    # Same value written with a different scale is still the same value.
    result = await repository.add_observations([observation(world, "1.08")])

    assert (result.inserted, result.unchanged, result.conflicting) == (0, 1, 0)
    assert await history(session, world) == [(world.provider_a, Decimal("1.0800"))]


async def test_conflicting_observation_keeps_stored_value_and_warns(
    session: AsyncSession, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    repository = SqlMarketDataRepository(session)
    await repository.add_observations([observation(world, "1.0800")])

    with caplog.at_level(logging.WARNING, logger="app.repositories.market_data"):
        result = await repository.add_observations([observation(world, "1.0900")])

    assert (result.inserted, result.unchanged, result.conflicting) == (0, 0, 1)
    assert await history(session, world) == [(world.provider_a, Decimal("1.0800"))]
    (record,) = caplog.records
    assert record.stored == {"value": "1.0800"}
    assert record.incoming == {"value": "1.0900"}


async def test_observations_from_two_providers_are_both_kept(
    session: AsyncSession, world: World
) -> None:
    repository = SqlMarketDataRepository(session)

    result = await repository.add_observations(
        [
            observation(world, "1.0800", provider=world.provider_a),
            observation(world, "1.0805", provider=world.provider_b),
        ]
    )

    assert result.inserted == 2
    assert sorted(await history(session, world)) == sorted(
        [(world.provider_a, Decimal("1.0800")), (world.provider_b, Decimal("1.0805"))]
    )


async def test_observation_types_do_not_collide(session: AsyncSession, world: World) -> None:
    repository = SqlMarketDataRepository(session)

    result = await repository.add_observations(
        [
            observation(world, "1.0800", kind=ObservationType.RATE),
            observation(world, "500", kind=ObservationType.VOLUME),
        ]
    )

    assert result.inserted == 2


async def test_duplicates_within_one_batch(session: AsyncSession, world: World) -> None:
    repository = SqlMarketDataRepository(session)

    result = await repository.add_observations(
        [observation(world, "1.0800"), observation(world, "1.0800"), observation(world, "1.0900")]
    )

    assert (result.inserted, result.unchanged, result.conflicting) == (1, 1, 1)
    assert await history(session, world) == [(world.provider_a, Decimal("1.0800"))]


async def test_large_batch_is_stored_and_deduplicated(session: AsyncSession, world: World) -> None:
    repository = SqlMarketDataRepository(session)
    batch = [observation(world, str(i), at=T0 + timedelta(minutes=i)) for i in range(2500)]

    first = await repository.add_observations(batch)
    second = await repository.add_observations(batch)

    assert first.inserted == 2500
    assert (second.inserted, second.unchanged, second.conflicting) == (0, 2500, 0)


async def test_precision_is_preserved(session: AsyncSession, world: World) -> None:
    repository = SqlMarketDataRepository(session)
    tiny = "0.000000012345678901"
    huge = "123456789012345678901234.56789"

    await repository.add_observations(
        [observation(world, tiny), observation(world, huge, at=T0 + timedelta(minutes=1))]
    )

    # Decimal equality is exact: no digit may be lost or rounded.
    assert [value for _, value in await history(session, world)] == [Decimal(tiny), Decimal(huge)]


async def test_latest_observation_per_instrument_and_provider(
    session: AsyncSession, world: World
) -> None:
    repository = SqlMarketDataRepository(session)
    await repository.add_observations(
        [
            observation(world, "1.0800", provider=world.provider_a),
            observation(world, "1.0850", provider=world.provider_a, at=T0 + timedelta(minutes=5)),
            observation(world, "1.0805", provider=world.provider_b),
        ]
    )

    latest = await repository.get_latest_observations([world.instrument], ObservationType.RATE)

    assert {(row.provider_id, row.value) for row in latest} == {
        (world.provider_a, Decimal("1.0850")),
        (world.provider_b, Decimal("1.0805")),
    }
    assert await repository.get_latest_observations([], ObservationType.RATE) == []


async def test_observation_requires_known_instrument(session: AsyncSession, world: World) -> None:
    orphan = observation(world, "1").model_copy(update={"instrument_id": uuid.uuid4()})

    with pytest.raises(IntegrityError):
        await SqlMarketDataRepository(session).add_observations([orphan])


# --- Candles -------------------------------------------------------------------


async def test_candles_are_stored_and_immutable(session: AsyncSession, world: World) -> None:
    repository = SqlMarketDataRepository(session)

    first = await repository.add_candles(
        [candle(world, "1.0850"), candle(world, "1.0870", at=T0 + timedelta(days=1))]
    )
    again = await repository.add_candles([candle(world, "1.0850")])
    changed = await repository.add_candles([candle(world, "1.0999")])

    assert first.inserted == 2
    assert (again.inserted, again.unchanged, again.conflicting) == (0, 1, 0)
    assert (changed.inserted, changed.unchanged, changed.conflicting) == (0, 0, 1)
    rows = await repository.get_candles(
        world.instrument, CandleInterval.DAY_1, start=FAR_PAST, end=FAR_FUTURE
    )
    assert [row.close for row in rows] == [Decimal("1.0850"), Decimal("1.0870")]
    assert rows[0].volume is None
    assert (
        await repository.get_candles(
            world.instrument, CandleInterval.HOUR_1, start=FAR_PAST, end=FAR_FUTURE
        )
        == []
    )


# --- Economic observations -------------------------------------------------------


def gdp(world: World, value: str, period: datetime = T0) -> EconomicObservationIn:
    return EconomicObservationIn(
        indicator_id=world.indicator, observed_at=period, value=Decimal(value)
    )


async def test_economic_revision_is_stored_alongside_the_earlier_value(
    session: AsyncSession, world: World
) -> None:
    repository = SqlEconomicDataRepository(session)

    first = await repository.add_observations([gdp(world, "29000.1")], ingested_at=T0)
    same = await repository.add_observations(
        [gdp(world, "29000.10")], ingested_at=T0 + timedelta(days=1)
    )
    revised = await repository.add_observations(
        [gdp(world, "29100.5")], ingested_at=T0 + timedelta(days=30)
    )

    assert (first.inserted, first.revised, first.unchanged) == (1, 0, 0)
    assert (same.inserted, same.revised, same.unchanged) == (0, 0, 1)
    assert (revised.inserted, revised.revised, revised.unchanged) == (0, 1, 0)

    current = await repository.get_observations(world.indicator, start=FAR_PAST, end=FAR_FUTURE)
    assert [row.value for row in current] == [Decimal("29100.5")]

    everything = await repository.get_observations(
        world.indicator, start=FAR_PAST, end=FAR_FUTURE, include_revisions=True
    )
    assert [row.value for row in everything] == [Decimal("29000.1"), Decimal("29100.5")]


async def test_economic_history_returns_current_value_per_period(
    session: AsyncSession, world: World
) -> None:
    repository = SqlEconomicDataRepository(session)
    next_period = T0 + timedelta(days=91)
    await repository.add_observations(
        [gdp(world, "100"), gdp(world, "200", next_period)], ingested_at=T0
    )
    await repository.add_observations([gdp(world, "101")], ingested_at=T0 + timedelta(days=1))

    current = await repository.get_observations(world.indicator, start=FAR_PAST, end=FAR_FUTURE)

    assert [(row.observed_at, row.value) for row in current] == [
        (T0, Decimal("101")),
        (next_period, Decimal("200")),
    ]


# --- Instruments ---------------------------------------------------------------


async def test_instrument_without_exchange_is_not_duplicated(
    session: AsyncSession, world: World
) -> None:
    repository = SqlInstrumentRepository(session)

    same_id = await repository.upsert(
        symbol="EUR/USD",
        name="Euro vs US Dollar",
        asset_type=AssetType.FOREX,
        exchange_id=None,
        currency="USD",
        enabled=False,
        metadata={"base": "EUR"},
    )

    assert same_id == world.instrument
    instrument = await repository.get_by_symbol(AssetType.FOREX, "EUR/USD")
    assert instrument is not None
    assert (instrument.name, instrument.enabled) == ("Euro vs US Dollar", False)
    assert instrument.metadata_ == {"base": "EUR"}


async def test_same_symbol_on_two_exchanges_are_two_instruments(session: AsyncSession) -> None:
    repository = SqlInstrumentRepository(session)
    exchanges = [
        await repository.upsert_exchange(code=code, name=code, country=None, timezone="UTC")
        for code in ("TESTA", "TESTB")
    ]

    ids = {
        await repository.upsert(
            symbol="ABC",
            name="ABC Corp",
            asset_type=AssetType.STOCK,
            exchange_id=exchange_id,
            currency="USD",
            enabled=True,
            metadata={},
        )
        for exchange_id in exchanges
    }

    assert len(ids) == 2
    found = await repository.get_by_symbol(AssetType.STOCK, "ABC", exchanges[1])
    assert found is not None and found.exchange_id == exchanges[1]
    assert await repository.get_by_symbol(AssetType.STOCK, "ABC") is None


async def test_instrument_listing_filters(session: AsyncSession, world: World) -> None:
    repository = SqlInstrumentRepository(session)
    await repository.upsert(
        symbol="BTC/USD",
        name="Bitcoin",
        asset_type=AssetType.CRYPTO,
        exchange_id=None,
        currency="USD",
        enabled=False,
        metadata={},
    )

    forex = await repository.list(asset_type=AssetType.FOREX)
    disabled = await repository.list(enabled=False)

    assert [i.symbol for i in forex] == ["EUR/USD"]
    assert [i.symbol for i in disabled] == ["BTC/USD"]
    assert await repository.get(world.instrument) is not None
    assert await repository.get(uuid.uuid4()) is None


async def test_mappings_exclude_disabled_instruments(session: AsyncSession, world: World) -> None:
    repository = SqlInstrumentRepository(session)
    disabled = await repository.upsert(
        symbol="GBP/USD",
        name="Pound / US Dollar",
        asset_type=AssetType.FOREX,
        exchange_id=None,
        currency="USD",
        enabled=False,
        metadata={},
    )
    for instrument_id, symbol in ((world.instrument, "EURUSD"), (disabled, "GBPUSD")):
        await repository.upsert_mapping(
            instrument_id=instrument_id, provider_id=world.provider_a, provider_symbol=symbol
        )

    active = await repository.list_mappings(world.provider_a)
    everything = await repository.list_mappings(world.provider_a, enabled_only=False)

    assert [m.provider_symbol for m in active] == ["EURUSD"]
    assert [m.provider_symbol for m in everything] == ["EURUSD", "GBPUSD"]
    assert await repository.list_mappings(world.provider_b) == []


# --- Seed ------------------------------------------------------------------------


SEED = {
    "exchanges": [
        {"code": "SEEDX", "name": "Seed Exchange", "country": "US", "timezone": "America/New_York"}
    ],
    "providers": [
        {
            "name": "seed_provider",
            "base_url": "https://seed.test",
            "configuration": {"max_retries": 5},
        }
    ],
    "instruments": [
        {
            "symbol": "SEED",
            "name": "Seed Corp",
            "asset_type": "stock",
            "exchange": "SEEDX",
            "currency": "USD",
            "providers": {"seed_provider": "SEED.X"},
        },
        {"symbol": "SEED/USD", "name": "Seed coin", "asset_type": "crypto", "currency": "USD"},
    ],
    "indicators": [
        {
            "code": "SEED_CPI",
            "name": "Seed CPI",
            "unit": "index",
            "frequency": "monthly",
            "provider": "seed_provider",
            "provider_series_code": "CPI",
        }
    ],
}


async def _apply(session: AsyncSession, data: dict) -> None:
    await apply_seed(
        SeedFile.model_validate(data),
        providers=SqlProviderRepository(session),
        instruments=SqlInstrumentRepository(session),
        economic_data=SqlEconomicDataRepository(session),
        jobs=SqlCollectionJobRepository(session),
    )
    await session.commit()


async def _count(session: AsyncSession, table: str) -> int:
    return (await session.execute(sa.text(f"SELECT count(*) FROM {table}"))).scalar_one()


async def test_seed_creates_everything_it_lists(session: AsyncSession) -> None:
    await _apply(session, SEED)

    instruments = SqlInstrumentRepository(session)
    exchange = await instruments.get_exchange_by_code("SEEDX")
    provider = await SqlProviderRepository(session).get_by_name("seed_provider")
    assert exchange is not None and provider is not None
    assert provider.configuration["max_retries"] == 5

    stock = await instruments.get_by_symbol(AssetType.STOCK, "SEED", exchange.id)
    assert stock is not None and stock.currency == "USD"
    assert await instruments.get_by_symbol(AssetType.CRYPTO, "SEED/USD") is not None
    (mapping,) = await instruments.list_mappings(provider.id)
    assert (mapping.instrument_id, mapping.provider_symbol) == (stock.id, "SEED.X")

    indicator = await SqlEconomicDataRepository(session).get_indicator_by_code("SEED_CPI")
    assert indicator is not None and indicator.provider_id == provider.id


async def test_applying_the_seed_twice_creates_no_duplicates(session: AsyncSession) -> None:
    tables = ("exchange", "provider", "instrument", "instrument_provider_mapping")
    await _apply(session, SEED)
    before = [await _count(session, table) for table in (*tables, "economic_indicator")]

    await _apply(session, SEED)

    assert [await _count(session, t) for t in (*tables, "economic_indicator")] == before


async def test_reapplying_a_changed_seed_updates_in_place(session: AsyncSession) -> None:
    await _apply(session, SEED)
    instruments = SqlInstrumentRepository(session)
    original = await instruments.get_by_symbol(AssetType.CRYPTO, "SEED/USD")
    assert original is not None

    changed = {
        **SEED,
        "instruments": [
            SEED["instruments"][0],
            {**SEED["instruments"][1], "name": "Seed coin renamed", "enabled": False},
        ],
    }
    await _apply(session, changed)

    await session.refresh(original)
    assert (original.name, original.enabled) == ("Seed coin renamed", False)
