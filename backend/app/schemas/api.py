"""Response models of the read API (PRD FR-006).

Decimals are serialized as JSON strings, so no precision is lost, and
timestamps as ISO 8601 in UTC with a `Z` suffix.
"""

import uuid
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer

from app.models.enums import (
    AssetType,
    CandleInterval,
    CollectorStatus,
    ExecutionTrigger,
    Freshness,
    IndicatorFrequency,
    IssueKind,
    IssueResolution,
    IssueStatus,
    JobKind,
    ObservationType,
    ProviderHealthStatus,
    QualityStatus,
    QuoteKind,
)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal(value: Decimal) -> str:
    # str() of a Decimal keeps every stored digit, and never uses floats.
    return str(value)


UtcTimestamp = Annotated[datetime, PlainSerializer(_utc, return_type=str)]
DecimalString = Annotated[Decimal, PlainSerializer(_decimal, return_type=str)]


class _Response(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    """A page of a list. Ask for the next page with offset = offset + limit."""

    items: list[T]
    limit: int
    offset: int
    has_more: bool


# --- Instruments -------------------------------------------------------------------------


class ProviderMappingOut(_Response):
    provider: str
    provider_symbol: str
    enabled: bool


class InstrumentOut(_Response):
    id: uuid.UUID
    symbol: str
    name: str
    asset_type: AssetType
    exchange: str | None
    currency: str | None
    enabled: bool
    metadata: dict[str, Any]
    providers: list[ProviderMappingOut]


# --- Market observations ------------------------------------------------------------------


class ObservationOut(_Response):
    """One value of one instrument from one provider."""

    instrument_id: uuid.UUID
    symbol: str
    asset_type: AssetType
    provider: str
    observation_type: ObservationType
    observed_at: UtcTimestamp
    ingested_at: UtcTimestamp
    value: DecimalString
    currency: str | None
    unit: str
    freshness: Freshness
    quote_kind: QuoteKind
    quality_status: QualityStatus


class ForexRateOut(ObservationOut):
    base_currency: str
    quote_currency: str


class BondYieldOut(ObservationOut):
    country: str | None
    maturity: str | None


class CandleOut(_Response):
    instrument_id: uuid.UUID
    provider: str
    interval: CandleInterval
    timestamp: UtcTimestamp
    open: DecimalString
    high: DecimalString
    low: DecimalString
    close: DecimalString
    volume: DecimalString | None
    currency: str | None
    ingested_at: UtcTimestamp


# --- Economic indicators -------------------------------------------------------------------


class IndicatorValueOut(_Response):
    observed_at: UtcTimestamp
    ingested_at: UtcTimestamp
    value: DecimalString
    metadata: dict[str, Any] | None


class IndicatorOut(_Response):
    code: str
    name: str
    country: str | None
    unit: str
    frequency: IndicatorFrequency
    provider: str
    provider_series_code: str
    enabled: bool
    metadata: dict[str, Any]
    # The current value of the most recent period, if any is stored.
    latest: IndicatorValueOut | None


# --- Providers and collection --------------------------------------------------------------


class RateLimitOut(BaseModel):
    limit: int
    period: str


class ProviderOut(_Response):
    name: str
    base_url: str
    enabled: bool
    # Configured limits only; no other configuration, and never credentials.
    rate_limits: list[RateLimitOut]
    health: ProviderHealthStatus | None
    last_success_at: UtcTimestamp | None
    last_error_at: UtcTimestamp | None
    last_error: str | None
    consecutive_failures: int


class JobStatusOut(_Response):
    name: str
    # None for a quality job, which checks every provider's data.
    provider: str | None
    kind: JobKind
    enabled: bool
    interval_minutes: int | None
    daily_at: time | None
    market_hours_only: bool
    retry_minutes: int | None
    settings: dict[str, Any]
    last_started_at: UtcTimestamp | None
    last_finished_at: UtcTimestamp | None
    last_status: CollectorStatus | None
    last_error: str | None
    next_run_at: UtcTimestamp | None


class ExecutionOut(_Response):
    id: uuid.UUID
    job_name: str
    provider: str | None
    trigger: ExecutionTrigger
    status: CollectorStatus
    started_at: UtcTimestamp
    finished_at: UtcTimestamp | None
    records_written: int
    error: str | None


class QualityIssueOut(_Response):
    """A data-quality issue (PRD FR-012)."""

    id: uuid.UUID
    kind: IssueKind
    status: IssueStatus
    resolution: IssueResolution | None
    # The instrument, providers and series concerned, where the issue has them.
    symbol: str | None
    asset_type: AssetType | None
    provider: str | None
    other_provider: str | None
    observation_type: ObservationType | None
    interval: CandleInterval | None
    # The period affected: a gap's missing range, a stale series' last value.
    range_start: UtcTimestamp | None
    range_end: UtcTimestamp | None
    message: str
    details: dict[str, Any]
    detected_at: UtcTimestamp
    last_seen_at: UtcTimestamp
    closed_at: UtcTimestamp | None
