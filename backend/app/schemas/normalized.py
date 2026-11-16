"""Common normalized schemas: what every provider adapter returns.

Adapters identify instruments by the provider's own symbol. The ingestion
service resolves that to an instrument, validates the value and stores it.
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.enums import CandleInterval, Freshness, ObservationType, QuoteKind
from app.schemas.market_data import UtcDatetime


class _Normalized(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Quote(_Normalized):
    """One value quoted for an instrument at a point in time."""

    provider_symbol: str
    observation_type: ObservationType
    # The provider's own timestamp for the value.
    observed_at: UtcDatetime
    value: Decimal
    currency: str | None = None
    # For example "currency", "percent", "points", "count".
    unit: str
    freshness: Freshness
    quote_kind: QuoteKind
    metadata: dict[str, Any] | None = None


class Candle(_Normalized):
    """A closed candle."""

    provider_symbol: str
    interval: CandleInterval
    # Candle open time.
    timestamp: UtcDatetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    currency: str | None = None


class SymbolMatch(_Normalized):
    """One result of a provider's symbol search."""

    provider_symbol: str
    name: str
    instrument_type: str | None = None
    # Market identifier code of the listing venue, where the provider gives one.
    exchange: str | None = None
    country: str | None = None
    currency: str | None = None


class IndicatorValue(_Normalized):
    """One published value of an economic series for one period."""

    series_code: str
    # The period the value refers to.
    observed_at: UtcDatetime
    value: Decimal
    metadata: dict[str, Any] | None = None
