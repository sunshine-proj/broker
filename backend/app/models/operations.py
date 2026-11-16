"""Operational records: collector runs and provider health."""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import CollectorStatus, ProviderHealthStatus, enum_type


class CollectorExecution(Base):
    """One run of a collection job."""

    __tablename__ = "collector_execution"
    __table_args__ = (sa.Index("ix_collector_execution_job_started", "job_name", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    job_name: Mapped[str] = mapped_column(sa.String(128))
    provider_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("provider.id"))
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    status: Mapped[CollectorStatus] = mapped_column(enum_type(CollectorStatus, "status"))
    records_written: Mapped[int] = mapped_column(server_default="0")
    error: Mapped[str | None] = mapped_column(sa.Text)


class ProviderHealth(Base):
    """Current health of a provider; one row per provider."""

    __tablename__ = "provider_health"

    provider_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("provider.id"), primary_key=True)
    status: Mapped[ProviderHealthStatus] = mapped_column(enum_type(ProviderHealthStatus, "status"))
    last_success_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    consecutive_failures: Mapped[int] = mapped_column(server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
