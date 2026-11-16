"""Ingestion validation (PRD FR-002).

Each check returns the reason an observation must be rejected, or None if it
is acceptable. Observations are never corrected.

The normalized schemas already guarantee that timestamps are timezone-aware
UTC and that numbers are finite decimals, so those are not checked again here.
"""

from datetime import datetime, timedelta

from app.models.enums import ObservationType
from app.schemas.normalized import Candle, IndicatorValue, Quote

# Allowance for clock differences between us and a provider.
FUTURE_TOLERANCE = timedelta(minutes=5)

# Observation types that are amounts of money and so must state their currency.
_MONETARY_TYPES = frozenset(
    {ObservationType.PRICE, ObservationType.NAV, ObservationType.MARKET_CAP}
)


def _timestamp_problem(timestamp: datetime, now: datetime) -> str | None:
    if timestamp > now + FUTURE_TOLERANCE:
        return f"timestamp {timestamp.isoformat()} is in the future"
    return None


def _currency_problem(currency: str | None, instrument_currency: str | None) -> str | None:
    if currency is not None and instrument_currency is not None and currency != instrument_currency:
        return f"currency {currency} does not match the instrument's {instrument_currency}"
    return None


def validate_quote(quote: Quote, instrument_currency: str | None, now: datetime) -> str | None:
    if quote.observation_type in _MONETARY_TYPES and quote.currency is None:
        return f"{quote.observation_type.value} has no currency"
    return _timestamp_problem(quote.observed_at, now) or _currency_problem(
        quote.currency, instrument_currency
    )


def validate_candle(candle: Candle, instrument_currency: str | None, now: datetime) -> str | None:
    if candle.high < max(candle.open, candle.close, candle.low):
        return "high is below another price of the candle"
    if candle.low > min(candle.open, candle.close):
        return "low is above another price of the candle"
    if candle.volume is not None and candle.volume < 0:
        return "volume is negative"
    return _timestamp_problem(candle.timestamp, now) or _currency_problem(
        candle.currency, instrument_currency
    )


def validate_indicator_value(value: IndicatorValue, now: datetime) -> str | None:
    return _timestamp_problem(value.observed_at, now)
