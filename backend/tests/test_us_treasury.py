"""US Treasury adapter, against feeds recorded from home.treasury.gov.

The fixtures under tests/fixtures/us_treasury/ are unmodified XML feeds
(recorded 2026-11-17). No test calls the network.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.config.settings import Settings
from app.models.enums import AssetType, Freshness, JobKind, ObservationType, QuoteKind
from app.providers.base import Capability
from app.providers.errors import ProviderConfigurationError, ProviderResponseError
from app.providers.factory import ADAPTERS
from app.providers.us_treasury import UsTreasuryProvider
from app.services.seed import load_seed_file
from tests.providers_support import make_client

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "us_treasury"
NOW = datetime(2026, 11, 17, 7, tzinfo=UTC)


def recorded(name: str) -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / name).read_bytes())


EMPTY = "yield_curve_empty.xml"


class Api:
    """Answers by the month or year asked for and records the requests."""

    def __init__(self, **periods: str) -> None:
        self.periods = periods
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        period = params.get("field_tdr_date_value_month") or params.get("field_tdr_date_value")
        return recorded(self.periods.get(f"p{period}", EMPTY))

    def params(self) -> list[dict[str, str]]:
        return [dict(request.url.params) for request in self.requests]


def provider(api: Api, now: datetime = NOW) -> UsTreasuryProvider:
    return UsTreasuryProvider(make_client(api), clock=lambda: now)


def day(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


async def test_latest_par_yields_are_daily_reference_yields() -> None:
    api = Api(p202611="yield_curve_202611.xml")

    quotes = await provider(api).fetch_quotes(["BC_10YEAR", "BC_1MONTH"])

    ten_year = [(q.observed_at, q.value) for q in quotes if q.provider_symbol == "BC_10YEAR"]
    # The last five business days published.
    assert ten_year == [
        (day("2026-11-09"), Decimal("4.88")),
        (day("2026-11-10"), Decimal("4.98")),
        (day("2026-11-12"), Decimal("5.10")),
        (day("2026-11-13"), Decimal("5.15")),
        (day("2026-11-16"), Decimal("5.12")),
    ]
    for quote in quotes:
        assert quote.observation_type is ObservationType.YIELD
        assert (quote.unit, quote.currency) == ("percent", None)
        assert (quote.freshness, quote.quote_kind) == (Freshness.END_OF_DAY, QuoteKind.REFERENCE)
    assert [q.value for q in quotes if q.provider_symbol == "BC_1MONTH"][-1] == Decimal("4.10")


async def test_current_values_read_this_month_and_last() -> None:
    api = Api(p202611="yield_curve_202611.xml")

    await provider(api).fetch_quotes(["BC_10YEAR"])

    assert [p["field_tdr_date_value_month"] for p in api.params()] == ["202610", "202611"]
    assert {p["data"] for p in api.params()} == {"daily_treasury_yield_curve"}


async def test_early_in_the_month_last_month_fills_in() -> None:
    api = Api(p202110="yield_curve_202110.xml")

    quotes = await provider(api, datetime(2021, 11, 1, 6, tzinfo=UTC)).fetch_quotes(["BC_10YEAR"])

    assert len(quotes) == 5
    assert [p["field_tdr_date_value_month"] for p in api.params()] == ["202110", "202111"]


async def test_january_reads_december_of_the_year_before() -> None:
    api = Api()

    await provider(api, datetime(2027, 1, 4, tzinfo=UTC)).fetch_quotes(["BC_10YEAR"])

    assert [p["field_tdr_date_value_month"] for p in api.params()] == ["202612", "202701"]


async def test_a_maturity_not_yet_quoted_is_left_out() -> None:
    # The 4-month bill was first quoted in 2022.
    api = Api(p202110="yield_curve_202110.xml")

    quotes = await provider(api, datetime(2021, 10, 31, tzinfo=UTC)).fetch_quotes(
        ["BC_4MONTH", "BC_10YEAR"]
    )

    assert {q.provider_symbol for q in quotes} == {"BC_10YEAR"}


async def test_history_reads_whole_years_and_keeps_the_range() -> None:
    api = Api(p2026="yield_curve_202611.xml")

    quotes = await provider(api).fetch_quote_history(
        "BC_10YEAR", day("2026-11-05"), day("2026-11-10")
    )

    assert [q.observed_at for q in quotes] == [
        day("2026-11-05"),
        day("2026-11-06"),
        day("2026-11-09"),
    ]
    assert [p["field_tdr_date_value"] for p in api.params()] == ["2026"]


async def test_history_across_years_asks_for_each() -> None:
    api = Api()

    await provider(api).fetch_quote_history("BC_10YEAR", day("2024-12-30"), day("2026-01-01"))

    assert [p["field_tdr_date_value"] for p in api.params()] == ["2024", "2025"]


async def test_an_empty_feed_is_an_empty_result() -> None:
    assert await provider(Api()).fetch_quotes(["BC_10YEAR"]) == []


@pytest.mark.parametrize(
    "body",
    [
        "not xml",
        "<html><body>maintenance</body></html>",
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><content/></entry></feed>',
    ],
)
async def test_unexpected_documents_are_response_errors(body: str) -> None:
    client = make_client(lambda request: httpx.Response(200, text=body))

    with pytest.raises(ProviderResponseError):
        await UsTreasuryProvider(client, clock=lambda: NOW).fetch_quotes(["BC_10YEAR"])


async def test_symbols_must_be_feed_fields() -> None:
    with pytest.raises(ProviderConfigurationError, match="BC_10YEAR"):
        await provider(Api()).fetch_quotes(["DGS10"])


def test_adapter_is_registered_and_plans_two_requests() -> None:
    assert ADAPTERS["us_treasury"] is UsTreasuryProvider
    assert UsTreasuryProvider.capabilities == {Capability.QUOTES, Capability.QUOTE_HISTORY}
    assert UsTreasuryProvider.planned_credits(JobKind.QUOTES, ["BC_1MONTH", "BC_10YEAR"]) == 2


def test_shipped_seed_configures_the_us_treasury(settings: Settings) -> None:
    seed = load_seed_file(settings.seed_file)
    (treasury,) = [p for p in seed.providers if p.name == "us_treasury"]
    assert treasury.configuration.auth.type == "none"

    tracked = [i for i in seed.instruments if "us_treasury" in i.providers]
    assert len(tracked) == 11
    for instrument in tracked:
        assert instrument.asset_type is AssetType.GOVERNMENT_BOND_YIELD
        assert instrument.metadata is not None
        assert instrument.metadata["country"] == "US"
        assert instrument.metadata["curve"] == "US Treasury"
        assert instrument.symbol == f"US{instrument.metadata['maturity']}"
