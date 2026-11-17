"""Watchlists, groups, yield spreads (seed file) and portfolios (portfolio file).

PRD FR-014 to FR-016. The calculations over them are database views and
functions (migration 0009).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InstrumentList(Base):
    """A watchlist (an ordered list to follow) or a group (instruments that belong together)."""

    __tablename__ = "instrument_list"
    __table_args__ = (sa.CheckConstraint("kind IN ('watchlist', 'group')", name="kind"),)

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(sa.String(32))
    name: Mapped[str] = mapped_column(sa.String(128), unique=True)
    title: Mapped[str] = mapped_column(sa.String(255))
    description: Mapped[str | None] = mapped_column(sa.Text)
    enabled: Mapped[bool] = mapped_column(server_default=sa.true())


class InstrumentListMember(Base):
    __tablename__ = "instrument_list_member"

    list_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("instrument_list.id", ondelete="CASCADE"), primary_key=True
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("instrument.id"), primary_key=True
    )
    position: Mapped[int]


class YieldSpread(Base):
    """The difference between two series, each from a named provider."""

    __tablename__ = "yield_spread"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(sa.String(128), unique=True)
    title: Mapped[str] = mapped_column(sa.String(255))
    long_instrument_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("instrument.id"))
    long_provider_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("provider.id"))
    short_instrument_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("instrument.id"))
    short_provider_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("provider.id"))
    enabled: Mapped[bool] = mapped_column(server_default=sa.true())


class Portfolio(Base):
    __tablename__ = "portfolio"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(sa.String(128), unique=True)
    base_currency: Mapped[str] = mapped_column(sa.String(10))
    loaded_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class PortfolioHolding(Base):
    """A position: a quantity of an instrument, valued with one provider's prices."""

    __tablename__ = "portfolio_holding"
    __table_args__ = (
        sa.CheckConstraint("quantity > 0", name="quantity"),
        sa.CheckConstraint("cost_basis >= 0", name="cost_basis"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("portfolio.id", ondelete="CASCADE")
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("instrument.id"))
    provider_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("provider.id"))
    quantity: Mapped[Decimal] = mapped_column(sa.Numeric)
    cost_basis: Mapped[Decimal] = mapped_column(sa.Numeric)
    cost_currency: Mapped[str] = mapped_column(sa.String(10))
    acquired_on: Mapped[date | None] = mapped_column(sa.Date)
    note: Mapped[str | None] = mapped_column(sa.Text)
