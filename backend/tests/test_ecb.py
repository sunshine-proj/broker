"""ECB adapter, against responses recorded from the live ECB Data Portal.

The fixtures under tests/fixtures/ecb/ are unmodified API responses
(recorded 2026-11-16; the yield curve 2026-11-17). No test calls the network.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config.settings import Settings
from app.models.enums import AssetType, Freshness, IndicatorFrequency, ObservationType, QuoteKind
from app.providers.base import Capability
from app.providers.ecb import EcbProvider, parse_period
from app.providers.errors import ProviderConfigurationError, ProviderResponseError
from app.providers.factory import ADAPTERS
from app.services.seed import load_seed_file
from tests.providers_support import make_client

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures" / "ecb"
EXR = [
    "EXR/D.USD.EUR.SP00.A",
    "EXR/D.GBP.EUR.SP00.A",
    "EXR/D.JPY.EUR.SP00.A",
    "EXR/D.CHF.EUR.SP00.A",
]
ESTR = "EST/B.EU000A2X2A25.WT"
DFR = "FM/B.U2.EUR.4F.KR.DFR.LEV"
MRO = "FM/B.U2.EUR.4F.KR.MRR_FR.LEV"


def recorded(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=(FIXTURES / name).read_bytes())


class Api:
    """Answers requests by dataflow and records them."""

    def __init__(self, **flows: httpx.Response) -> None:
        self.flows = flows
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        flow = request.url.path.split("/")[3]
        return self.flows[flow]

    def keys(self) -> list[str]:
        return ["/".join(request.url.path.split("/")[3:5]) for request in self.requests]


def provider(api: Api) -> EcbProvider:
    return EcbProvider(make_client(api))


def values(quotes: list[Any], symbol: str) -> list[tuple[datetime, Decimal]]:
    return [(q.observed_at, q.value) for q in quotes if q.provider_symbol == symbol]


# --- Periods -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period", "start"),
    [
        ("2026-11-13", datetime(2026, 11, 13, tzinfo=UTC)),
        ("2026-09", datetime(2026, 9, 1, tzinfo=UTC)),
        ("2026-Q1", datetime(2026, 1, 1, tzinfo=UTC)),
        ("2025-Q4", datetime(2025, 10, 1, tzinfo=UTC)),
        ("2026-S2", datetime(2026, 7, 1, tzinfo=UTC)),
        ("2025", datetime(2025, 1, 1, tzinfo=UTC)),
    ],
)
def test_periods_are_dated_by_their_first_day(period: str, start: datetime) -> None:
    assert parse_period(period) == start


@pytest.mark.parametrize("period", ["2026-Q5", "2026-W05", "26-11-13", "soon"])
def test_unsupported_periods_are_refused(period: str) -> None:
    with pytest.raises(ValueError):
        parse_period(period)


# --- Exchange rates ---------------------------------------------------------------------


async def test_reference_rates_are_normalized() -> None:
    api = Api(EXR=recorded("exr_latest.json"))

    quotes = await provider(api).fetch_quotes(EXR)

    usd = [q for q in quotes if q.provider_symbol == "EXR/D.USD.EUR.SP00.A"]
    assert [(q.observed_at.date().isoformat(), q.value) for q in usd] == [
        ("2026-11-11", Decimal("1.1127")),
        ("2026-11-12", Decimal("1.1081")),
        ("2026-11-13", Decimal("1.1052")),
    ]
    latest = usd[-1]
    assert latest.observation_type is ObservationType.RATE
    assert (latest.currency, latest.unit) == ("USD", "currency")
    assert latest.quote_kind is QuoteKind.REFERENCE
    assert latest.freshness is Freshness.END_OF_DAY
    assert latest.observed_at == datetime(2026, 11, 13, tzinfo=UTC)

    jpy = [q for q in quotes if q.provider_symbol == "EXR/D.JPY.EUR.SP00.A"]
    assert (jpy[-1].value, jpy[-1].currency) == (Decimal("176.83"), "JPY")
    assert {q.provider_symbol for q in quotes} == set(EXR)


async def test_all_currencies_of_a_dataflow_share_one_request() -> None:
    api = Api(EXR=recorded("exr_latest.json"))

    await provider(api).fetch_quotes(EXR)

    (request,) = api.requests
    assert request.url.path == "/v1/data/EXR/D.USD+GBP+JPY+CHF.EUR.SP00.A"
    assert dict(request.url.params) == {
        "format": "jsondata",
        "detail": "dataonly",
        "lastNObservations": "5",
    }


async def test_series_not_asked_for_are_ignored() -> None:
    api = Api(EXR=recorded("exr_latest.json"))

    quotes = await provider(api).fetch_quotes(["EXR/D.USD.EUR.SP00.A"])

    assert {q.provider_symbol for q in quotes} == {"EXR/D.USD.EUR.SP00.A"}


async def test_other_observation_types_need_no_request() -> None:
    api = Api()

    assert await provider(api).fetch_quotes(EXR, {ObservationType.PRICE}) == []
    assert api.requests == []


async def test_history_is_limited_to_the_requested_range() -> None:
    api = Api(EXR=recorded("exr_latest.json"))
    start, end = datetime(2026, 11, 12, tzinfo=UTC), datetime(2026, 11, 13, tzinfo=UTC)

    quotes = await provider(api).fetch_quote_history("EXR/D.USD.EUR.SP00.A", start, end)

    assert values(quotes, "EXR/D.USD.EUR.SP00.A") == [(start, Decimal("1.1081"))]
    # SDMX periods are inclusive, so the day before `end` is the last one asked for.
    assert dict(api.requests[0].url.params)["startPeriod"] == "2026-11-12"
    assert dict(api.requests[0].url.params)["endPeriod"] == "2026-11-12"


async def test_no_data_is_an_empty_result() -> None:
    api = Api(EXR=recorded("no_data.json"))

    quotes = await provider(api).fetch_quote_history(
        "EXR/D.USD.EUR.SP00.A",
        datetime(2026, 11, 14, tzinfo=UTC),
        datetime(2026, 11, 16, tzinfo=UTC),
    )

    assert quotes == []


# --- Government yields -------------------------------------------------------------------

YC_2Y = "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y"
YC_10Y = "YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"


async def test_yield_curve_spot_rates_are_yields_at_full_precision() -> None:
    api = Api(YC=recorded("yield_curve_latest.json"))

    quotes = await provider(api).fetch_quotes([YC_2Y, YC_10Y])

    assert values(quotes, YC_10Y) == [
        (datetime(2026, 11, 12, tzinfo=UTC), Decimal("3.3672459164")),
        (datetime(2026, 11, 13, tzinfo=UTC), Decimal("3.4115908883")),
    ]
    assert values(quotes, YC_2Y)[-1] == (
        datetime(2026, 11, 13, tzinfo=UTC),
        Decimal("3.0718048758"),
    )
    assert {q.observation_type for q in quotes} == {ObservationType.YIELD}
    assert {(q.unit, q.currency, q.quote_kind) for q in quotes} == {
        ("percent", None, QuoteKind.REFERENCE)
    }
    # Both maturities in one request.
    assert api.keys() == ["YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y+SR_10Y"]


async def test_observation_types_select_dataflows() -> None:
    api = Api(YC=recorded("yield_curve_latest.json"), EST=recorded("estr_latest.json"))

    yields = await provider(api).fetch_quotes([YC_10Y, ESTR], {ObservationType.YIELD})
    rates = await provider(api).fetch_quotes([YC_10Y, ESTR], {ObservationType.RATE})

    assert {q.provider_symbol for q in yields} == {YC_10Y}
    assert {q.provider_symbol for q in rates} == {ESTR}
    requested = api.keys()
    assert [key.split("/")[0] for key in requested] == ["YC", "EST"]


# --- Interest rates -----------------------------------------------------------------------


async def test_estr_is_a_percentage_reference_rate() -> None:
    api = Api(EST=recorded("estr_latest.json"))

    quotes = await provider(api).fetch_quotes([ESTR])

    latest = quotes[-1]
    assert (latest.observed_at, latest.value) == (
        datetime(2026, 11, 13, tzinfo=UTC),
        Decimal("2.437"),
    )
    assert (latest.unit, latest.currency) == ("percent", None)
    assert (latest.quote_kind, latest.freshness) == (QuoteKind.REFERENCE, Freshness.END_OF_DAY)


async def test_key_rates_are_stored_on_the_dates_they_changed() -> None:
    api = Api(FM=recorded("key_rates.json"))

    quotes = await provider(api).fetch_quotes([DFR, MRO])

    assert api.requests[0].url.path == "/v1/data/FM/B.U2.EUR.4F.KR.DFR+MRR_FR.LEV"
    deposit = values(quotes, DFR)
    assert deposit[-2:] == [
        (datetime(2026, 6, 17, tzinfo=UTC), Decimal("2.25")),
        (datetime(2026, 9, 16, tzinfo=UTC), Decimal("2.5")),
    ]
    assert values(quotes, MRO)[-1] == (datetime(2026, 9, 16, tzinfo=UTC), Decimal("2.65"))


async def test_each_dataflow_gets_its_own_request() -> None:
    api = Api(EXR=recorded("exr_latest.json"), EST=recorded("estr_latest.json"))

    await provider(api).fetch_quotes(["EXR/D.USD.EUR.SP00.A", ESTR])

    assert api.keys() == ["EXR/D.USD.EUR.SP00.A", "EST/B.EU000A2X2A25.WT"]


async def test_series_outside_the_quote_dataflows_is_refused_as_a_quote() -> None:
    api = Api()

    with pytest.raises(ProviderConfigurationError, match="EST, EXR, FM"):
        await provider(api).fetch_quotes(["ICP/M.U2.N.000000.4.ANR"])


@pytest.mark.parametrize(
    "symbol", ["EXR", "EXR/", "D.USD.EUR.SP00.A", "EXR/D.USD+GBP.EUR.SP00.A", "EXR/D..EUR"]
)
async def test_malformed_series_is_a_configuration_error(symbol: str) -> None:
    with pytest.raises(ProviderConfigurationError, match="<dataflow>/<full key>"):
        await provider(Api()).fetch_quotes([symbol])


# --- Economic indicators --------------------------------------------------------------------

LONG_AGO = datetime(2020, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 11, 16, tzinfo=UTC)


async def test_monthly_indicator_values() -> None:
    api = Api(LFSI=recorded("unemployment.json"))

    result = await provider(api).fetch_indicator("LFSI/M.I9.S.UNEHRT.TOTAL0.15_74.T", LONG_AGO, NOW)

    assert result[-1].observed_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert result[-1].value == Decimal("6.6")
    assert result[-1].series_code == "LFSI/M.I9.S.UNEHRT.TOTAL0.15_74.T"
    assert [v.observed_at for v in result] == sorted(v.observed_at for v in result)


async def test_quarterly_indicator_keeps_full_precision() -> None:
    api = Api(MNA=recorded("gdp.json"))

    result = await provider(api).fetch_indicator(
        "MNA/Q.Y.I9.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.GY", LONG_AGO, NOW
    )

    assert result[-1].observed_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert result[-1].value == Decimal("0.4889328315877739")


async def test_annual_indicator_values() -> None:
    api = Api(GFS=recorded("government_debt.json"))

    result = await provider(api).fetch_indicator(
        "GFS/A.N.I9.W0.S13.S1.C.L.LE.GD.T._Z.XDC_R_B1GQ._T.F.V.N._T", LONG_AGO, NOW
    )

    assert result[-2:][0].observed_at == datetime(2024, 1, 1, tzinfo=UTC)
    assert result[-1].value == Decimal("87.8062")


async def test_indicator_periods_outside_the_range_are_dropped() -> None:
    api = Api(ICP=recorded("hicp.json"))

    result = await provider(api).fetch_indicator(
        "ICP/M.U2.N.000000.4.ANR",
        datetime(2025, 3, 1, tzinfo=UTC),
        datetime(2025, 6, 1, tzinfo=UTC),
    )

    assert [v.observed_at.month for v in result] == [3, 4, 5]


async def test_missing_values_are_not_returned() -> None:
    message = json.loads((FIXTURES / "unemployment.json").read_bytes())
    series = next(iter(message["dataSets"][0]["series"].values()))
    first = min(series["observations"], key=int)
    series["observations"][first] = [None]
    api = Api(LFSI=httpx.Response(200, json=message))

    result = await provider(api).fetch_indicator("LFSI/M.I9.S.UNEHRT.TOTAL0.15_74.T", LONG_AGO, NOW)

    assert len(result) == len(series["observations"]) - 1


# --- Errors ----------------------------------------------------------------------------------


async def test_unknown_series_is_a_response_error() -> None:
    api = Api(ICP=recorded("unknown_series.json", status=404))

    with pytest.raises(ProviderResponseError, match="HTTP 404"):
        await provider(api).fetch_indicator("ICP/M.U2.Y.000000.3.ANR", LONG_AGO, NOW)


async def test_invalid_key_is_a_response_error() -> None:
    api = Api(EXR=recorded("bad_key.html", status=400))

    with pytest.raises(ProviderResponseError, match="HTTP 400"):
        await provider(api).fetch_quotes(["EXR/D.USD.EUR.SP00.A"])


async def test_malformed_message_is_a_response_error() -> None:
    api = Api(EXR=httpx.Response(200, json={"dataSets": []}))

    with pytest.raises(ProviderResponseError, match="structure"):
        await provider(api).fetch_quotes(["EXR/D.USD.EUR.SP00.A"])


async def test_series_position_outside_the_dimensions_is_a_response_error() -> None:
    message = json.loads((FIXTURES / "estr_latest.json").read_bytes())
    message["dataSets"][0]["series"] = {"0:7:0": {"observations": {"0": [1.0]}}}
    api = Api(EST=httpx.Response(200, json=message))

    with pytest.raises(ProviderResponseError, match="malformed series"):
        await provider(api).fetch_quotes([ESTR])


# --- Registration and seed -----------------------------------------------------------------


def test_adapter_is_registered_with_its_capabilities() -> None:
    assert ADAPTERS["ecb"] is EcbProvider
    assert EcbProvider.capabilities == {
        Capability.QUOTES,
        Capability.QUOTE_HISTORY,
        Capability.INDICATORS,
    }


def test_shipped_seed_configures_the_ecb(settings: Settings) -> None:
    seed = load_seed_file(settings.seed_file)
    (ecb,) = [p for p in seed.providers if p.name == "ecb"]
    assert ecb.configuration.auth.type == "none"

    tracked = {i.symbol: i for i in seed.instruments if "ecb" in i.providers}
    # Reference rates for the EUR pairs, stored on the same instruments as Twelve Data's.
    assert tracked["EUR/USD"].providers == {
        "twelve_data": "EUR/USD",
        "ecb": "EXR/D.USD.EUR.SP00.A",
    }
    for instrument in tracked.values():
        flow, key = instrument.providers["ecb"].split("/")
        if flow == "EXR":
            assert instrument.asset_type is AssetType.FOREX
            assert key.split(".")[1] == instrument.currency == instrument.symbol.split("/")[1]
        elif flow == "YC":
            assert instrument.asset_type is AssetType.GOVERNMENT_BOND_YIELD
            assert instrument.metadata is not None
            assert key.endswith(f"SR_{instrument.metadata['maturity']}")
        else:
            assert instrument.asset_type is AssetType.INTEREST_RATE
            assert instrument.currency is None

    indicators = [i for i in seed.indicators if i.provider == "ecb"]
    assert {i.frequency for i in indicators} >= {
        IndicatorFrequency.MONTHLY,
        IndicatorFrequency.QUARTERLY,
        IndicatorFrequency.ANNUAL,
    }
    assert all("/" in i.provider_series_code for i in indicators)
