"""Time-series tables. Both are TimescaleDB hypertables.

Their primary keys are natural keys that include the time column, as
TimescaleDB requires; there is no surrogate id.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import (
    CandleInterval,
    Freshness,
    ObservationType,
    QualityStatus,
    QuoteKind,
    enum_type,
)


class MarketObservation(Base):
    __tablename__ = "market_observation"
    __table_args__ = (
        # Serves history and latest-value queries across providers.
        sa.Index(
            "ix_market_observation_instrument_type_time",
            "instrument_id",
            "observation_type",
            "observed_at",
        ),
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("instrument.id"), primary_key=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("provider.id"), primary_key=True)
    observation_type: Mapped[ObservationType] = mapped_column(
        enum_type(ObservationType, "observation_type"), primary_key=True
    )
    observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), primary_key=True)
    ingested_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    value: Mapped[Decimal] = mapped_column(sa.Numeric)
    currency: Mapped[str | None] = mapped_column(sa.String(10))
    unit: Mapped[str] = mapped_column(sa.String(32))
    freshness: Mapped[Freshness] = mapped_column(enum_type(Freshness, "freshness"))
    quote_kind: Mapped[QuoteKind] = mapped_column(enum_type(QuoteKind, "quote_kind"))
    quality_status: Mapped[QualityStatus] = mapped_column(
        enum_type(QualityStatus, "quality_status"), server_default=QualityStatus.OK.value
    )
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB(none_as_null=True))


class OHLCV(Base):
    """A closed market candle."""

    __tablename__ = "ohlcv"
    __table_args__ = (
        sa.Index("ix_ohlcv_instrument_interval_time", "instrument_id", "interval", "timestamp"),
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("instrument.id"), primary_key=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("provider.id"), primary_key=True)
    interval: Mapped[CandleInterval] = mapped_column(
        enum_type(CandleInterval, "interval", length=8), primary_key=True
    )
    # Candle open time.
    timestamp: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(sa.Numeric)
    high: Mapped[Decimal] = mapped_column(sa.Numeric)
    low: Mapped[Decimal] = mapped_column(sa.Numeric)
    close: Mapped[Decimal] = mapped_column(sa.Numeric)
    # Null where the market reports none, for example forex.
    volume: Mapped[Decimal | None] = mapped_column(sa.Numeric)
    currency: Mapped[str | None] = mapped_column(sa.String(10))
    ingested_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
