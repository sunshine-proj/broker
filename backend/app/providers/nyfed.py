"""Federal Reserve Bank of New York adapter: the US reference rates it administers.

Provider symbols are the rate names the New York Fed uses: SOFR, EFFR, OBFR,
TGCR and BGCR. They are published once per business day, in percent, from the
New York Fed's Markets Data API. No key is needed. API facts this adapter
relies on are recorded in docs/providers/nyfed.md.
"""

from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, field_validator

from app.models.enums import Freshness, ObservationType, QuoteKind
from app.providers.base import Capability, Provider
from app.providers.errors import ProviderConfigurationError
from app.providers.factory import register_adapter
from app.providers.parsing import parse_decimal, parse_utc_date
from app.schemas.normalized import Quote

# Rates published per business day, by the API's path group.
_GROUPS = {
    "SOFR": "secured",
    "TGCR": "secured",
    "BGCR": "secured",
    "EFFR": "unsecured",
    "OBFR": "unsecured",
}

# Business days requested when collecting current values. More than one, so
# that a missed collection day is filled in on the next run.
_LATEST_OBSERVATIONS = 5

# Fields kept with each value: the distribution and volume behind the rate.
_DETAIL_FIELDS = (
    "percentPercentile1",
    "percentPercentile25",
    "percentPercentile75",
    "percentPercentile99",
    "volumeInBillions",
    "targetRateFrom",
    "targetRateTo",
)


class _Rate(BaseModel):
    effectiveDate: datetime  # noqa: N815 (the API's field names)
    type: str
    percentRate: Decimal  # noqa: N815
    revisionIndicator: str | None = None  # noqa: N815

    model_config = {"extra": "allow"}

    _date = field_validator("effectiveDate", mode="before")(parse_utc_date)
    _rate = field_validator("percentRate", mode="before")(parse_decimal)


class _Rates(BaseModel):
    refRates: list[_Rate]  # noqa: N815


@register_adapter
class NyFedProvider(Provider):
    name = "nyfed"
    capabilities = frozenset({Capability.QUOTES, Capability.QUOTE_HISTORY})

    async def fetch_quotes(
        self,
        symbols: Sequence[str],
        observation_types: Collection[ObservationType] | None = None,
    ) -> list[Quote]:
        """The latest published values of each rate, one request per rate."""
        if observation_types is not None and ObservationType.RATE not in observation_types:
            return []
        quotes: list[Quote] = []
        for symbol in symbols:
            path = f"{self._path(symbol)}/last/{_LATEST_OBSERVATIONS}.json"
            quotes += await self._quotes(symbol, path, {})
        return quotes

    async def fetch_quote_history(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        last = (end - timedelta(microseconds=1)).astimezone(UTC).date()
        quotes = await self._quotes(
            symbol,
            f"{self._path(symbol)}/search.json",
            {"startDate": start.astimezone(UTC).date().isoformat(), "endDate": last.isoformat()},
        )
        return [quote for quote in quotes if start <= quote.observed_at < end]

    async def _quotes(self, symbol: str, path: str, params: dict[str, Any]) -> list[Quote]:
        raw = await self._client.get_json(path, params)
        rates = self._validate(_Rates, raw).refRates
        return sorted(
            (self._to_quote(symbol, rate) for rate in rates if rate.type.upper() == symbol),
            key=lambda quote: quote.observed_at,
        )

    def _to_quote(self, symbol: str, rate: _Rate) -> Quote:
        extra = rate.model_extra or {}
        details = {
            field: str(extra[field]) for field in _DETAIL_FIELDS if extra.get(field) is not None
        }
        if rate.revisionIndicator:
            details["revisionIndicator"] = rate.revisionIndicator
        return Quote(
            provider_symbol=symbol,
            observation_type=ObservationType.RATE,
            # The business day the rate applies to, not the publication time.
            observed_at=rate.effectiveDate,
            value=rate.percentRate,
            unit="percent",
            freshness=Freshness.END_OF_DAY,
            # Benchmark rates computed from transactions, not a tradable quote.
            quote_kind=QuoteKind.REFERENCE,
            metadata=details or None,
        )

    def _path(self, symbol: str) -> str:
        group = _GROUPS.get(symbol)
        if group is None:
            raise ProviderConfigurationError(
                self.name, f"unknown rate {symbol!r}; supported: {', '.join(sorted(_GROUPS))}"
            )
        return f"/rates/{group}/{symbol.lower()}"
