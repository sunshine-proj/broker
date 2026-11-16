"""Helpers for turning raw provider values into normalized ones.

They raise ValueError; inside a Pydantic validator that becomes a validation
error, which `Provider._validate` reports as a ProviderResponseError.
"""

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation


def parse_decimal(value: object) -> Decimal:
    """Exact decimal from a string, int or Decimal.

    Floats are refused: they have already lost precision. The shared HTTP
    client decodes JSON numbers as Decimal, so an adapter never needs one.
    """
    # bool is checked first because it is a subclass of int.
    if isinstance(value, (bool, float)):
        raise ValueError(f"cannot take a decimal from {type(value).__name__}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError("not a decimal number") from exc
    else:
        raise ValueError(f"cannot take a decimal from {type(value).__name__}")
    if not result.is_finite():
        raise ValueError("not a finite number")
    return result


def parse_epoch_seconds(value: int | Decimal | str) -> datetime:
    return datetime.fromtimestamp(float(parse_decimal(value)), UTC)


def parse_epoch_milliseconds(value: int | Decimal | str) -> datetime:
    return datetime.fromtimestamp(float(parse_decimal(value) / 1000), UTC)


def parse_utc_datetime(value: str) -> datetime:
    """An ISO 8601 timestamp, converted to UTC.

    A timestamp without an offset is refused: guessing its timezone would
    silently shift the observation. Use `assume_utc` when the provider
    documents that its timestamps are UTC.
    """
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(UTC)


def assume_utc(value: str) -> datetime:
    """An ISO 8601 timestamp the provider documents as UTC but sends without an offset."""
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_utc_date(value: str) -> datetime:
    """A date-only value (YYYY-MM-DD) becomes midnight UTC of that date."""
    day = date.fromisoformat(value.strip())
    return datetime(day.year, day.month, day.day, tzinfo=UTC)
