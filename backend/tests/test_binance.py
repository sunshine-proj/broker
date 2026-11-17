"""Binance adapter, against responses recorded from the live public API.

The fixtures under tests/fixtures/binance/ are unmodified responses from
data-api.binance.vision (recorded 2026-11-17). No test calls the network.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.config.settings import Settings
from app.models.enums import (
    AssetType,
    CandleInterval,
    Freshness,
    JobKind,
    ObservationType,
    QuoteKind,
)
from app.providers.base import Capability
from app.providers.binance import BinanceProvider
from app.providers.errors import ProviderConfigurationError, ProviderResponseError
from app.providers.factory import ADAPTERS
from app.services.seed import load_seed_file
from tests.providers_support import make_client

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "binance"
# When klines_1m.json was recorded: its last candle was still forming.
RECORDED_1M = datetime(2026, 11, 17, 6, 34, 42, tzinfo=UTC)
RECORDED_1D = datetime(2026, 11, 17, 6, 44, 17, tzinfo=UTC)


def recorded(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=(FIXTURES / name).read_bytes())


class Api:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def params(self, index: int = 0) -> dict[str, str]:
        return dict(self.requests[index].url.params)


def provider(api: Api, now: datetime = RECORDED_1M) -> BinanceProvider:
    return BinanceProvider(
        make_client(api, rate_limits=[{"limit": 100, "period": "minute"}]), clock=lambda: now
    )


def ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, UTC)


# --- Quotes -------------------------------------------------------------------------------


async def test_last_prices_are_real_time_traded_prices_in_the_quote_asset() -> None:
    api = Api(recorded("ticker_24hr.json"))

    quotes = await provider(api).fetch_quotes(["BTC/USDT", "ETH/USDT"], {ObservationType.PRICE})

    assert [(q.provider_symbol, q.value) for q in quotes] == [
        ("BTC/USDT", Decimal("64373.44000000")),
        ("ETH/USDT", Decimal("1818.27000000")),
    ]
    for quote in quotes:
        assert quote.observation_type is ObservationType.PRICE
        assert (quote.currency, quote.unit) == ("USDT", "currency")
        assert (quote.freshness, quote.quote_kind) == (Freshness.REAL_TIME, QuoteKind.TRADED)
        assert quote.observed_at == ms(1794897279013)
    # Pairs go to Binance in its own spelling, as a compact JSON array.
    assert json.loads(api.params()["symbols"]) == ["BTCUSDT", "ETHUSDT"]
    assert " " not in api.params()["symbols"]


async def test_volume_is_the_24_hour_turnover_in_the_quote_asset() -> None:
    api = Api(recorded("ticker_24hr.json"))

    quotes = await provider(api).fetch_quotes(["BTC/USDT"], {ObservationType.VOLUME})

    (volume,) = [q for q in quotes if q.provider_symbol == "BTC/USDT"]
    assert volume.observation_type is ObservationType.VOLUME
    assert (volume.value, volume.currency) == (Decimal("1494309328.73016410"), "USDT")
    assert volume.metadata == {"window": "24h", "field": "quoteVolume"}


async def test_unrequested_types_need_no_request() -> None:
    api = Api(recorded("ticker_24hr.json"))

    assert await provider(api).fetch_quotes(["BTC/USDT"], {ObservationType.MARKET_CAP}) == []
    assert api.requests == []


async def test_pairs_are_sent_twenty_at_a_time() -> None:
    api = Api(httpx.Response(200, json=[]))
    pairs = [f"C{i}/USDT" for i in range(25)]

    await provider(api).fetch_quotes(pairs)

    assert [len(json.loads(r.url.params["symbols"])) for r in api.requests] == [20, 5]


@pytest.mark.parametrize("symbol", ["BTCUSDT", "BTC/", "BTC-X/USDT"])
async def test_symbols_must_be_pairs(symbol: str) -> None:
    with pytest.raises(ProviderConfigurationError, match="BASE/QUOTE"):
        await provider(Api()).fetch_quotes([symbol])


async def test_unknown_symbol_is_named() -> None:
    api = Api(recorded("unknown_symbol.json", 400))

    with pytest.raises(ProviderResponseError, match="does not list a requested symbol"):
        await provider(api).fetch_quotes(["NOPE/USDT"])


@pytest.mark.parametrize(
    ("status", "message"),
    [(451, "from this location"), (418, "IP address banned"), (403, "firewall")],
)
async def test_binance_refusals_are_explained(status: int, message: str) -> None:
    api = Api(httpx.Response(status, json={"code": 0, "msg": "refused"}))

    with pytest.raises(ProviderResponseError, match=message):
        await provider(api).fetch_quotes(["BTC/USDT"])


async def test_malformed_ticker_is_a_response_error() -> None:
    api = Api(httpx.Response(200, json=[{"symbol": "BTCUSDT", "lastPrice": 1.5}]))

    with pytest.raises(ProviderResponseError):
        await provider(api).fetch_quotes(["BTC/USDT"])


# --- Candles ------------------------------------------------------------------------------


async def test_minute_candles_skip_the_one_still_forming() -> None:
    api = Api(recorded("klines_1m.json"))
    start, end = datetime(2026, 11, 17, 6, 30, tzinfo=UTC), datetime(2026, 11, 17, 7, tzinfo=UTC)

    candles = await provider(api).fetch_candles("BTC/USDT", CandleInterval.MIN_1, start, end)

    assert [c.timestamp for c in candles] == [ms(1794897120000), ms(1794897180000)]
    first = candles[0]
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("64500.27000000"),
        Decimal("64500.27000000"),
        Decimal("64394.46000000"),
        Decimal("64427.17000000"),
    )
    # Volume in the base asset, prices in the quote asset.
    assert (first.volume, first.currency) == (Decimal("85.71120000"), "USDT")
    params = api.params()
    assert (params["symbol"], params["interval"]) == ("BTCUSDT", "1m")
    assert int(params["startTime"]) == int(start.timestamp() * 1000)
    assert int(params["endTime"]) == int(end.timestamp() * 1000) - 1


async def test_daily_candles_open_at_midnight_utc() -> None:
    api = Api(recorded("klines_1d.json"))
    start = datetime(2026, 11, 12, tzinfo=UTC)

    candles = await provider(api, RECORDED_1D).fetch_candles(
        "BTC/USDT", CandleInterval.DAY_1, start, RECORDED_1D
    )

    assert [c.timestamp for c in candles] == [
        datetime(2026, 11, 15, tzinfo=UTC),
        datetime(2026, 11, 16, tzinfo=UTC),
    ]
    assert api.params()["interval"] == "1d"


async def test_candles_outside_the_range_are_dropped() -> None:
    api = Api(recorded("klines_1d.json"))

    candles = await provider(api, RECORDED_1D).fetch_candles(
        "BTC/USDT",
        CandleInterval.DAY_1,
        datetime(2026, 11, 16, tzinfo=UTC),
        datetime(2026, 11, 17, tzinfo=UTC),
    )

    assert [c.timestamp for c in candles] == [datetime(2026, 11, 16, tzinfo=UTC)]


async def test_full_pages_are_followed() -> None:
    minute = 60_000
    first = 1794700800000
    page = [
        [first + i * minute, "1", "1", "1", "1", "0", first + (i + 1) * minute - 1]
        for i in range(1000)
    ]
    rest = [[first + 1000 * minute, "1", "1", "1", "1", "0", first + 1001 * minute - 1]]
    api = Api(httpx.Response(200, json=page), httpx.Response(200, json=rest))

    candles = await provider(api, RECORDED_1D).fetch_candles(
        "BTC/USDT", CandleInterval.MIN_1, ms(first), ms(first + 2000 * minute)
    )

    assert len(candles) == 1001
    assert int(api.params(1)["startTime"]) == first + 999 * minute + 1


# --- Search, registration and budget -------------------------------------------------------


async def test_search_finds_trading_pairs_by_asset() -> None:
    api = Api(recorded("exchange_info.json"))

    matches = await provider(api).search_symbols("eth")

    assert {m.provider_symbol for m in matches} == {"ETH/BTC", "ETH/USDT"}
    assert api.params()["symbolStatus"] == "TRADING"


def test_adapter_is_registered_with_its_capabilities() -> None:
    assert ADAPTERS["binance"] is BinanceProvider
    assert BinanceProvider.capabilities == {
        Capability.QUOTES,
        Capability.CANDLES,
        Capability.SYMBOL_SEARCH,
    }


def test_planned_credits_are_request_weight() -> None:
    pairs = [f"C{i}/USDT" for i in range(25)]

    assert BinanceProvider.planned_credits(JobKind.QUOTES, pairs[:5]) == 2
    assert BinanceProvider.planned_credits(JobKind.QUOTES, pairs) == 4
    assert BinanceProvider.planned_credits(JobKind.CANDLES, pairs[:5]) == 10


def test_shipped_seed_configures_binance(settings: Settings) -> None:
    seed = load_seed_file(settings.seed_file)
    (binance,) = [p for p in seed.providers if p.name == "binance"]
    assert binance.configuration.auth.type == "none"
    # Well under Binance's 6,000 request weight a minute per IP address.
    (limit,) = binance.configuration.rate_limits
    assert limit.period.value == "minute" and limit.limit <= 6000

    tracked = [i for i in seed.instruments if "binance" in i.providers]
    assert tracked
    for instrument in tracked:
        assert instrument.asset_type is AssetType.CRYPTO
        # Tether-quoted pairs are their own instruments, never the USD ones.
        assert instrument.symbol == instrument.providers["binance"]
        assert instrument.currency == instrument.symbol.split("/")[1] == "USDT"
        assert set(instrument.providers) == {"binance"}

    (quotes,) = [j for j in seed.jobs if j.name == "binance_quotes"]
    assert quotes.interval_minutes == 1
