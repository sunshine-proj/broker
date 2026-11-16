"""FRED adapter.

No FRED API key was available when the adapter was written, so the fixtures
under tests/fixtures/fred/ are not recordings. They follow the response format
in FRED's API documentation and hold real values taken from FRED's public CSV
download on 2026-11-16. Replace them with recordings once a key is available.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.config.settings import Settings
from app.models.enums import AssetType, Freshness, ObservationType, QuoteKind
from app.providers.base import Capability
from app.providers.errors import (
    ProviderAuthError,
    ProviderConfigurationError,
    ProviderResponseError,
)
from app.providers.factory import ADAPTERS, ProviderFactory
from app.providers.fred import FredProvider
from app.services.seed import load_seed_file
from tests.providers_support import make_client

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "fred"
KEY = "abcdefghijklmnopqrstuvwxyz012345"
AUTH = {"type": "query", "name": "api_key", "env": "FRED_API_KEY"}


def recorded(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=(FIXTURES / name).read_bytes())


class Api:
    """Answers each request with the next queued response and records it."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def params(self, index: int = 0) -> dict[str, str]:
        return dict(self.requests[index].url.params)


def provider(api: Api) -> FredProvider:
    return FredProvider(make_client(api, credential=KEY, auth=AUTH))


def day(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# --- Quotes ------------------------------------------------------------------------------


async def test_treasury_yield_is_normalized() -> None:
    api = Api(recorded("observations_dgs10_latest.json"))

    quotes = await provider(api).fetch_quotes(["DGS10"])

    # The holiday on 2026-11-11 has no value and is left out; oldest first.
    assert [(q.observed_at, q.value) for q in quotes] == [
        (day("2026-11-06"), Decimal("4.86")),
        (day("2026-11-09"), Decimal("4.88")),
        (day("2026-11-10"), Decimal("4.98")),
        (day("2026-11-12"), Decimal("5.10")),
    ]
    latest = quotes[-1]
    assert latest.observation_type is ObservationType.YIELD
    assert (latest.unit, latest.currency) == ("percent", None)
    assert (latest.quote_kind, latest.freshness) == (QuoteKind.REFERENCE, Freshness.END_OF_DAY)


async def test_latest_request_asks_for_the_last_few_observations_with_the_key() -> None:
    api = Api(recorded("observations_dgs10_latest.json"))

    await provider(api).fetch_quotes(["DGS10"])

    request = api.requests[0]
    assert request.url.path == "/v1/series/observations"
    assert api.params() == {
        "file_type": "json",
        "series_id": "DGS10",
        "sort_order": "desc",
        "limit": "5",
        "api_key": KEY,
    }


async def test_federal_funds_rate_is_a_rate() -> None:
    api = Api(recorded("observations_dff_latest.json"))

    quotes = await provider(api).fetch_quotes(["DFF"])

    assert {q.observation_type for q in quotes} == {ObservationType.RATE}
    assert quotes[-1].value == Decimal("3.88")
    assert len(quotes) == 5


@pytest.mark.parametrize(
    ("series", "kind"),
    [
        ("DGS1MO", ObservationType.YIELD),
        ("DGS3MO", ObservationType.YIELD),
        ("DGS30", ObservationType.YIELD),
        ("DFF", ObservationType.RATE),
        ("DGS10X", ObservationType.RATE),
    ],
)
def test_treasury_constant_maturity_series_are_yields(series: str, kind: ObservationType) -> None:
    assert FredProvider._observation_type(series) is kind


async def test_one_request_per_series() -> None:
    api = Api(recorded("observations_dgs10_latest.json"), recorded("observations_dff_latest.json"))

    quotes = await provider(api).fetch_quotes(["DGS10", "DFF"])

    assert [api.params(i)["series_id"] for i in range(2)] == ["DGS10", "DFF"]
    assert {q.provider_symbol for q in quotes} == {"DGS10", "DFF"}


async def test_series_of_other_types_are_not_requested() -> None:
    api = Api(recorded("observations_dgs10_latest.json"))

    quotes = await provider(api).fetch_quotes(["DGS10", "DFF"], {ObservationType.YIELD})

    assert {q.provider_symbol for q in quotes} == {"DGS10"}
    assert len(api.requests) == 1


async def test_history_is_limited_to_the_requested_range() -> None:
    api = Api(recorded("observations_dgs10_history.json"))

    quotes = await provider(api).fetch_quote_history("DGS10", day("2026-11-04"), day("2026-11-10"))

    assert [q.observed_at.day for q in quotes] == [4, 5, 6, 9]
    # FRED includes both ends, so the day before the exclusive end is the last one asked for.
    assert api.params()["observation_start"] == "2026-11-04"
    assert api.params()["observation_end"] == "2026-11-09"


# --- Indicators ----------------------------------------------------------------------------


async def test_monthly_indicator_records_when_each_value_was_published() -> None:
    api = Api(recorded("observations_cpiaucsl.json"))

    values = await provider(api).fetch_indicator("CPIAUCSL", day("2026-06-01"), day("2026-11-16"))

    assert [(v.observed_at.month, v.value) for v in values] == [
        (6, Decimal("332.568")),
        (7, Decimal("332.813")),
        (8, Decimal("334.131")),
        (9, Decimal("336.298")),
        (10, Decimal("337.476")),
    ]
    assert values[-1].observed_at == day("2026-10-01")
    assert values[-1].metadata == {"realtime_start": "2026-11-13"}
    assert values[-1].series_code == "CPIAUCSL"


async def test_quarterly_indicator_is_dated_by_the_quarters_first_day() -> None:
    api = Api(recorded("observations_gdp.json"))

    values = await provider(api).fetch_indicator("GDP", day("2025-07-01"), day("2026-11-16"))

    assert values[-1].observed_at == day("2026-07-01")
    assert values[-1].value == Decimal("33010.036")


async def test_missing_indicator_values_are_skipped() -> None:
    body = json.loads((FIXTURES / "observations_cpiaucsl.json").read_bytes())
    body["observations"][0]["value"] = "."
    api = Api(httpx.Response(200, json=body))

    values = await provider(api).fetch_indicator("CPIAUCSL", day("2026-06-01"), day("2026-11-16"))

    assert [v.observed_at.month for v in values] == [7, 8, 9, 10]


# --- Search ---------------------------------------------------------------------------------


async def test_search_returns_series_ids() -> None:
    api = Api(recorded("series_search.json"))

    matches = await provider(api).search_symbols("treasury 10-year")

    assert api.requests[0].url.path == "/v1/series/search"
    assert api.params()["search_text"] == "treasury 10-year"
    assert [m.provider_symbol for m in matches] == ["DGS10", "GS10"]
    assert matches[0].instrument_type == "Daily, Percent"
    assert matches[0].country == "US"


# --- Errors ---------------------------------------------------------------------------------


async def test_unregistered_key_is_an_auth_error() -> None:
    api = Api(recorded("error_invalid_key.json", status=400))

    with pytest.raises(ProviderAuthError, match="credential rejected"):
        await provider(api).fetch_quotes(["DGS10"])

    assert len(api.requests) == 1


async def test_unknown_series_is_a_response_error() -> None:
    api = Api(recorded("error_unknown_series.json", status=400))

    with pytest.raises(ProviderResponseError, match="HTTP 400"):
        await provider(api).fetch_indicator("NOTASERIES", day("2026-01-01"), day("2026-11-16"))


async def test_malformed_observations_are_a_response_error() -> None:
    body = json.loads((FIXTURES / "observations_gdp.json").read_bytes())
    body["observations"][0]["date"] = "Q3 2025"
    api = Api(httpx.Response(200, json=body))

    with pytest.raises(ProviderResponseError, match="date"):
        await provider(api).fetch_indicator("GDP", day("2025-01-01"), day("2026-11-16"))


async def test_api_key_never_appears_in_errors() -> None:
    api = Api(recorded("error_unknown_series.json", status=400))

    with pytest.raises(ProviderResponseError) as raised:
        await provider(api).fetch_quotes(["NOTASERIES"])

    assert KEY not in str(raised.value)


def test_provider_without_key_cannot_be_built(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    monkeypatch.delenv("FRED_API_KEY", raising=False)
    row = SimpleNamespace(
        name="fred",
        base_url="https://api.stlouisfed.org/fred",
        enabled=True,
        configuration={"auth": AUTH},
    )
    factory = ProviderFactory(settings, http=httpx.AsyncClient(), adapters=ADAPTERS)

    with pytest.raises(ProviderConfigurationError, match="FRED_API_KEY is not set"):
        factory.build(row)


# --- Registration and seed -----------------------------------------------------------------


def test_adapter_is_registered_with_its_capabilities() -> None:
    assert ADAPTERS["fred"] is FredProvider
    assert FredProvider.capabilities == {
        Capability.QUOTES,
        Capability.QUOTE_HISTORY,
        Capability.INDICATORS,
        Capability.SYMBOL_SEARCH,
    }


def test_shipped_seed_configures_fred(settings: Settings) -> None:
    seed = load_seed_file(settings.seed_file)
    (fred,) = [p for p in seed.providers if p.name == "fred"]
    config = fred.configuration

    assert (config.auth.type, config.auth.name, config.auth.env) == (
        "query",
        "api_key",
        "FRED_API_KEY",
    )
    assert not config.auth.optional
    # FRED allows 120 requests a minute.
    assert all(limit.limit <= 120 for limit in config.rate_limits)

    instruments = [i for i in seed.instruments if "fred" in i.providers]
    yields = [i for i in instruments if i.asset_type is AssetType.GOVERNMENT_BOND_YIELD]
    assert {i.providers["fred"] for i in yields} >= {"DGS2", "DGS10", "DGS30"}
    for instrument in instruments:
        expected = (
            ObservationType.YIELD
            if instrument.asset_type is AssetType.GOVERNMENT_BOND_YIELD
            else ObservationType.RATE
        )
        assert FredProvider._observation_type(instrument.providers["fred"]) is expected
        assert instrument.currency is None

    indicators = {i.provider_series_code for i in seed.indicators if i.provider == "fred"}
    assert {"CPIAUCSL", "UNRATE", "GDP"} <= indicators
