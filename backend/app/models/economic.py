"""Economic indicators and their observations."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import IndicatorFrequency, enum_type


class EconomicIndicator(Base):
    """An economic series from one provider."""

    __tablename__ = "economic_indicator"
    __table_args__ = (
        sa.UniqueConstraint(
            "provider_id", "provider_series_code", name="uq_economic_indicator_provider_series"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(sa.String(64), unique=True)
    name: Mapped[str] = mapped_column(sa.String(255))
    country: Mapped[str | None] = mapped_column(sa.String(2))
    unit: Mapped[str] = mapped_column(sa.String(32))
    frequency: Mapped[IndicatorFrequency] = mapped_column(
        enum_type(IndicatorFrequency, "frequency")
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("provider.id"))
    provider_series_code: Mapped[str] = mapped_column(sa.String(128))
    enabled: Mapped[bool] = mapped_column(server_default=sa.true())
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=sa.text("'{}'::jsonb")
    )


class EconomicObservation(Base):
    """One published value for one period.

    A revision is a further row for the same period with a later `ingested_at`;
    the current value is the row with the latest `ingested_at`.
    """

    __tablename__ = "economic_observation"

    indicator_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("economic_indicator.id"), primary_key=True
    )
    # The period the value refers to.
    observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), primary_key=True)
    ingested_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), primary_key=True)
    value: Mapped[Decimal] = mapped_column(sa.Numeric)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB(none_as_null=True))
