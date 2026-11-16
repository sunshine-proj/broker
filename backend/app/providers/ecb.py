"""European Central Bank adapter, over the ECB Data Portal's SDMX API.

Provider symbols and series codes are SDMX series: `<dataflow>/<key>`, for
example `EXR/D.USD.EUR.SP00.A` (US dollars per euro, daily reference rate).
API facts this adapter relies on are recorded in docs/providers/ecb.md.
"""

import logging
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from app.models.enums import Freshness, ObservationType, QuoteKind
from app.providers.base import Capability, Provider
from app.providers.errors import ProviderConfigurationError, ProviderResponseError
from app.providers.factory import register_adapter
from app.providers.parsing import parse_decimal
from app.schemas.normalized import IndicatorValue, Quote

logger = logging.getLogger(__name__)

# Observations requested per series when collecting current values. More than
# one, so that a missed collection day is filled in on the next run.
_LATEST_OBSERVATIONS = 5


@dataclass(frozen=True)
class _QuoteFlow:
    """How the values of a dataflow are stored as quotes."""

    # "currency": exchange rates, quoted in the series' CURRENCY. "percent": rates.
    unit: str


# Dataflows whose series are stored as instrument quotes. Anything else is
# collected as an economic indicator.
_QUOTE_FLOWS = {
    # Euro foreign exchange reference rates: units of CURRENCY per euro.
    "EXR": _QuoteFlow(unit="currency"),
    # Euro short-term rate (€STR).
    "EST": _QuoteFlow(unit="percent"),
    # Financial market data, including the ECB's key interest rates.
    "FM": _QuoteFlow(unit="percent"),
}


class _DimensionValue(BaseModel):
    id: str


class _Dimension(BaseModel):
    id: str
    values: list[_DimensionValue]


class _Dimensions(BaseModel):
    series: list[_Dimension]
    observation: list[_Dimension]


class _Structure(BaseModel):
    dimensions: _Dimensions


class _Series(BaseModel):
    # Observation index -> [value, attribute indices...]; value may be null.
    observations: dict[str, list[Any]]


class _DataSet(BaseModel):
    series: dict[str, _Series] = {}


class _Message(BaseModel):
    """An SDMX-JSON data message (format=jsondata)."""

    structure: _Structure
    dataSets: list[_DataSet]  # noqa: N815 (the API's field name)


@dataclass(frozen=True)
class _Observation:
    period: datetime
    value: Decimal


def parse_period(period: str) -> datetime:
    """Start of an SDMX time period, at midnight UTC.

    Daily `2026-11-13`, monthly `2026-09`, quarterly `2026-Q1`, half-yearly
    `2026-S1` and annual `2025` are supported.
    """
    text = period.strip()
    if len(text) == 4 and text.isdigit():
        return datetime(int(text), 1, 1, tzinfo=UTC)
    year, separator, rest = text.partition("-")
    if not separator or len(year) != 4 or not year.isdigit():
        raise ValueError(f"unsupported period {period!r}")
    if rest.startswith("Q") and rest[1:] in {"1", "2", "3", "4"}:
        return datetime(int(year), 3 * int(rest[1:]) - 2, 1, tzinfo=UTC)
    if rest.startswith("S") and rest[1:] in {"1", "2"}:
        return datetime(int(year), 6 * int(rest[1:]) - 5, 1, tzinfo=UTC)
    if len(rest) == 2 and rest.isdigit():
        return datetime(int(year), int(rest), 1, tzinfo=UTC)
    month, separator, day = rest.partition("-")
    if not (separator and len(month) == 2 and len(day) == 2 and (month + day).isdigit()):
        raise ValueError(f"unsupported period {period!r}")
    return datetime(int(year), int(month), int(day), tzinfo=UTC)


def _split(provider: str, series: str) -> tuple[str, list[str]]:
    flow, separator, key = series.partition("/")
    parts = key.split(".")
    if not separator or not flow or not key or any(not part or "+" in part for part in parts):
        raise ProviderConfigurationError(
            provider, f"series {series!r} must have the form <dataflow>/<full key>"
        )
    return flow, parts


def _combined_key(keys: list[list[str]]) -> str:
    """One SDMX key matching every given key, using the OR operator per position.

    It can match more series than were asked for; those are filtered out.
    """
    positions = []
    for values in zip(*keys, strict=True):
        positions.append("+".join(dict.fromkeys(values)))
    return ".".join(positions)


@register_adapter
class EcbProvider(Provider):
    name = "ecb"
    capabilities = frozenset({Capability.QUOTES, Capability.QUOTE_HISTORY, Capability.INDICATORS})

    async def fetch_quotes(
        self,
        symbols: Sequence[str],
        observation_types: Collection[ObservationType] | None = None,
    ) -> list[Quote]:
        """The latest published values of each series.

        The last few observations are taken, so that a day missed by an
        earlier collection is filled in; values already stored are not
        written again. One request per dataflow.
        """
        if observation_types is not None and ObservationType.RATE not in observation_types:
            return []
        return await self._quotes(symbols, {"lastNObservations": _LATEST_OBSERVATIONS})

    async def fetch_quote_history(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        quotes = await self._quotes([symbol], _period_params(start, end))
        return [quote for quote in quotes if start <= quote.observed_at < end]

    async def fetch_indicator(
        self, series_code: str, start: datetime, end: datetime
    ) -> list[IndicatorValue]:
        """Values for periods starting within start <= period < end.

        A period is dated by its first day: September 2026 is 2026-09-01.
        """
        flow, key = _split(self.name, series_code)
        series = await self._fetch(flow, [key], _period_params(start, end))
        return [
            IndicatorValue(
                series_code=series_code, observed_at=observation.period, value=observation.value
            )
            for observation in series.get(".".join(key), [])
            if start <= observation.period < end
        ]

    async def _quotes(self, symbols: Sequence[str], params: dict[str, Any]) -> list[Quote]:
        by_flow: dict[tuple[str, int], list[tuple[str, list[str]]]] = defaultdict(list)
        for symbol in symbols:
            flow, key = _split(self.name, symbol)
            if flow not in _QUOTE_FLOWS:
                raise ProviderConfigurationError(
                    self.name,
                    f"series {symbol!r} cannot be collected as quotes; "
                    f"quotes are supported for dataflows {', '.join(sorted(_QUOTE_FLOWS))}",
                )
            # Keys of different lengths cannot share one request.
            by_flow[(flow, len(key))].append((symbol, key))

        quotes: list[Quote] = []
        for (flow, _), entries in by_flow.items():
            series = await self._fetch(flow, [key for _, key in entries], params)
            for symbol, key in entries:
                observations = series.get(".".join(key))
                if observations is None:
                    logger.warning(
                        "No observations returned for a series",
                        extra={"provider": self.name, "symbol": symbol},
                    )
                    continue
                quotes += [self._to_quote(symbol, flow, key, obs) for obs in observations]
        return quotes

    def _to_quote(self, symbol: str, flow: str, key: list[str], observation: _Observation) -> Quote:
        if _QUOTE_FLOWS[flow].unit == "currency":
            # EXR keys are FREQ.CURRENCY.CURRENCY_DENOM.EXR_TYPE.EXR_SUFFIX.
            currency, unit = key[1], "currency"
        else:
            currency, unit = None, "percent"
        return Quote(
            provider_symbol=symbol,
            observation_type=ObservationType.RATE,
            observed_at=observation.period,
            value=observation.value,
            currency=currency,
            unit=unit,
            freshness=Freshness.END_OF_DAY,
            # Official rates published for information, not prices to trade at.
            quote_kind=QuoteKind.REFERENCE,
        )

    async def _fetch(
        self, flow: str, keys: list[list[str]], params: dict[str, Any]
    ) -> dict[str, list[_Observation]]:
        """Observations of every series matching `keys`, by full series key, oldest first."""
        raw = await self._client.get_json(
            f"/data/{flow}/{_combined_key(keys)}",
            {"format": "jsondata", "detail": "dataonly", **params},
            allow_empty=True,
        )
        if raw is None:
            # The ECB answers a query that matches no observations with an empty body.
            return {}
        message = self._validate(_Message, raw)
        return self._observations(message)

    def _observations(self, message: _Message) -> dict[str, list[_Observation]]:
        dimensions = message.structure.dimensions
        if len(dimensions.observation) != 1:
            raise ProviderResponseError(self.name, "expected one observation dimension")
        periods = dimensions.observation[0].values
        result: dict[str, list[_Observation]] = {}
        for data_set in message.dataSets:
            for position, series in data_set.series.items():
                try:
                    indices = [int(index) for index in position.split(":")]
                    key = ".".join(
                        dimension.values[index].id
                        for dimension, index in zip(dimensions.series, indices, strict=True)
                    )
                    observations = [
                        _Observation(parse_period(periods[int(index)].id), parse_decimal(value[0]))
                        for index, value in series.observations.items()
                        # A missing value is sent as null; nothing is stored for it.
                        if value and value[0] is not None
                    ]
                except (ValueError, IndexError) as exc:
                    raise ProviderResponseError(
                        self.name, f"malformed series in response ({type(exc).__name__})"
                    ) from exc
                result[key] = sorted(observations, key=lambda o: o.period)
        return result


def _period_params(start: datetime, end: datetime) -> dict[str, str]:
    """startPeriod and endPeriod as dates; both are inclusive in SDMX."""
    last = (end - timedelta(microseconds=1)).astimezone(UTC).date()
    return {"startPeriod": start.astimezone(UTC).date().isoformat(), "endPeriod": last.isoformat()}
