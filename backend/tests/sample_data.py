"""A small, known dataset in the test database, for tests that read data back.

Registered as the `ids` fixture in conftest.py. It needs `empty_database`, so
it commits for real and is cleared after each test.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.database import create_session_factory
from app.models import CollectionJob, CollectorExecution
from app.models.enums import (
    AssetType,
    CandleInterval,
    CollectorStatus,
    ExecutionTrigger,
    Freshness,
    IndicatorFrequency,
    JobKind,
    ObservationType,
    QuoteKind,
)
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository
from app.repositories.market_data import SqlMarketDataRepository
from app.repositories.provider_health import SqlProviderHealthRepository
from app.repositories.providers import SqlProviderRepository
from app.schemas.market_data import EconomicObservationIn, MarketObservationIn, OHLCVIn

DAY = datetime(2026, 11, 13, tzinfo=UTC)
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
PRECISE = "1.10520000000000000001"


@dataclass
class Ids:
    aapl: uuid.UUID
    eurusd: uuid.UUID
    us10y: uuid.UUID
    btc: uuid.UUID


@pytest.fixture
async def ids(empty_database: AsyncEngine) -> AsyncIterator[Ids]:
    async with create_session_factory(empty_database)() as session:
        providers = SqlProviderRepository(session)
        alpha = await providers.upsert(
            name="alpha",
            base_url="https://alpha.test",
            enabled=True,
            configuration={
                "rate_limits": [{"limit": 8, "period": "minute"}],
                "auth": {"type": "query", "name": "apikey", "env": "ALPHA_API_KEY"},
            },
        )
        beta = await providers.upsert(
            name="beta", base_url="https://beta.test", enabled=True, configuration={}
        )
        instruments = SqlInstrumentRepository(session)
        exchange = await instruments.upsert_exchange(
            code="XNAS", name="Nasdaq", country="US", timezone="America/New_York"
        )

        async def instrument(symbol: str, asset_type: AssetType, **fields: object) -> uuid.UUID:
            values = {"exchange_id": None, "currency": None, "metadata": {}, **fields}
            return await instruments.upsert(
                symbol=symbol, name=f"{symbol} name", asset_type=asset_type, enabled=True, **values
            )

        aapl = await instrument("AAPL", AssetType.STOCK, exchange_id=exchange, currency="USD")
        eurusd = await instrument("EUR/USD", AssetType.FOREX, currency="USD")
        us10y = await instrument(
            "US10Y",
            AssetType.GOVERNMENT_BOND_YIELD,
            metadata={"country": "US", "maturity": "10Y", "curve": "US Treasury"},
        )
        btc = await instrument("BTC/USD", AssetType.CRYPTO, currency="USD")
        for instrument_id, provider_id, symbol in (
            (aapl, alpha, "AAPL"),
            (eurusd, alpha, "EUR/USD"),
            (eurusd, beta, "EXR/D.USD.EUR.SP00.A"),
            (us10y, beta, "DGS10"),
            (btc, alpha, "bitcoin/usd"),
        ):
            await instruments.upsert_mapping(
                instrument_id=instrument_id, provider_id=provider_id, provider_symbol=symbol
            )

        def obs(
            instrument_id: uuid.UUID,
            provider_id: uuid.UUID,
            kind: ObservationType,
            at: datetime,
            value: str,
            **fields: object,
        ) -> MarketObservationIn:
            values = {
                "currency": "USD",
                "unit": "currency",
                "freshness": Freshness.DELAYED,
                "quote_kind": QuoteKind.TRADED,
                **fields,
            }
            return MarketObservationIn(
                instrument_id=instrument_id,
                provider_id=provider_id,
                observation_type=kind,
                observed_at=at,
                value=Decimal(value),
                **values,
            )

        await SqlMarketDataRepository(session).add_observations(
            [
                obs(aapl, alpha, ObservationType.PRICE, DAY + timedelta(hours=h), f"35{h}.25")
                for h in range(3)
            ]
            + [
                obs(
                    eurusd,
                    alpha,
                    ObservationType.RATE,
                    DAY + timedelta(hours=14),
                    PRECISE,
                    quote_kind=QuoteKind.INDICATIVE,
                ),
                obs(
                    eurusd,
                    beta,
                    ObservationType.RATE,
                    DAY,
                    "1.1052",
                    quote_kind=QuoteKind.REFERENCE,
                    freshness=Freshness.END_OF_DAY,
                ),
                obs(
                    us10y,
                    beta,
                    ObservationType.YIELD,
                    DAY,
                    "5.10",
                    currency=None,
                    unit="percent",
                    quote_kind=QuoteKind.REFERENCE,
                ),
                obs(btc, alpha, ObservationType.PRICE, DAY, "64000.5"),
                obs(btc, alpha, ObservationType.MARKET_CAP, DAY, "1289097514250"),
            ]
        )
        await SqlMarketDataRepository(session).add_candles(
            [
                OHLCVIn(
                    instrument_id=aapl,
                    provider_id=alpha,
                    interval=CandleInterval.DAY_1,
                    timestamp=DAY - timedelta(days=d),
                    open=Decimal("350"),
                    high=Decimal("360"),
                    low=Decimal("340"),
                    close=Decimal(f"35{d}.5"),
                    volume=Decimal("1000") if d else None,
                    currency="USD",
                )
                for d in range(3)
            ]
        )

        economic = SqlEconomicDataRepository(session)
        cpi = await economic.upsert_indicator(
            code="US_CPI",
            name="US CPI",
            country="US",
            unit="index",
            frequency=IndicatorFrequency.MONTHLY,
            provider_id=beta,
            provider_series_code="CPIAUCSL",
            enabled=True,
            metadata={},
        )
        await economic.upsert_indicator(
            code="EA_GDP",
            name="Euro area GDP",
            country=None,
            unit="percent",
            frequency=IndicatorFrequency.QUARTERLY,
            provider_id=beta,
            provider_series_code="MNA/Q",
            enabled=True,
            metadata={},
        )
        september, october = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
        await economic.add_observations(
            [
                EconomicObservationIn(
                    indicator_id=cpi, observed_at=september, value=Decimal("336.3")
                ),
                EconomicObservationIn(
                    indicator_id=cpi, observed_at=october, value=Decimal("337.4")
                ),
            ],
            ingested_at=NOW - timedelta(days=30),
        )
        await economic.add_observations(
            [EconomicObservationIn(indicator_id=cpi, observed_at=october, value=Decimal("337.5"))],
            ingested_at=NOW,
        )

        await SqlProviderHealthRepository(session).record_failure(
            alpha, NOW, "ProviderUnavailableError: request timed out", down_after=3
        )
        session.add(
            CollectionJob(
                name="alpha_quotes",
                provider_id=alpha,
                kind=JobKind.QUOTES,
                interval_minutes=15,
                settings={"asset_types": ["stock"]},
                last_status=CollectorStatus.SUCCESS,
                next_run_at=NOW + timedelta(minutes=15),
            )
        )
        session.add_all(
            [
                CollectorExecution(
                    job_name="alpha_quotes",
                    provider_id=alpha,
                    trigger=ExecutionTrigger.SCHEDULED,
                    status=status,
                    started_at=NOW - timedelta(minutes=minutes),
                    finished_at=NOW - timedelta(minutes=minutes - 1),
                    records_written=written,
                    error=error,
                )
                for minutes, status, written, error in (
                    (30, CollectorStatus.FAILED, 0, "1 of 1 provider calls failed: timeout"),
                    (15, CollectorStatus.SUCCESS, 3, None),
                )
            ]
        )
        await session.commit()
    yield Ids(aapl, eurusd, us10y, btc)
