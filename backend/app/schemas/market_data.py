"""Normalized observations, as handed to the repositories for storage."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict

from app.models.enums import CandleInterval, Freshness, ObservationType, QualityStatus, QuoteKind


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Rejects naive datetimes and converts aware ones to UTC.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


class _Normalized(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MarketObservationIn(_Normalized):
    instrument_id: uuid.UUID
    provider_id: uuid.UUID
    observation_type: ObservationType
    observed_at: UtcDatetime
    value: Decimal
    currency: str | None = None
    unit: str
    freshness: Freshness
    quote_kind: QuoteKind
    quality_status: QualityStatus = QualityStatus.OK
    metadata: dict[str, Any] | None = None


class OHLCVIn(_Normalized):
    instrument_id: uuid.UUID
    provider_id: uuid.UUID
    interval: CandleInterval
    timestamp: UtcDatetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    currency: str | None = None


class EconomicObservationIn(_Normalized):
    indicator_id: uuid.UUID
    observed_at: UtcDatetime
    value: Decimal
    metadata: dict[str, Any] | None = None
