"""Reference entities: exchanges, instruments, providers and their mappings."""

import uuid
from datetime import datetime, time
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import AssetType, enum_type

_UUID_DEFAULT = sa.text("gen_random_uuid()")
_EMPTY_JSON = sa.text("'{}'::jsonb")


class Exchange(Base):
    __tablename__ = "exchange"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    code: Mapped[str] = mapped_column(sa.String(16), unique=True)
    name: Mapped[str] = mapped_column(sa.String(255))
    country: Mapped[str | None] = mapped_column(sa.String(2))
    timezone: Mapped[str] = mapped_column(sa.String(64))
    # Regular session, in the exchange's local time, Monday to Friday.
    # Null where the session is not configured. Holidays are not modelled.
    trading_open: Mapped[time | None] = mapped_column(sa.Time)
    trading_close: Mapped[time | None] = mapped_column(sa.Time)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class Instrument(Base):
    __tablename__ = "instrument"
    __table_args__ = (
        # Instruments without a venue (forex, crypto) have a null exchange; two
        # such rows with the same type and symbol are still duplicates.
        sa.UniqueConstraint(
            "asset_type",
            "symbol",
            "exchange_id",
            name="uq_instrument_identity",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    symbol: Mapped[str] = mapped_column(sa.String(64))
    name: Mapped[str] = mapped_column(sa.String(255))
    asset_type: Mapped[AssetType] = mapped_column(enum_type(AssetType, "asset_type"))
    exchange_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("exchange.id"))
    currency: Mapped[str | None] = mapped_column(sa.String(10))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default=_EMPTY_JSON)
    enabled: Mapped[bool] = mapped_column(server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class Provider(Base):
    __tablename__ = "provider"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=_UUID_DEFAULT)
    name: Mapped[str] = mapped_column(sa.String(64), unique=True)
    base_url: Mapped[str] = mapped_column(sa.String(255))
    enabled: Mapped[bool] = mapped_column(server_default=sa.true())
    # Non-secret settings only. Credentials live in environment variables.
    configuration: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class InstrumentProviderMapping(Base):
    """The symbol a provider uses for an instrument."""

    __tablename__ = "instrument_provider_mapping"
    __table_args__ = (sa.Index("ix_instrument_provider_mapping_provider_id", "provider_id"),)

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("instrument.id"), primary_key=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("provider.id"), primary_key=True)
    provider_symbol: Mapped[str] = mapped_column(sa.String(64))
    enabled: Mapped[bool] = mapped_column(server_default=sa.true())
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default=_EMPTY_JSON)
