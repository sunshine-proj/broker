"""The read API against the test database (PRD FR-006).

Each test starts from an emptied database holding a small, known dataset.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config.settings import Settings
from app.database import create_session_factory
from app.main import create_app
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

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

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
            metadata={"country": "US", "maturity": "10Y"},
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


@pytest.fixture
def client(migrated_database: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(migrated_database)) as test_client:
        yield test_client


def get(client: TestClient, path: str, **params: object) -> dict:
    response = client.get(f"/api/v1{path}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def error(client: TestClient, path: str, status: int, **params: object) -> dict:
    response = client.get(f"/api/v1{path}", params=params)
    assert response.status_code == status, response.text
    return response.json()["error"]


# --- Instruments -------------------------------------------------------------------------------


async def test_instruments_are_listed_with_their_provider_symbols(
    client: TestClient, ids: Ids
) -> None:
    page = get(client, "/instruments")

    assert [i["symbol"] for i in page["items"]] == ["BTC/USD", "EUR/USD", "US10Y", "AAPL"]
    assert (page["limit"], page["offset"], page["has_more"]) == (100, 0, False)
    eurusd = page["items"][1]
    assert eurusd["providers"] == [
        {"provider": "alpha", "provider_symbol": "EUR/USD", "enabled": True},
        {"provider": "beta", "provider_symbol": "EXR/D.USD.EUR.SP00.A", "enabled": True},
    ]
    assert page["items"][3]["exchange"] == "XNAS"


async def test_instruments_are_paginated(client: TestClient, ids: Ids) -> None:
    first = get(client, "/instruments", limit=3)
    second = get(client, "/instruments", limit=3, offset=3)

    assert (len(first["items"]), first["has_more"]) == (3, True)
    assert ([i["symbol"] for i in second["items"]], second["has_more"]) == (["AAPL"], False)


@pytest.mark.parametrize(
    ("params", "symbols"),
    [
        ({"asset_type": "forex"}, ["EUR/USD"]),
        ({"provider": "beta"}, ["EUR/USD", "US10Y"]),
        ({"search": "usd"}, ["BTC/USD", "EUR/USD"]),
        ({"search": "%"}, []),
        ({"enabled": "false"}, []),
    ],
)
async def test_instruments_can_be_filtered(
    client: TestClient, ids: Ids, params: dict, symbols: list[str]
) -> None:
    assert [i["symbol"] for i in get(client, "/instruments", **params)["items"]] == symbols


async def test_instrument_details(client: TestClient, ids: Ids) -> None:
    instrument = get(client, f"/instruments/{ids.us10y}")

    assert instrument["symbol"] == "US10Y"
    assert instrument["metadata"] == {"country": "US", "maturity": "10Y"}


async def test_unknown_instrument_is_404(client: TestClient, ids: Ids) -> None:
    assert error(client, f"/instruments/{uuid.uuid4()}", 404)["code"] == "instrument_not_found"
    assert error(client, "/instruments/not-a-uuid", 422)["code"] == "validation_error"


# --- Prices -------------------------------------------------------------------------------------


async def test_latest_prices_keep_providers_apart(client: TestClient, ids: Ids) -> None:
    items = get(client, "/prices/latest")["items"]

    summary = [(i["symbol"], i["provider"], i["observation_type"], i["value"]) for i in items]
    assert summary == [
        ("BTC/USD", "alpha", "price", "64000.5"),
        ("EUR/USD", "alpha", "rate", PRECISE),
        ("EUR/USD", "beta", "rate", "1.1052"),
        ("US10Y", "beta", "yield", "5.10"),
        ("AAPL", "alpha", "price", "352.25"),
    ]


async def test_values_are_strings_and_timestamps_are_utc(client: TestClient, ids: Ids) -> None:
    (item,) = get(client, "/prices/latest", symbol="AAPL")["items"]

    assert item["value"] == "352.25"
    assert item["observed_at"] == "2026-11-13T02:00:00Z"
    assert item["ingested_at"].endswith("Z")
    assert (item["currency"], item["unit"], item["freshness"], item["quote_kind"]) == (
        "USD",
        "currency",
        "delayed",
        "traded",
    )


async def test_latest_prices_can_be_filtered(client: TestClient, ids: Ids) -> None:
    by_type = get(client, "/prices/latest", observation_type="market_cap")["items"]
    by_asset = get(client, "/prices/latest", asset_type=["stock", "crypto"])["items"]
    by_provider = get(client, "/prices/latest", provider="beta")["items"]
    by_id = get(client, "/prices/latest", instrument_id=str(ids.us10y))["items"]

    assert [(i["symbol"], i["value"]) for i in by_type] == [("BTC/USD", "1289097514250")]
    assert [i["symbol"] for i in by_asset] == ["BTC/USD", "AAPL"]
    assert {i["provider"] for i in by_provider} == {"beta"}
    assert [i["symbol"] for i in by_id] == ["US10Y"]


async def test_unknown_provider_filter_is_rejected(client: TestClient, ids: Ids) -> None:
    body = error(client, "/prices/latest", 422, provider="nope")

    assert (body["code"], body["details"]) == ("unknown_provider", {"provider": "nope"})


async def test_price_history_is_ascending_and_bounded(client: TestClient, ids: Ids) -> None:
    page = get(
        client,
        "/prices/history",
        instrument_id=str(ids.aapl),
        start="2026-11-13T01:00:00Z",
        end="2026-11-14T00:00:00Z",
    )

    assert [(i["observed_at"], i["value"]) for i in page["items"]] == [
        ("2026-11-13T01:00:00Z", "351.25"),
        ("2026-11-13T02:00:00Z", "352.25"),
    ]


async def test_price_history_defaults_to_the_instruments_main_value(
    client: TestClient, ids: Ids
) -> None:
    window = {"start": "2026-11-01", "end": "2026-11-15"}

    forex = get(client, "/prices/history", instrument_id=str(ids.eurusd), **window)["items"]
    reference = get(
        client, "/prices/history", instrument_id=str(ids.eurusd), provider="beta", **window
    )["items"]
    yields = get(client, "/prices/history", instrument_id=str(ids.us10y), **window)["items"]

    assert [(i["provider"], i["observation_type"]) for i in forex] == [
        ("beta", "rate"),
        ("alpha", "rate"),
    ]
    assert [i["quote_kind"] for i in reference] == ["reference"]
    assert [i["observation_type"] for i in yields] == ["yield"]


async def test_price_history_pages(client: TestClient, ids: Ids) -> None:
    window = {"instrument_id": str(ids.aapl), "start": "2026-11-13", "end": "2026-11-14"}

    first = get(client, "/prices/history", limit=2, **window)
    rest = get(client, "/prices/history", limit=2, offset=2, **window)

    assert ([i["value"] for i in first["items"]], first["has_more"]) == (["350.25", "351.25"], True)
    assert ([i["value"] for i in rest["items"]], rest["has_more"]) == (["352.25"], False)


async def test_invalid_time_range_is_rejected(client: TestClient, ids: Ids) -> None:
    body = error(
        client,
        "/prices/history",
        422,
        instrument_id=str(ids.aapl),
        start="2026-11-14",
        end="2026-11-13",
    )

    assert body["code"] == "invalid_time_range"


@pytest.mark.parametrize("limit", [0, 1001])
async def test_limit_is_bounded(client: TestClient, ids: Ids, limit: int) -> None:
    assert error(client, "/instruments", 422, limit=limit)["code"] == "validation_error"


async def test_history_needs_an_instrument(client: TestClient, ids: Ids) -> None:
    assert error(client, "/prices/history", 422)["code"] == "validation_error"
    unknown = error(client, "/prices/history", 404, instrument_id=str(uuid.uuid4()))
    assert unknown["code"] == "instrument_not_found"


async def test_ohlcv(client: TestClient, ids: Ids) -> None:
    items = get(
        client, "/ohlcv", instrument_id=str(ids.aapl), start="2026-11-01", end="2026-11-14"
    )["items"]

    assert [(i["timestamp"], i["close"], i["volume"]) for i in items] == [
        ("2026-11-11T00:00:00Z", "352.5", "1000"),
        ("2026-11-12T00:00:00Z", "351.5", "1000"),
        ("2026-11-13T00:00:00Z", "350.5", None),
    ]
    assert items[0]["interval"] == "1day"
    hourly = get(client, "/ohlcv", instrument_id=str(ids.aapl), interval="1h", start="2026-11-01")
    assert hourly["items"] == []


async def test_forex_rates(client: TestClient, ids: Ids) -> None:
    items = get(client, "/forex/rates", base="eur", quote="USD")["items"]
    references = get(client, "/forex/rates", quote_kind="reference")["items"]

    assert [(i["provider"], i["base_currency"], i["quote_currency"]) for i in items] == [
        ("alpha", "EUR", "USD"),
        ("beta", "EUR", "USD"),
    ]
    assert [(i["provider"], i["value"]) for i in references] == [("beta", "1.1052")]
    assert get(client, "/forex/rates", base="GBP")["items"] == []


async def test_bond_yields(client: TestClient, ids: Ids) -> None:
    (item,) = get(client, "/bonds/yields", country="us")["items"]

    assert (item["symbol"], item["value"], item["unit"], item["currency"]) == (
        "US10Y",
        "5.10",
        "percent",
        None,
    )
    assert (item["country"], item["maturity"]) == ("US", "10Y")
    assert get(client, "/bonds/yields", country="DE")["items"] == []


# --- Indicators -------------------------------------------------------------------------------


async def test_indicators_show_the_current_value_of_the_latest_period(
    client: TestClient, ids: Ids
) -> None:
    items = get(client, "/indicators")["items"]

    by_code = {i["code"]: i for i in items}
    assert list(by_code) == ["EA_GDP", "US_CPI"]
    assert by_code["EA_GDP"]["latest"] is None
    latest = by_code["US_CPI"]["latest"]
    assert (latest["observed_at"], latest["value"]) == ("2026-10-01T00:00:00Z", "337.5")
    assert by_code["US_CPI"]["provider"] == "beta"
    assert [i["code"] for i in get(client, "/indicators", country="US")["items"]] == ["US_CPI"]
    assert [i["code"] for i in get(client, "/indicators", frequency="quarterly")["items"]] == [
        "EA_GDP"
    ]


async def test_indicator_history_with_and_without_revisions(client: TestClient, ids: Ids) -> None:
    window = {"start": "2026-01-01", "end": "2026-11-16"}

    current = get(client, "/indicators/US_CPI/observations", **window)["items"]
    everything = get(client, "/indicators/US_CPI/observations", include_revisions="true", **window)[
        "items"
    ]

    assert [(i["observed_at"][:7], i["value"]) for i in current] == [
        ("2026-09", "336.3"),
        ("2026-10", "337.5"),
    ]
    assert [(i["observed_at"][:7], i["value"]) for i in everything] == [
        ("2026-09", "336.3"),
        ("2026-10", "337.4"),
        ("2026-10", "337.5"),
    ]


async def test_unknown_indicator_is_404(client: TestClient, ids: Ids) -> None:
    assert error(client, "/indicators/NOPE/observations", 404)["code"] == "indicator_not_found"


# --- Providers and collection ---------------------------------------------------------------------


async def test_providers_show_limits_and_health_but_no_credentials(
    client: TestClient, ids: Ids
) -> None:
    response = client.get("/api/v1/providers")

    items = response.json()["items"]
    alpha, beta = items
    assert alpha["rate_limits"] == [{"limit": 8, "period": "minute"}]
    assert (alpha["health"], alpha["consecutive_failures"]) == ("degraded", 1)
    assert alpha["last_error"] == "ProviderUnavailableError: request timed out"
    assert (beta["health"], beta["consecutive_failures"], beta["rate_limits"]) == (None, 0, [])
    assert "ALPHA_API_KEY" not in response.text
    assert "auth" not in alpha


async def test_collector_status(client: TestClient, ids: Ids) -> None:
    (job,) = get(client, "/collectors/status")["items"]

    assert (job["name"], job["provider"], job["kind"], job["interval_minutes"]) == (
        "alpha_quotes",
        "alpha",
        "quotes",
        15,
    )
    assert (job["last_status"], job["next_run_at"]) == ("success", "2026-11-16T12:15:00Z")
    assert get(client, "/collectors/status", provider="beta")["items"] == []


async def test_collector_executions_newest_first(client: TestClient, ids: Ids) -> None:
    items = get(client, "/collectors/executions", job="alpha_quotes")["items"]
    failed = get(client, "/collectors/executions", status="failed")["items"]

    assert [(i["status"], i["records_written"]) for i in items] == [("success", 3), ("failed", 0)]
    assert items[0]["provider"] == "alpha"
    assert [i["error"] for i in failed] == ["1 of 1 provider calls failed: timeout"]


async def test_documentation_lists_every_endpoint(client: TestClient, ids: Ids) -> None:
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]

    assert {
        "/api/v1/health",
        "/api/v1/instruments",
        "/api/v1/instruments/{instrument_id}",
        "/api/v1/prices/latest",
        "/api/v1/prices/history",
        "/api/v1/ohlcv",
        "/api/v1/forex/rates",
        "/api/v1/bonds/yields",
        "/api/v1/indicators",
        "/api/v1/indicators/{code}/observations",
        "/api/v1/providers",
        "/api/v1/collectors/status",
        "/api/v1/collectors/executions",
        "/api/v1/collectors/{job}/run",
    } <= set(paths)
