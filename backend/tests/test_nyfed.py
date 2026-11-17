"""New York Fed adapter, against responses recorded from the live Markets Data API.

The fixtures under tests/fixtures/nyfed/ are unmodified API responses
(recorded 2026-11-17). No test calls the network.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.config.settings import Settings
from app.models.enums import AssetType, Freshness, ObservationType, QuoteKind
from app.providers.base import Capability
from app.providers.errors import ProviderConfigurationError, ProviderResponseError
from app.providers.factory import ADAPTERS
from app.providers.nyfed import NyFedProvider
from app.services.seed import load_seed_file
from tests.providers_support import make_client

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "nyfed"


def recorded(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=(FIXTURES / name).read_bytes())


class Api:
    """Answers by path and records the requests."""

    def __init__(self, **paths: httpx.Response) -> None:
        self.paths = paths
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.paths[request.url.path.split("/v1/", 1)[1].replace("/", "_").replace(".", "_")]


def provider(api: Api) -> NyFedProvider:
    return NyFedProvider(make_client(api))


def day(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


async def test_latest_rates_are_daily_reference_rates_in_percent() -> None:
    api = Api(rates_secured_sofr_last_5_json=recorded("sofr_last.json"))

    quotes = await provider(api).fetch_quotes(["SOFR"])

    # Oldest first, dated by the business day the rate applies to.
    assert [(q.observed_at, q.value) for q in quotes] == [
        (day("2026-11-10"), Decimal("3.89")),
        (day("2026-11-12"), Decimal("3.90")),
        (day("2026-11-13"), Decimal("3.90")),
    ]
    latest = quotes[-1]
    assert latest.observation_type is ObservationType.RATE
    assert (latest.unit, latest.currency) == ("percent", None)
    assert (latest.freshness, latest.quote_kind) == (Freshness.END_OF_DAY, QuoteKind.REFERENCE)
    # The distribution and volume behind the rate are kept, exactly.
    assert latest.metadata == {
        "percentPercentile1": "3.85",
        "percentPercentile25": "3.87",
        "percentPercentile75": "3.95",
        "percentPercentile99": "3.97",
        "volumeInBillions": "3092",
    }


async def test_effr_keeps_the_target_range_and_uses_the_unsecured_path() -> None:
    api = Api(rates_unsecured_effr_last_5_json=recorded("effr_last.json"))

    quotes = await provider(api).fetch_quotes(["EFFR"])

    assert quotes[-1].value == Decimal("3.88")
    assert quotes[-1].metadata is not None
    assert (quotes[-1].metadata["targetRateFrom"], quotes[-1].metadata["targetRateTo"]) == (
        "3.75",
        "4.00",
    )


async def test_one_request_per_rate() -> None:
    api = Api(
        rates_secured_sofr_last_5_json=recorded("sofr_last.json"),
        rates_unsecured_effr_last_5_json=recorded("effr_last.json"),
    )

    quotes = await provider(api).fetch_quotes(["SOFR", "EFFR"])

    assert {q.provider_symbol for q in quotes} == {"SOFR", "EFFR"}
    assert len(api.requests) == 2


async def test_other_observation_types_need_no_request() -> None:
    api = Api()

    assert await provider(api).fetch_quotes(["SOFR"], {ObservationType.YIELD}) == []
    assert api.requests == []


async def test_history_asks_for_inclusive_dates_and_keeps_the_range() -> None:
    api = Api(rates_secured_sofr_search_json=recorded("sofr_search.json"))
    start, end = day("2026-11-03"), day("2026-11-06")

    quotes = await provider(api).fetch_quote_history("SOFR", start, end)

    assert [q.observed_at for q in quotes] == [
        day("2026-11-03"),
        day("2026-11-04"),
        day("2026-11-05"),
    ]
    params = dict(api.requests[0].url.params)
    assert (params["startDate"], params["endDate"]) == ("2026-11-03", "2026-11-05")


async def test_no_published_rates_is_an_empty_result() -> None:
    api = Api(rates_secured_sofr_search_json=httpx.Response(200, json={"refRates": []}))

    assert (
        await provider(api).fetch_quote_history("SOFR", day("2026-11-14"), day("2026-11-16")) == []
    )


async def test_unknown_rate_is_a_configuration_error() -> None:
    with pytest.raises(ProviderConfigurationError, match="supported: BGCR, EFFR, OBFR, SOFR, TGCR"):
        await provider(Api()).fetch_quotes(["LIBOR"])


async def test_malformed_response_is_a_response_error() -> None:
    api = Api(rates_secured_sofr_last_5_json=httpx.Response(200, json={"rates": []}))

    with pytest.raises(ProviderResponseError):
        await provider(api).fetch_quotes(["SOFR"])


def test_adapter_is_registered_with_its_capabilities() -> None:
    assert ADAPTERS["nyfed"] is NyFedProvider
    assert NyFedProvider.capabilities == {Capability.QUOTES, Capability.QUOTE_HISTORY}


def test_shipped_seed_configures_the_new_york_fed(settings: Settings) -> None:
    seed = load_seed_file(settings.seed_file)
    (nyfed,) = [p for p in seed.providers if p.name == "nyfed"]
    assert nyfed.configuration.auth.type == "none"

    tracked = {i.symbol: i for i in seed.instruments if "nyfed" in i.providers}
    assert {"SOFR", "EFFR"} <= set(tracked)
    assert all(i.asset_type is AssetType.INTEREST_RATE for i in tracked.values())
    # EFFR from its administrator and from FRED: two providers, one instrument.
    assert tracked["EFFR"].providers == {"fred": "DFF", "nyfed": "EFFR"}
