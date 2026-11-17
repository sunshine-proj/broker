"""Analytics, watchlists, groups and portfolios against the test database
(PRD FR-014 to FR-016).

The views date their latest values relative to now(), so this dataset is laid
out in days before today (UTC midnight) rather than on fixed dates; every
expected value is computed from the same offsets. The data is committed and
the daily continuous aggregate refreshed, as in production.
"""

import math
import statistics
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config.settings import Settings
from app.database import create_session_factory
from app.main import create_app
from app.models.enums import CandleInterval, Freshness, ObservationType, QuoteKind
from app.repositories.collection_jobs import SqlCollectionJobRepository
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository
from app.repositories.lists import SqlListRepository
from app.repositories.market_data import SqlMarketDataRepository
from app.repositories.portfolios import SqlPortfolioRepository
from app.repositories.providers import SqlProviderRepository
from app.schemas.market_data import MarketObservationIn, OHLCVIn
from app.schemas.seed import SeedFile
from app.services.portfolio import PortfolioFile, apply_portfolios
from app.services.seed import apply_seed
from tests.conftest import refresh_daily_observation

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TODAY = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
# STK: one daily candle a day for 260 days, closing at 100, 101, ... 359 today.
STK_DAYS = 260
EURUSD = Decimal("1.10")
EURGBP = Decimal("0.85")

SEED = {
    "providers": [
        {"name": "alpha", "base_url": "https://alpha.test"},
        {"name": "ecb", "base_url": "https://ecb.test"},
        {"name": "beta", "base_url": "https://beta.test"},
    ],
    "instruments": [
        {
            "symbol": "STK",
            "name": "Stock",
            "asset_type": "stock",
            "currency": "USD",
            "providers": {"alpha": "STK"},
        },
        {
            "symbol": "FND",
            "name": "Fund",
            "asset_type": "etf",
            "currency": "USD",
            "providers": {"alpha": "FND"},
        },
        {
            "symbol": "COIN/USD",
            "name": "Coin",
            "asset_type": "crypto",
            "currency": "USD",
            "metadata": {"base": "COIN", "quote": "USD"},
            "providers": {"alpha": "coin"},
        },
        {
            "symbol": "EUR/USD",
            "name": "Euro / US Dollar",
            "asset_type": "forex",
            "currency": "USD",
            "metadata": {"base": "EUR", "quote": "USD"},
            "providers": {"ecb": "USD", "alpha": "EUR/USD"},
        },
        {
            "symbol": "EUR/GBP",
            "name": "Euro / Pound",
            "asset_type": "forex",
            "currency": "GBP",
            "metadata": {"base": "EUR", "quote": "GBP"},
            "providers": {"ecb": "GBP"},
        },
        *(
            {
                "symbol": symbol,
                "name": symbol,
                "asset_type": "government_bond_yield",
                "metadata": {"country": "US", "maturity": maturity, "curve": "US Treasury"},
                "providers": {"beta": symbol},
            }
            for symbol, maturity in (("US2Y", "2Y"), ("US10Y", "10Y"))
        ),
    ],
    "watchlists": [{"name": "mine", "title": "Mine", "instruments": ["COIN/USD", "STK"]}],
    "groups": [{"name": "pair", "title": "Pair", "instruments": ["STK", "COIN/USD"]}],
    "spreads": [
        {
            "name": "us_10y_2y",
            "title": "10y - 2y",
            "long": {"symbol": "US10Y", "provider": "beta"},
            "short": {"symbol": "US2Y", "provider": "beta"},
        }
    ],
}


def day(offset: int) -> datetime:
    """`offset` days before today."""
    return TODAY - timedelta(days=offset)


def stk_close(offset: int) -> Decimal:
    return Decimal(100 + STK_DAYS - 1 - offset)


def us2y(offset: int) -> Decimal:
    return Decimal("4.00") - Decimal("0.01") * offset


def us10y(offset: int) -> Decimal:
    return Decimal("4.50") - Decimal("0.02") * offset


@dataclass
class Data:
    ids: dict[str, uuid.UUID]
    providers: dict[str, uuid.UUID]


@pytest.fixture
async def data(empty_database: AsyncEngine) -> AsyncIterator[Data]:
    async with create_session_factory(empty_database)() as session:
        await apply_seed(
            SeedFile.model_validate(SEED),
            providers=SqlProviderRepository(session),
            instruments=SqlInstrumentRepository(session),
            economic_data=SqlEconomicDataRepository(session),
            jobs=SqlCollectionJobRepository(session),
            lists=SqlListRepository(session),
        )
        ids = {
            row.symbol: row.id
            for row in await session.execute(sa.text("SELECT symbol, id FROM instrument"))
        }
        providers = {
            row.name: row.id
            for row in await session.execute(sa.text("SELECT name, id FROM provider"))
        }

        def obs(
            symbol: str, provider: str, kind: ObservationType, at: datetime, value: Decimal
        ) -> MarketObservationIn:
            percent = kind is ObservationType.YIELD
            return MarketObservationIn(
                instrument_id=ids[symbol],
                provider_id=providers[provider],
                observation_type=kind,
                observed_at=at,
                value=value,
                currency=None if percent else ("GBP" if symbol == "EUR/GBP" else "USD"),
                unit="percent" if percent else "currency",
                freshness=Freshness.END_OF_DAY,
                quote_kind=QuoteKind.REFERENCE,
            )

        def candle(symbol: str, at: datetime, close: Decimal) -> OHLCVIn:
            return OHLCVIn(
                instrument_id=ids[symbol],
                provider_id=providers["alpha"],
                interval=CandleInterval.DAY_1,
                timestamp=at,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=None,
                currency="USD",
            )

        market = SqlMarketDataRepository(session)
        await market.add_candles(
            [candle("STK", day(offset), stk_close(offset)) for offset in range(STK_DAYS)]
            # FND: candles close at 50; its NAV, on the same days, is 49.
            + [candle("FND", day(offset), Decimal(50)) for offset in range(5)]
        )
        observations = [
            obs("FND", "alpha", ObservationType.NAV, day(offset) + timedelta(hours=21), Decimal(49))
            for offset in range(5)
        ]
        # COIN: two prices a day; the later one closes the day. 10, 11, ... 19 today.
        for offset in range(10):
            at = day(offset)
            observations += [
                obs(
                    "COIN/USD", "alpha", ObservationType.PRICE, at + timedelta(hours=8), Decimal(1)
                ),
                obs(
                    "COIN/USD",
                    "alpha",
                    ObservationType.PRICE,
                    at + timedelta(hours=20),
                    Decimal(19 - offset),
                ),
            ]
        # Exchange rates for the last 30 days. The ECB's are preferred to alpha's.
        for offset in range(30):
            at = day(offset) + timedelta(hours=15)
            observations += [
                obs("EUR/USD", "ecb", ObservationType.RATE, at, EURUSD),
                obs("EUR/USD", "alpha", ObservationType.RATE, at, Decimal("1.20")),
                obs("EUR/GBP", "ecb", ObservationType.RATE, at, EURGBP),
            ]
        # Yields for the last 40 days.
        for offset in range(40):
            observations += [
                obs("US2Y", "beta", ObservationType.YIELD, day(offset), us2y(offset)),
                obs("US10Y", "beta", ObservationType.YIELD, day(offset), us10y(offset)),
            ]
        await market.add_observations(observations)

        await apply_portfolios(
            SqlPortfolioRepository(session),
            PortfolioFile.model_validate(
                {
                    "portfolios": [
                        {
                            "name": "main",
                            "base_currency": "EUR",
                            "holdings": [
                                {
                                    "symbol": "STK",
                                    "provider": "alpha",
                                    "quantity": "2",
                                    "cost_basis": "550",
                                    "cost_currency": "USD",
                                    "acquired_on": (TODAY - timedelta(days=10)).date(),
                                },
                                {
                                    "symbol": "COIN/USD",
                                    "provider": "alpha",
                                    "quantity": "10",
                                    "cost_basis": "100",
                                },
                            ],
                        }
                    ]
                }
            ),
        )
        await session.commit()
    await refresh_daily_observation(empty_database)
    yield Data(ids, providers)


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


def close_to(value: str | None, expected: float | Decimal, places: int = 12) -> bool:
    assert value is not None
    return round(Decimal(value) - Decimal(str(expected)), places) == 0


def returns(item: dict) -> dict[str, dict]:
    return {r["horizon"]: r for r in item["returns"]}


# --- Daily closes ----------------------------------------------------------------------------


async def test_daily_closes_come_from_candles_else_the_days_last_observation(
    data: Data, empty_database: AsyncEngine
) -> None:
    async with empty_database.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT symbol, observation_type, day, close, source FROM v_daily_close"
                    " WHERE symbol IN ('FND', 'COIN/USD') AND day = :today"
                    " ORDER BY symbol, observation_type"
                ),
                {"today": TODAY},
            )
        ).all()

    assert [tuple(row) for row in rows] == [
        ("COIN/USD", "price", TODAY, Decimal(19), "observations"),
        # The candle closes the fund's price; its NAV stays a series of its own.
        ("FND", "nav", TODAY, Decimal(49), "observations"),
        ("FND", "price", TODAY, Decimal(50), "candle"),
    ]


async def test_daily_endpoint_gives_closes_with_metrics_and_conversion(
    client: TestClient, data: Data
) -> None:
    page = get(
        client,
        "/analytics/daily",
        instrument_id=data.ids["STK"],
        start=day(4).isoformat(),
        currency="eur",
    )

    assert [item["close"] for item in page["items"]] == [
        str(stk_close(o)) for o in range(4, -1, -1)
    ]
    latest = page["items"][-1]
    assert latest["day"] == TODAY.isoformat().replace("+00:00", "Z")
    assert latest["source"] == "candle"
    assert close_to(latest["daily_return"], stk_close(0) / stk_close(1) - 1)
    assert close_to(latest["converted"]["close"], stk_close(0) / EURUSD)
    assert latest["converted"]["currency"] == "EUR"


# --- Performance -----------------------------------------------------------------------------


async def test_performance_gives_returns_averages_and_volatility(
    client: TestClient, data: Data
) -> None:
    [item] = get(client, "/analytics/performance", symbol="STK")["items"]

    assert (item["close"], item["measured_in"], item["provider"]) == ("359", "return", "alpha")
    horizons = returns(item)
    assert list(horizons) == ["1w", "1m", "3m", "ytd", "1y"]
    assert horizons["1w"]["past_close"] == str(stk_close(7))
    assert close_to(horizons["1w"]["value"], stk_close(0) / stk_close(7) - 1)
    # A year ago is before the first close: nothing to compare with.
    assert horizons["1y"]["value"] is None

    assert close_to(item["sma_20"], statistics.mean(stk_close(o) for o in range(20)))
    assert close_to(item["sma_200"], statistics.mean(stk_close(o) for o in range(200)))
    steps = [math.log(float(stk_close(o) / stk_close(o + 1))) for o in range(20)]
    assert close_to(item["volatility_20d"], statistics.stdev(steps) * math.sqrt(252), places=9)


async def test_values_in_percent_change_in_points(client: TestClient, data: Data) -> None:
    [item] = get(client, "/analytics/performance", symbol="US10Y")["items"]

    assert item["measured_in"] == "points"
    assert item["daily_return"] is None
    assert close_to(returns(item)["1w"]["value"], us10y(0) - us10y(7))
    # Volatility of daily changes in points: constant here.
    assert close_to(item["volatility_20d"], 0)


async def test_each_provider_is_its_own_series(client: TestClient, data: Data) -> None:
    items = get(client, "/analytics/performance", symbol="EUR/USD")["items"]

    assert [(i["provider"], i["close"]) for i in items] == [("alpha", "1.20"), ("ecb", "1.10")]


async def test_performance_of_a_list_is_in_list_order(client: TestClient, data: Data) -> None:
    items = get(client, "/analytics/performance", list="mine")["items"]

    assert [i["symbol"] for i in items] == ["COIN/USD", "STK"]
    assert error(client, "/analytics/performance", 404, list="nope")["code"] == "list_not_found"


async def test_cross_rates_go_through_eur(data: Data, empty_database: AsyncEngine) -> None:
    async with empty_database.connect() as connection:
        rate, inverse, none = (
            await connection.execute(
                sa.text(
                    "SELECT fx_rate('GBP', 'USD', :at), fx_rate('USD', 'EUR', :at),"
                    " fx_rate('JPY', 'EUR', :at)"
                ),
                {"at": TODAY},
            )
        ).one()

    assert rate == pytest.approx(EURUSD / EURGBP)
    assert inverse == pytest.approx(1 / EURUSD)
    # No rate: no value, never an estimate.
    assert none is None


# --- Period returns and relative performance --------------------------------------------------


async def test_monthly_returns_compare_each_months_last_close(
    client: TestClient, data: Data
) -> None:
    items = get(client, "/analytics/returns", instrument_id=data.ids["STK"], period="month")[
        "items"
    ]

    last_close: dict[date, Decimal] = {}
    for offset in range(STK_DAYS - 1, -1, -1):
        last_close[day(offset).date().replace(day=1)] = stk_close(offset)
    months = sorted(last_close)
    assert [i["period_start"][:10] for i in items] == [m.isoformat() for m in months]
    assert items[0]["return"] is None
    assert close_to(items[-1]["return"], last_close[months[-1]] / last_close[months[-2]] - 1)


async def test_relative_performance_is_rebased_to_100(client: TestClient, data: Data) -> None:
    page = get(
        client,
        "/analytics/relative",
        instrument_id=[data.ids["STK"]],
        list="pair",
        start=day(5).isoformat(),
    )

    stk = [i for i in page["items"] if i["symbol"] == "STK"]
    coin = [i for i in page["items"] if i["symbol"] == "COIN/USD"]
    assert close_to(stk[0]["rebased"], 100) and close_to(coin[0]["rebased"], 100)
    assert close_to(stk[-1]["rebased"], stk_close(0) / stk_close(5) * 100)
    assert close_to(coin[-1]["rebased"], Decimal(19) / Decimal(14) * 100)
    assert error(client, "/analytics/relative", 422)["code"] == "nothing_to_compare"


# --- Spreads and yield curves -----------------------------------------------------------------


async def test_spreads_are_long_minus_short(client: TestClient, data: Data) -> None:
    [spread] = get(client, "/analytics/spreads")["items"]
    history = get(client, "/analytics/spreads/us_10y_2y", start=day(2).isoformat())["items"]

    assert spread["long"] == {"symbol": "US10Y", "provider": "beta"}
    assert close_to(spread["spread"], us10y(0) - us2y(0))
    assert close_to(spread["spread_bp"], (us10y(0) - us2y(0)) * 100)
    assert [h["spread"] for h in history] == [str(us10y(o) - us2y(o)) for o in (2, 1, 0)]
    assert error(client, "/analytics/spreads/nope", 404)["code"] == "spread_not_found"


async def test_yield_curve_by_maturity_with_earlier_curves(client: TestClient, data: Data) -> None:
    items = get(client, "/analytics/yield-curves", curve="US Treasury")["items"]

    assert [(i["maturity"], i["maturity_months"]) for i in items] == [("2Y", 24), ("10Y", 120)]
    assert close_to(items[1]["latest"], us10y(0))
    assert close_to(items[1]["week_ago"], us10y(7))
    assert close_to(items[1]["month_ago"], us10y(_month().days))
    # Forty days of data: no curve a year ago.
    assert items[1]["year_ago"] is None


def _month() -> timedelta:
    """The distance to the same day a month earlier, as PostgreSQL counts it."""
    previous = TODAY.month - 1 or 12
    year = TODAY.year - (1 if TODAY.month == 1 else 0)
    for last in (TODAY.day, 30, 29, 28):
        try:
            return TODAY - TODAY.replace(year=year, month=previous, day=last)
        except ValueError:
            continue
    raise AssertionError


# --- Watchlists and groups ------------------------------------------------------------------


async def test_watchlists_keep_their_order(client: TestClient, data: Data) -> None:
    [watchlist] = get(client, "/watchlists")["items"]

    assert watchlist["name"] == "mine"
    assert [(m["position"], m["symbol"]) for m in watchlist["members"]] == [
        (1, "COIN/USD"),
        (2, "STK"),
    ]
    assert get(client, "/watchlists/mine") == watchlist
    assert error(client, "/watchlists/pair", 404)["code"] == "watchlist_not_found"


async def test_group_performance_is_equal_weighted(client: TestClient, data: Data) -> None:
    group = get(client, "/groups/pair")
    week = {p["horizon"]: p for p in group["performance"]}["1w"]

    stk = stk_close(0) / stk_close(7) - 1
    coin = Decimal(19) / Decimal(12) - 1
    assert close_to(week["value"], (stk + coin) / 2)
    assert (week["members_with_value"], week["members"]) == (2, 2)
    assert get(client, "/groups")["items"] == [group]


async def test_a_list_removed_from_the_seed_is_disabled(
    client: TestClient, data: Data, empty_database: AsyncEngine
) -> None:
    async with create_session_factory(empty_database)() as session:
        await apply_seed(
            SeedFile.model_validate({**SEED, "watchlists": []}),
            providers=SqlProviderRepository(session),
            instruments=SqlInstrumentRepository(session),
            economic_data=SqlEconomicDataRepository(session),
            jobs=SqlCollectionJobRepository(session),
            lists=SqlListRepository(session),
        )
        await session.commit()

    assert get(client, "/watchlists")["items"] == []
    assert get(client, "/groups")["items"][0]["name"] == "pair"


# --- Portfolios -----------------------------------------------------------------------------


async def test_portfolio_is_valued_in_its_base_currency(client: TestClient, data: Data) -> None:
    portfolio = get(client, "/portfolios/main")
    positions = {p["symbol"]: p for p in portfolio["positions"]}

    stk_value = 2 * stk_close(0) / EURUSD
    coin_value = 10 * Decimal(19) / EURUSD
    assert close_to(positions["STK"]["value"], stk_value)
    assert close_to(positions["STK"]["value_local"], 2 * stk_close(0))
    assert positions["STK"]["price_currency"] == "USD"
    assert close_to(positions["STK"]["cost"], Decimal(550) / EURUSD)
    # Cost given in the base currency; valued from its first close.
    assert close_to(positions["COIN/USD"]["cost"], 100)
    assert positions["COIN/USD"]["valued_since"] == day(9).isoformat().replace("+00:00", "Z")
    assert close_to(positions["STK"]["weight"], stk_value / (stk_value + coin_value))
    assert (portfolio["holdings"], portfolio["valued_holdings"]) == (2, 2)
    assert close_to(portfolio["value"], stk_value + coin_value)
    total_cost = Decimal(550) / EURUSD + 100
    assert close_to(portfolio["profit"], stk_value + coin_value - total_cost)
    assert get(client, "/portfolios")["items"][0]["name"] == "main"
    assert error(client, "/portfolios/nope", 404)["code"] == "portfolio_not_found"


async def test_portfolio_history_counts_holdings_from_their_start(
    client: TestClient, data: Data
) -> None:
    history = get(client, "/portfolios/main/history", start=day(12).isoformat())["items"]

    by_day = {h["day"][:10]: h for h in history}
    # Nothing before the first holding starts: STK is held from 10 days ago,
    # COIN from its first close 9 days ago.
    assert [h["day"][:10] for h in history] == [
        day(o).date().isoformat() for o in range(10, -1, -1)
    ]
    assert by_day[day(10).date().isoformat()]["holdings"] == 1
    assert close_to(by_day[day(10).date().isoformat()]["value"], 2 * stk_close(10) / EURUSD)
    today = by_day[TODAY.date().isoformat()]
    assert (today["holdings"], today["valued_holdings"]) == (2, 2)
    assert close_to(today["value"], (2 * stk_close(0) + 10 * Decimal(19)) / EURUSD)
