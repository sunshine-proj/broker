"""CoinGecko adapter, against responses recorded from the real keyless API.

The fixtures under tests/fixtures/coingecko/ are unmodified API responses
(recorded 2026-11-16). No test calls the network.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config.settings import Settings
from app.models.enums import AssetType, Freshness, ObservationType, QuoteKind
from app.providers.base import Capability
from app.providers.coingecko import CoinGeckoProvider
from app.providers.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderRateLimitedError,
    ProviderResponseError,
)
from app.providers.factory import ADAPTERS
from app.schemas.provider_config import RatePeriod
from app.services.seed import load_seed_file
from tests.providers_support import FakeClock, make_client

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "coingecko"
# When the fixtures were recorded.
RECORDED = datetime(2026, 11, 16, 13, 30, tzinfo=UTC)
DEMO_AUTH = {
    "type": "header",
    "name": "x-cg-demo-api-key",
    "env": "COINGECKO_API_KEY",
    "optional": True,
}
SYMBOLS = ["bitcoin/usd", "ethereum/usd", "tether/usd", "solana/usd", "notacoin/usd"]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_bytes())


def recorded(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=(FIXTURES / f"{name}.json").read_bytes())


class Api:
    """Answers each request by path and records it."""

    def __init__(self, **routes: httpx.Response | list[httpx.Response]) -> None:
        # `global_` stands for /global, a reserved word in Python.
        self.routes = {
            path.rstrip("_"): r if isinstance(r, list) else [r] for path, r in routes.items()
        }
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path.removeprefix("/v1/").replace("/", "_")
        responses = self.routes[key]
        return responses.pop(0) if len(responses) > 1 else responses[0]

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def provider(
    api: Api,
    *,
    now: datetime = RECORDED,
    clock: FakeClock | None = None,
    credential: str | None = None,
    **settings: object,
) -> CoinGeckoProvider:
    client = make_client(api, clock=clock, credential=credential, auth=DEMO_AUTH, **settings)
    return CoinGeckoProvider(client, clock=lambda: now)


def by_type(quotes: list[Any], symbol: str) -> dict[ObservationType, Any]:
    return {q.observation_type: q for q in quotes if q.provider_symbol == symbol}


# --- Current market data -----------------------------------------------------------


async def test_market_data_is_normalized() -> None:
    api = Api(coins_markets=recorded("coins_markets"), global_=recorded("global"))

    quotes = await provider(api).fetch_quotes(
        SYMBOLS[:4], set(ObservationType) - {ObservationType.DOMINANCE}
    )

    bitcoin = by_type(quotes, "bitcoin/usd")
    assert set(bitcoin) == {
        ObservationType.PRICE,
        ObservationType.MARKET_CAP,
        ObservationType.VOLUME,
        ObservationType.CIRCULATING_SUPPLY,
        ObservationType.TOTAL_SUPPLY,
    }
    price = bitcoin[ObservationType.PRICE]
    assert price.value == Decimal("64093.34119812878")
    assert (price.currency, price.unit) == ("USD", "currency")
    assert price.quote_kind is QuoteKind.INDICATIVE
    assert price.freshness is Freshness.DELAYED
    assert price.observed_at == datetime(2026, 11, 16, 13, 29, 39, tzinfo=UTC)

    assert bitcoin[ObservationType.MARKET_CAP].value == Decimal("1289097514250")
    volume = bitcoin[ObservationType.VOLUME]
    assert (volume.value, volume.currency) == (Decimal("29225066651"), "USD")
    assert volume.metadata == {"field": "total_volume"}
    supply = bitcoin[ObservationType.CIRCULATING_SUPPLY]
    assert (supply.value, supply.currency, supply.unit) == (Decimal("20112815.0"), None, "count")
    assert bitcoin[ObservationType.TOTAL_SUPPLY].value == Decimal("20112831.0")

    assert by_type(quotes, "tether/usd")[ObservationType.PRICE].value == Decimal(
        "0.9992149532463409"
    )


async def test_one_request_covers_every_coin() -> None:
    api = Api(coins_markets=recorded("coins_markets"))

    await provider(api).fetch_quotes(SYMBOLS, {ObservationType.PRICE})

    (request,) = api.requests
    assert request.url.path == "/v1/coins/markets"
    assert dict(request.url.params) == {
        "vs_currency": "usd",
        "ids": "bitcoin,ethereum,tether,solana,notacoin",
        "per_page": "250",
        "precision": "full",
    }


async def test_unknown_coin_is_left_out_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    api = Api(coins_markets=recorded("coins_markets"))

    with caplog.at_level(logging.WARNING, logger="app.providers.coingecko"):
        quotes = await provider(api).fetch_quotes(SYMBOLS, {ObservationType.PRICE})

    assert {q.provider_symbol for q in quotes} == set(SYMBOLS[:4])
    (record,) = caplog.records
    assert record.symbol == "notacoin/usd"


async def test_only_the_requested_types_are_returned() -> None:
    api = Api(coins_markets=recorded("coins_markets"))

    quotes = await provider(api).fetch_quotes(SYMBOLS[:4], {ObservationType.PRICE})

    assert {q.observation_type for q in quotes} == {ObservationType.PRICE}
    assert api.paths() == ["/v1/coins/markets"]


async def test_dominance_comes_from_the_global_endpoint() -> None:
    api = Api(coins_markets=recorded("coins_markets"), global_=recorded("global"))

    quotes = await provider(api).fetch_quotes(SYMBOLS[:4], {ObservationType.DOMINANCE})

    assert api.paths() == ["/v1/coins/markets", "/v1/global"]
    assert {q.observation_type for q in quotes} == {ObservationType.DOMINANCE}
    bitcoin = by_type(quotes, "bitcoin/usd")[ObservationType.DOMINANCE]
    assert bitcoin.value == Decimal("59.27015985000001")
    assert (bitcoin.unit, bitcoin.currency) == ("percent", None)
    # The global figures carry their own update time.
    assert bitcoin.observed_at == datetime.fromtimestamp(1794835349, UTC)
    assert by_type(quotes, "solana/usd")[ObservationType.DOMINANCE].value == Decimal(
        "1.9283646752533912"
    )


async def test_everything_is_fetched_when_no_types_are_given() -> None:
    api = Api(coins_markets=recorded("coins_markets"), global_=recorded("global"))

    quotes = await provider(api).fetch_quotes(SYMBOLS[:4])

    assert len(by_type(quotes, "bitcoin/usd")) == 6
    assert len(api.requests) == 2


async def test_coin_outside_the_dominance_list_gets_no_dominance() -> None:
    markets = fixture("coins_markets")
    markets[3]["symbol"] = "not-in-the-top-ten"
    api = Api(coins_markets=httpx.Response(200, json=markets), global_=recorded("global"))

    quotes = await provider(api).fetch_quotes(SYMBOLS[:4], {ObservationType.DOMINANCE})

    assert "solana/usd" not in {q.provider_symbol for q in quotes}
    assert len(quotes) == 3


@pytest.mark.parametrize(
    ("field", "value", "missing"),
    [
        ("market_cap", None, ObservationType.MARKET_CAP),
        ("market_cap", 0, ObservationType.MARKET_CAP),
        ("circulating_supply", 0, ObservationType.CIRCULATING_SUPPLY),
        ("total_supply", None, ObservationType.TOTAL_SUPPLY),
        ("current_price", None, ObservationType.PRICE),
    ],
)
async def test_missing_values_are_not_stored(
    field: str, value: object, missing: ObservationType
) -> None:
    markets = fixture("coins_markets")
    markets[0][field] = value
    api = Api(coins_markets=httpx.Response(200, json=markets))

    quotes = await provider(api).fetch_quotes(
        ["bitcoin/usd"], set(ObservationType) - {ObservationType.DOMINANCE}
    )

    assert missing not in by_type(quotes, "bitcoin/usd")
    assert len(by_type(quotes, "bitcoin/usd")) == 4


async def test_zero_volume_is_a_real_value() -> None:
    markets = fixture("coins_markets")
    markets[0]["total_volume"] = 0
    api = Api(coins_markets=httpx.Response(200, json=markets))

    quotes = await provider(api).fetch_quotes(["bitcoin/usd"], {ObservationType.VOLUME})

    assert quotes[0].value == 0


async def test_each_vs_currency_gets_its_own_request() -> None:
    api = Api(coins_markets=recorded("coins_markets"))

    quotes = await provider(api).fetch_quotes(
        ["bitcoin/usd", "bitcoin/EUR"], {ObservationType.PRICE}
    )

    assert [r.url.params["vs_currency"] for r in api.requests] == ["usd", "eur"]
    assert {(q.provider_symbol, q.currency) for q in quotes} == {
        ("bitcoin/usd", "USD"),
        ("bitcoin/EUR", "EUR"),
    }


async def test_batches_are_capped_by_configuration() -> None:
    api = Api(coins_markets=recorded("coins_markets"))

    await provider(api, max_batch_size=2).fetch_quotes(SYMBOLS[:4], {ObservationType.PRICE})

    assert [r.url.params["ids"] for r in api.requests] == ["bitcoin,ethereum", "tether,solana"]


async def test_symbol_without_currency_is_a_configuration_error() -> None:
    api = Api(coins_markets=recorded("coins_markets"))

    with pytest.raises(ProviderConfigurationError, match="<coin id>/<vs currency>"):
        await provider(api).fetch_quotes(["bitcoin"])

    assert api.requests == []


async def test_malformed_market_data_is_a_response_error() -> None:
    markets = fixture("coins_markets")
    del markets[0]["last_updated"]
    api = Api(coins_markets=httpx.Response(200, json=markets))

    with pytest.raises(ProviderResponseError, match="last_updated"):
        await provider(api).fetch_quotes(["bitcoin/usd"])


async def test_non_list_market_data_is_a_response_error() -> None:
    api = Api(coins_markets=httpx.Response(200, json={"error": "unexpected"}))

    with pytest.raises(ProviderResponseError):
        await provider(api).fetch_quotes(["bitcoin/usd"])


# --- Authentication and rate limiting ---------------------------------------------------


async def test_demo_key_is_sent_as_a_header_when_configured() -> None:
    api = Api(coins_markets=recorded("coins_markets"))

    await provider(api, credential="demo-key").fetch_quotes(
        ["bitcoin/usd"], {ObservationType.PRICE}
    )

    request = api.requests[0]
    assert request.headers["x-cg-demo-api-key"] == "demo-key"
    assert "demo-key" not in str(request.url)


async def test_keyless_requests_send_no_key() -> None:
    api = Api(coins_markets=recorded("coins_markets"))

    await provider(api).fetch_quotes(["bitcoin/usd"], {ObservationType.PRICE})

    assert "x-cg-demo-api-key" not in api.requests[0].headers


async def test_rate_limit_waits_for_retry_after() -> None:
    clock = FakeClock()
    rate_limited = httpx.Response(
        429,
        headers={"Retry-After": "60"},
        content=(FIXTURES / "rate_limited.json").read_bytes(),
    )
    api = Api(coins_markets=[rate_limited, recorded("coins_markets")])

    quotes = await provider(api, clock=clock, backoff_max_seconds=65).fetch_quotes(
        ["bitcoin/usd"], {ObservationType.PRICE}
    )

    assert len(quotes) == 1
    assert clock.sleeps == [60]


async def test_rate_limit_with_a_long_wait_is_reported() -> None:
    api = Api(
        coins_markets=httpx.Response(
            429,
            headers={"Retry-After": "60"},
            content=(FIXTURES / "rate_limited.json").read_bytes(),
        )
    )

    with pytest.raises(ProviderRateLimitedError):
        await provider(api, backoff_max_seconds=30).fetch_quotes(["bitcoin/usd"])

    assert len(api.requests) == 1


# --- History ----------------------------------------------------------------------------

# Late enough for every point of the recorded hourly chart to have settled.
SETTLED = datetime(2026, 11, 19, tzinfo=UTC)


async def test_history_is_normalized() -> None:
    api = Api(coins_bitcoin_market_chart_range=recorded("market_chart_hourly"))
    start = datetime(2026, 11, 14, tzinfo=UTC)

    quotes = await provider(api, now=SETTLED).fetch_quote_history("bitcoin/usd", start, SETTLED)

    prices = [q for q in quotes if q.observation_type is ObservationType.PRICE]
    assert len(prices) == 47
    assert len(quotes) == 47 * 3
    first = prices[0]
    assert first.observed_at == datetime(2026, 11, 14, 14, 0, tzinfo=UTC)
    assert first.value == Decimal("66786.8048274595")
    assert (first.currency, first.quote_kind, first.freshness) == (
        "USD",
        QuoteKind.INDICATIVE,
        Freshness.DELAYED,
    )
    volumes = [q for q in quotes if q.observation_type is ObservationType.VOLUME]
    assert volumes[-1].value == Decimal("28837192530.222466")
    assert volumes[-1].metadata == {"field": "total_volumes"}


async def test_history_request_uses_unix_seconds() -> None:
    api = Api(coins_bitcoin_market_chart_range=recorded("market_chart_hourly"))
    start = datetime(2026, 11, 14, tzinfo=UTC)
    end = datetime(2026, 11, 16, tzinfo=UTC)

    await provider(api, now=SETTLED).fetch_quote_history("bitcoin/usd", start, end)

    request = api.requests[0]
    assert request.url.path == "/v1/coins/bitcoin/market_chart/range"
    assert dict(request.url.params) == {
        "vs_currency": "usd",
        "from": str(int(start.timestamp())),
        "to": str(int(end.timestamp())),
        "precision": "full",
    }


async def test_history_of_the_last_two_days_is_not_taken() -> None:
    # At recording time, every point of the hourly chart is less than two days old.
    api = Api(coins_bitcoin_market_chart_range=recorded("market_chart_hourly"))
    start = datetime(2026, 11, 14, tzinfo=UTC)

    quotes = await provider(api, now=RECORDED).fetch_quote_history("bitcoin/usd", start, RECORDED)

    assert quotes == []
    # The request itself already stopped at the settlement point.
    assert api.requests[0].url.params["to"] == str(int((RECORDED - timedelta(days=2)).timestamp()))


async def test_range_entirely_within_the_last_two_days_sends_nothing() -> None:
    api = Api()

    quotes = await provider(api, now=RECORDED).fetch_quote_history(
        "bitcoin/usd", RECORDED - timedelta(days=1), RECORDED
    )

    assert quotes == []
    assert api.requests == []


async def test_daily_history_points_are_at_midnight_utc() -> None:
    api = Api(coins_ethereum_market_chart_range=recorded("market_chart_daily"))

    quotes = await provider(api, now=RECORDED).fetch_quote_history(
        "ethereum/usd", RECORDED - timedelta(days=100), RECORDED
    )

    prices = [q for q in quotes if q.observation_type is ObservationType.PRICE]
    assert prices
    assert all(q.observed_at.time() == datetime.min.time() for q in prices)
    assert prices[0].value == Decimal("1915.447810935271")
    assert max(q.observed_at for q in prices) < RECORDED - timedelta(days=2)


async def test_history_beyond_the_free_plan_is_shortened(caplog: pytest.LogCaptureFixture) -> None:
    api = Api(coins_ethereum_market_chart_range=recorded("market_chart_daily"))

    with caplog.at_level(logging.WARNING, logger="app.providers.coingecko"):
        await provider(api, now=RECORDED).fetch_quote_history(
            "ethereum/usd", RECORDED - timedelta(days=400), RECORDED
        )

    start = int(api.requests[0].url.params["from"])
    assert RECORDED.timestamp() - start < 365 * 86400
    assert caplog.records[0].symbol == "ethereum/usd"


async def test_refusal_for_history_beyond_the_plan_is_not_an_auth_error() -> None:
    api = Api(coins_bitcoin_market_chart_range=recorded("market_chart_beyond_history", status=401))

    with pytest.raises(ProviderResponseError, match="beyond what the plan allows"):
        await provider(api, now=SETTLED).fetch_quote_history(
            "bitcoin/usd", SETTLED - timedelta(days=30), SETTLED
        )


async def test_other_401_responses_stay_auth_errors() -> None:
    body = {"status": {"error_code": 10002, "error_message": "API Key Missing"}}
    api = Api(coins_bitcoin_market_chart_range=httpx.Response(401, json=body))

    with pytest.raises(ProviderAuthError):
        await provider(api, now=SETTLED).fetch_quote_history(
            "bitcoin/usd", SETTLED - timedelta(days=30), SETTLED
        )


async def test_unknown_coin_history_is_a_response_error() -> None:
    api = Api(coins_notacoin_market_chart_range=recorded("market_chart_unknown_coin", status=404))

    with pytest.raises(ProviderResponseError, match="HTTP 404"):
        await provider(api, now=SETTLED).fetch_quote_history(
            "notacoin/usd", SETTLED - timedelta(days=30), SETTLED
        )


async def test_malformed_history_is_a_response_error() -> None:
    chart = fixture("market_chart_hourly")
    chart["prices"][0] = [1794664800000]
    api = Api(coins_bitcoin_market_chart_range=httpx.Response(200, json=chart))

    with pytest.raises(ProviderResponseError, match="prices"):
        await provider(api, now=SETTLED).fetch_quote_history(
            "bitcoin/usd", SETTLED - timedelta(days=30), SETTLED
        )


# --- Search -------------------------------------------------------------------------


async def test_search_returns_coin_ids() -> None:
    api = Api(search=recorded("search"))

    matches = await provider(api).search_symbols("bitcoin")

    assert api.requests[0].url.params["query"] == "bitcoin"
    assert (matches[0].provider_symbol, matches[0].name) == ("bitcoin", "Bitcoin")
    assert matches[0].instrument_type == "crypto (BTC)"
    assert len(matches) == 25


# --- Registration and seed --------------------------------------------------------------


def test_adapter_is_registered_with_its_capabilities() -> None:
    assert ADAPTERS["coingecko"] is CoinGeckoProvider
    assert CoinGeckoProvider.capabilities == {
        Capability.QUOTES,
        Capability.QUOTE_HISTORY,
        Capability.SYMBOL_SEARCH,
    }


def test_shipped_seed_configures_coingecko_within_its_budget(settings: Settings) -> None:
    seed = load_seed_file(settings.seed_file)
    (coingecko,) = [p for p in seed.providers if p.name == "coingecko"]
    config = coingecko.configuration
    limits = {limit.period: limit for limit in config.rate_limits}

    assert config.auth.optional and config.auth.env == "COINGECKO_API_KEY"
    # The keyless API allows about 5 calls a minute and asks for 60 seconds after a 429.
    assert limits[RatePeriod.MINUTE].limit <= 5
    assert config.backoff_max_seconds > 60
    assert config.rate_limit_max_wait_seconds > 60

    coins = [i for i in seed.instruments if "coingecko" in i.providers and i.enabled]
    assert coins and all(i.asset_type is AssetType.CRYPTO for i in coins)
    # Symbol BASE/QUOTE; CoinGecko symbol <coin id>/<vs currency> in the same currency.
    for coin in coins:
        assert coin.providers["coingecko"].split("/")[1].upper() == coin.currency
        assert coin.symbol.split("/")[1] == coin.currency
    # Monthly budget: quotes every 5 minutes in one request, dominance hourly,
    # one history refresh a day per coin, over a 31-day month.
    month = 31
    calls = month * (24 * 12 + 24 + len(coins))
    assert calls <= limits[RatePeriod.MONTH].limit
    assert limits[RatePeriod.MONTH].resets_on_boundary
