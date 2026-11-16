"""Twelve Data adapter, against responses recorded from the real API.

The fixtures under tests/fixtures/twelve_data/ are unmodified API responses
(recorded 2026-11-16). No test calls the network.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.config.settings import Settings
from app.models.enums import AssetType, CandleInterval, Freshness, ObservationType, QuoteKind
from app.providers.base import Capability
from app.providers.errors import (
    ProviderAuthError,
    ProviderRateLimitedError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from app.providers.factory import ADAPTERS
from app.providers.twelve_data import TwelveDataProvider
from app.services.seed import load_seed_file
from tests.providers_support import FakeClock, make_client

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "twelve_data"
NOW = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
HEADER_AUTH = {
    "type": "header",
    "name": "Authorization",
    "prefix": "apikey ",
    "env": "TWELVEDATA_API_KEY",
}


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


class Api:
    """Answers each request with the next queued response and records the request."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


def recorded(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=fixture(name))


def provider(api: Api, *, clock: FakeClock | None = None, **settings: object) -> TwelveDataProvider:
    client = make_client(api, clock=clock, credential="test-key", auth=HEADER_AUTH, **settings)
    return TwelveDataProvider(client, clock=lambda: NOW)


# --- Quotes -------------------------------------------------------------------


async def test_batched_quotes_are_normalized() -> None:
    api = Api(recorded("quote_batch"))

    quotes = await provider(api).fetch_quotes(["AAPL", "EUR/USD", "SPY"])

    apple, eur_usd, spy = quotes
    assert apple.provider_symbol == "AAPL"
    assert apple.observation_type is ObservationType.PRICE
    assert apple.value == Decimal("356.26999")
    assert apple.currency == "USD"
    assert apple.unit == "currency"
    assert apple.quote_kind is QuoteKind.TRADED
    assert apple.freshness is Freshness.REAL_TIME
    # last_quote_at: the provider's own time for the price, not the request time.
    assert apple.observed_at == datetime(2026, 11, 13, 20, 59, tzinfo=UTC)

    assert eur_usd.observation_type is ObservationType.RATE
    assert eur_usd.value == Decimal("1.10234")
    assert eur_usd.currency == "USD"
    assert eur_usd.quote_kind is QuoteKind.INDICATIVE
    assert eur_usd.observed_at == datetime(2026, 11, 16, 9, 13, tzinfo=UTC)

    assert (spy.provider_symbol, spy.observation_type, spy.currency) == (
        "SPY",
        ObservationType.PRICE,
        "USD",
    )


async def test_quote_request_is_batched_and_authenticated_by_header() -> None:
    api = Api(recorded("quote_batch"))

    await provider(api).fetch_quotes(["AAPL", "EUR/USD", "SPY"])

    (request,) = api.requests
    assert request.url.path == "/v1/quote"
    assert request.url.params["symbol"] == "AAPL,EUR/USD,SPY"
    assert request.headers["Authorization"] == "apikey test-key"
    assert "test-key" not in str(request.url)


async def test_single_symbol_response_has_no_symbol_key() -> None:
    api = Api(recorded("quote_single"))

    (quote,) = await provider(api).fetch_quotes(["USD/JPY"])

    assert (quote.provider_symbol, quote.observation_type) == ("USD/JPY", ObservationType.RATE)
    assert quote.value == Decimal("159.45994")
    # The quote currency of the pair, taken from the symbol.
    assert quote.currency == "JPY"


async def test_symbols_are_split_into_batches_and_each_symbol_costs_a_credit() -> None:
    clock = FakeClock()
    batch = json.loads(fixture("quote_batch"))
    first = {"AAPL": batch["AAPL"], "EUR/USD": batch["EUR/USD"]}
    api = Api(httpx.Response(200, json=first), httpx.Response(200, json=batch["SPY"]))
    twelve_data = provider(
        api, clock=clock, max_batch_size=2, rate_limits=[{"limit": 2, "period": "minute"}]
    )

    quotes = await twelve_data.fetch_quotes(["AAPL", "EUR/USD", "SPY"])

    assert [q.provider_symbol for q in quotes] == ["AAPL", "EUR/USD", "SPY"]
    assert [r.url.params["symbol"] for r in api.requests] == ["AAPL,EUR/USD", "SPY"]
    # Two credits were used by the first batch, so the second waited a minute.
    assert clock.sleeps == [60]


async def test_unknown_symbol_in_a_batch_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    api = Api(recorded("quote_unknown_symbol"))

    with caplog.at_level(logging.WARNING, logger="app.providers.twelve_data"):
        quotes = await provider(api).fetch_quotes(["AAPL", "NOTASYMBOL123"])

    assert [q.provider_symbol for q in quotes] == ["AAPL"]
    (record,) = caplog.records
    assert (record.symbol, record.code) == ("NOTASYMBOL123", 404)


async def test_unknown_single_symbol_is_a_response_error() -> None:
    api = Api(recorded("quote_single_unknown", status=404))

    with pytest.raises(ProviderResponseError, match="HTTP 404"):
        await provider(api).fetch_quotes(["NOTASYMBOL123"])

    assert len(api.requests) == 1


async def test_rate_limit_response_is_retried_then_reported() -> None:
    clock = FakeClock()
    api = Api(recorded("rate_limited", status=429))
    twelve_data = provider(api, clock=clock, max_retries=2, backoff_base_seconds=15)

    with pytest.raises(ProviderRateLimitedError):
        await twelve_data.fetch_quotes(["AAPL"])

    assert len(api.requests) == 3
    assert clock.sleeps == [15, 30]


async def test_rate_limit_recovers_on_retry() -> None:
    api = Api(recorded("rate_limited", status=429), recorded("quote_single"))

    quotes = await provider(api).fetch_quotes(["USD/JPY"])

    assert len(quotes) == 1


async def test_error_reported_inside_a_successful_response() -> None:
    api = Api(recorded("rate_limited", status=200))

    with pytest.raises(ProviderRateLimitedError, match="code 429"):
        await provider(api).fetch_quotes(["AAPL"])


async def test_rejected_key_is_an_auth_error() -> None:
    api = Api(recorded("unauthorized", status=401))

    with pytest.raises(ProviderAuthError):
        await provider(api).fetch_quotes(["AAPL"])

    assert len(api.requests) == 1


async def test_timeout_is_reported_as_unavailable() -> None:
    api = Api(httpx.ReadTimeout("timed out"))

    with pytest.raises(ProviderUnavailableError, match="timed out"):
        await provider(api, max_retries=1).fetch_quotes(["AAPL"])

    assert len(api.requests) == 2


@pytest.mark.parametrize("field", ["close", "last_quote_at"])
async def test_quote_missing_a_required_field_is_a_response_error(field: str) -> None:
    quote = json.loads(fixture("quote_single"))
    del quote[field]
    api = Api(httpx.Response(200, json=quote))

    with pytest.raises(ProviderResponseError, match=field):
        await provider(api).fetch_quotes(["USD/JPY"])


async def test_non_numeric_price_is_a_response_error() -> None:
    quote = json.loads(fixture("quote_single")) | {"close": "N/A"}
    api = Api(httpx.Response(200, json=quote))

    with pytest.raises(ProviderResponseError, match="close"):
        await provider(api).fetch_quotes(["USD/JPY"])


# --- Candles --------------------------------------------------------------------

START = datetime(2026, 11, 2, tzinfo=UTC)
END = datetime(2026, 11, 14, tzinfo=UTC)


async def test_daily_candles_are_normalized() -> None:
    api = Api(recorded("time_series_daily"))

    candles = await provider(api).fetch_candles("AAPL", CandleInterval.DAY_1, START, END)

    assert len(candles) == 10
    first, last = candles[0], candles[-1]
    assert first.provider_symbol == "AAPL"
    assert first.interval is CandleInterval.DAY_1
    # A daily bar is stamped with its trading date at midnight UTC.
    assert first.timestamp == datetime(2026, 11, 2, tzinfo=UTC)
    assert (first.open, first.high, first.low, first.close) == (
        Decimal("342.67001"),
        Decimal("343.79999"),
        Decimal("335.82001"),
        Decimal("338.32001"),
    )
    assert first.volume == Decimal("64757700")
    assert first.currency == "USD"
    assert last.timestamp == datetime(2026, 11, 13, tzinfo=UTC)
    assert [c.timestamp for c in candles] == sorted(c.timestamp for c in candles)


async def test_candle_request_asks_for_unadjusted_utc_bars() -> None:
    api = Api(recorded("time_series_daily"))

    await provider(api).fetch_candles("AAPL", CandleInterval.DAY_1, START, END)

    params = dict(api.requests[0].url.params)
    assert api.requests[0].url.path == "/v1/time_series"
    assert params == {
        "symbol": "AAPL",
        "interval": "1day",
        "start_date": "2026-11-02T00:00:00",
        "end_date": "2026-11-14T00:00:00",
        "timezone": "UTC",
        "order": "asc",
        "outputsize": "5000",
        "adjust": "none",
    }


async def test_bars_outside_the_requested_range_are_dropped() -> None:
    api = Api(recorded("time_series_daily"))

    candles = await provider(api).fetch_candles(
        "AAPL",
        CandleInterval.DAY_1,
        datetime(2026, 11, 4, tzinfo=UTC),
        datetime(2026, 11, 6, tzinfo=UTC),
    )

    assert [c.timestamp.day for c in candles] == [4, 5]


async def test_forming_candle_is_not_returned() -> None:
    # The recording holds the 07:00, 08:00 and 09:00 bars; at 09:30 the last is still open.
    api = Api(recorded("time_series_forex"))

    candles = await provider(api).fetch_candles(
        "EUR/USD", CandleInterval.HOUR_1, NOW - timedelta(hours=6), NOW
    )

    assert [c.timestamp for c in candles] == [
        datetime(2026, 11, 16, 7, 0, tzinfo=UTC),
        datetime(2026, 11, 16, 8, 0, tzinfo=UTC),
    ]


async def test_forex_candles_have_no_volume_and_take_currency_from_the_pair() -> None:
    api = Api(recorded("time_series_forex"))

    candles = await provider(api).fetch_candles(
        "EUR/USD", CandleInterval.HOUR_1, NOW - timedelta(hours=6), NOW
    )

    assert candles[0].volume is None
    assert candles[0].currency == "USD"
    assert candles[0].close == Decimal("1.10176")


async def test_no_data_for_the_dates_is_an_empty_result() -> None:
    api = Api(recorded("time_series_no_data", status=400))

    candles = await provider(api).fetch_candles("AAPL", CandleInterval.DAY_1, START, END)

    assert candles == []
    assert len(api.requests) == 1


async def test_other_bad_requests_are_errors() -> None:
    body = {"code": 400, "message": "Invalid **interval** provided: 7min.", "status": "error"}
    api = Api(httpx.Response(400, json=body))

    with pytest.raises(ProviderResponseError, match="HTTP 400"):
        await provider(api).fetch_candles("AAPL", CandleInterval.DAY_1, START, END)


async def test_long_ranges_are_fetched_page_by_page() -> None:
    start = datetime(2026, 11, 2, tzinfo=UTC)

    def bars(first: int, count: int) -> list[dict[str, str]]:
        return [
            {
                "datetime": (start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S"),
                "open": "1.1",
                "high": "1.2",
                "low": "1.0",
                "close": "1.15",
            }
            for i in range(first, first + count)
        ]

    def page(values: list[dict[str, str]]) -> httpx.Response:
        return httpx.Response(200, json={"meta": {}, "values": values, "status": "ok"})

    # The second page starts with the bar the first one ended on.
    api = Api(page(bars(0, 5000)), page(bars(4999, 11)))

    candles = await provider(api).fetch_candles(
        "EUR/USD", CandleInterval.MIN_1, start, start + timedelta(days=5)
    )

    assert len(candles) == 5010
    assert len({c.timestamp for c in candles}) == 5010
    assert api.requests[1].url.params["start_date"] == "2026-11-05T11:19:01"


async def test_malformed_time_series_is_a_response_error() -> None:
    api = Api(httpx.Response(200, json={"meta": {}, "status": "ok"}))

    with pytest.raises(ProviderResponseError, match="values"):
        await provider(api).fetch_candles("AAPL", CandleInterval.DAY_1, START, END)


# --- Symbol search -----------------------------------------------------------------


async def test_symbol_search_returns_matches() -> None:
    api = Api(recorded("symbol_search"))

    matches = await provider(api).search_symbols("apple")

    assert api.requests[0].url.params["symbol"] == "apple"
    first = matches[0]
    assert (first.provider_symbol, first.name) == ("AAPL", "Apple Inc.")
    assert (first.exchange, first.currency, first.instrument_type) == (
        "XNGS",
        "USD",
        "Common Stock",
    )
    assert first.country == "United States"
    assert len(matches) == 3


# --- Registration and seed -----------------------------------------------------------


def test_adapter_is_registered_with_its_capabilities() -> None:
    assert ADAPTERS["twelve_data"] is TwelveDataProvider
    assert TwelveDataProvider.capabilities == {
        Capability.QUOTES,
        Capability.CANDLES,
        Capability.SYMBOL_SEARCH,
    }


def test_shipped_seed_configures_twelve_data_within_its_budget(settings: Settings) -> None:
    seed = load_seed_file(settings.seed_file)
    (twelve_data,) = [p for p in seed.providers if p.name == "twelve_data"]
    config = twelve_data.configuration
    limits = {limit.period.value: limit.limit for limit in config.rate_limits}

    assert limits == {"minute": 8, "day": 800}
    # Twelve Data restores credits on the minute and at midnight UTC.
    assert all(limit.resets_on_boundary for limit in config.rate_limits)
    # A rate-limited request must be able to wait out the rest of the minute.
    assert config.backoff_max_seconds > 61
    assert config.rate_limit_max_wait_seconds > 61
    assert config.max_batch_size is not None and config.max_batch_size <= limits["minute"]
    assert config.auth.env == "TWELVEDATA_API_KEY"

    tracked = [i for i in seed.instruments if "twelve_data" in i.providers and i.enabled]
    forex = [i for i in tracked if i.asset_type is AssetType.FOREX]
    listed = [i for i in tracked if i.asset_type in (AssetType.STOCK, AssetType.ETF)]
    assert len(forex) + len(listed) == len(tracked)
    # PRD section 8: every 15 minutes, listed instruments over a 6.5-hour session
    # (26 polls) and forex around the clock (96 polls), one credit per symbol.
    assert len(listed) * 26 + len(forex) * 96 <= limits["day"]
    # Forex pairs are quoted in the pair's quote currency.
    assert all(i.symbol.split("/")[1] == i.currency for i in forex)
