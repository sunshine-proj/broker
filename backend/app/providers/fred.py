"""FRED adapter (Federal Reserve Bank of St. Louis): US Treasury yields, the
federal funds rate and US economic indicators.

Provider symbols and series codes are FRED series ids, for example `DGS10`.
API facts this adapter relies on are recorded in docs/providers/fred.md.
"""

import logging
import re
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, field_validator

from app.models.enums import Freshness, ObservationType, QuoteKind
from app.providers.base import Capability, Provider
from app.providers.errors import ProviderAuthError, ProviderResponseError
from app.providers.factory import register_adapter
from app.providers.parsing import parse_decimal, parse_utc_date
from app.schemas.normalized import IndicatorValue, Quote, SymbolMatch

logger = logging.getLogger(__name__)

# Observations requested per series when collecting current values. More than
# one, so that a missed collection day is filled in on the next run.
_LATEST_OBSERVATIONS = 5

# FRED's marker for a date without a value, such as a market holiday.
_MISSING = {".", ""}

# Treasury constant maturity yields (DGS1MO ... DGS30) are yields; every other
# series collected as a quote is a rate.
_YIELD_SERIES = re.compile(r"^DGS\d+(MO)?$")


def _value(value: object) -> Decimal | None:
    if isinstance(value, str) and value.strip() in _MISSING:
        return None
    return parse_decimal(value)


class _Observation(BaseModel):
    date: datetime
    value: Decimal | None
    # First day this value was the published one (its vintage).
    realtime_start: str

    _date = field_validator("date", mode="before")(parse_utc_date)
    _value = field_validator("value", mode="before")(_value)


class _Observations(BaseModel):
    observations: list[_Observation]


class _SearchResult(BaseModel):
    id: str
    title: str
    frequency: str | None = None
    units: str | None = None


class _Search(BaseModel):
    seriess: list[_SearchResult]


def _period_params(start: datetime, end: datetime) -> dict[str, str]:
    """observation_start and observation_end as dates; FRED includes both."""
    last = (end - timedelta(microseconds=1)).astimezone(UTC).date()
    return {
        "observation_start": start.astimezone(UTC).date().isoformat(),
        "observation_end": last.isoformat(),
    }


@register_adapter
class FredProvider(Provider):
    name = "fred"
    capabilities = frozenset(
        {
            Capability.QUOTES,
            Capability.QUOTE_HISTORY,
            Capability.INDICATORS,
            Capability.SYMBOL_SEARCH,
        }
    )

    async def fetch_quotes(
        self,
        symbols: Sequence[str],
        observation_types: Collection[ObservationType] | None = None,
    ) -> list[Quote]:
        """The latest published values of each series, one request per series.

        The last few observations are taken, so that a day missed by an
        earlier collection is filled in; values already stored are not
        written again.
        """
        quotes: list[Quote] = []
        for symbol in symbols:
            observation_type = self._observation_type(symbol)
            if observation_types is not None and observation_type not in observation_types:
                continue
            observations = await self._observations(
                symbol, {"sort_order": "desc", "limit": _LATEST_OBSERVATIONS}
            )
            quotes += [self._to_quote(symbol, observation_type, o) for o in observations]
        return quotes

    async def fetch_quote_history(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        observation_type = self._observation_type(symbol)
        observations = await self._observations(symbol, _period_params(start, end))
        return [
            self._to_quote(symbol, observation_type, o)
            for o in observations
            if start <= o.date < end
        ]

    async def fetch_indicator(
        self, series_code: str, start: datetime, end: datetime
    ) -> list[IndicatorValue]:
        """Current values for periods starting within start <= period < end.

        FRED dates monthly and quarterly values by the first day of the period.
        Each value records the date it was first published in that form
        (`realtime_start`), so a later revision can be told apart.
        """
        observations = await self._observations(series_code, _period_params(start, end))
        return [
            IndicatorValue(
                series_code=series_code,
                observed_at=o.date,
                value=o.value,
                metadata={"realtime_start": o.realtime_start},
            )
            for o in observations
            if o.value is not None and start <= o.date < end
        ]

    async def search_symbols(self, query: str) -> list[SymbolMatch]:
        raw = await self._request(
            "/series/search", {"search_text": query, "limit": 30, "order_by": "popularity"}
        )
        search = self._validate(_Search, raw)
        return [
            SymbolMatch(
                provider_symbol=series.id,
                name=series.title,
                instrument_type=", ".join(p for p in (series.frequency, series.units) if p) or None,
                country="US",
            )
            for series in search.seriess
        ]

    @staticmethod
    def _observation_type(symbol: str) -> ObservationType:
        return ObservationType.YIELD if _YIELD_SERIES.match(symbol) else ObservationType.RATE

    def _to_quote(
        self, symbol: str, observation_type: ObservationType, observation: _Observation
    ) -> Quote:
        assert observation.value is not None
        return Quote(
            provider_symbol=symbol,
            observation_type=observation_type,
            observed_at=observation.date,
            value=observation.value,
            unit="percent",
            freshness=Freshness.END_OF_DAY,
            # Official published rates and yields, not prices anyone can trade at.
            quote_kind=QuoteKind.REFERENCE,
        )

    async def _observations(self, series_id: str, params: dict[str, Any]) -> list[_Observation]:
        """Observations of one series, oldest first, without missing values."""
        raw = await self._request("/series/observations", {"series_id": series_id, **params})
        parsed = self._validate(_Observations, raw)
        present = [o for o in parsed.observations if o.value is not None]
        return sorted(present, key=lambda o: o.date)

    async def _request(self, path: str, params: dict[str, Any]) -> Any:
        try:
            return await self._client.get_json(path, {"file_type": "json", **params})
        except ProviderResponseError as exc:
            # FRED answers an unregistered key with HTTP 400, not 401.
            if "api_key" in _error_message(exc.body):
                raise ProviderAuthError(self.name, "credential rejected (HTTP 400)") from exc
            raise


def _error_message(body: object) -> str:
    if isinstance(body, dict):
        return str(body.get("error_message", ""))
    return ""
