"""Operational records: collector runs and provider health."""

import uuid
from datetime import datetime, time
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import (
    CollectorStatus,
    ExecutionTrigger,
    JobKind,
    ProviderHealthStatus,
    enum_type,
)


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
    trigger: Mapped[ExecutionTrigger] = mapped_column(
        enum_type(ExecutionTrigger, "trigger"), server_default=ExecutionTrigger.SCHEDULED.value
    )
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


class CollectionJob(Base):
    """A scheduled collection, configured in the seed file.

    The schedule is either every `interval_minutes`, aligned to the clock, or
    once a day at `daily_at` (UTC). The status columns are kept up to date by
    the scheduler.
    """

    __tablename__ = "collection_job"
    __table_args__ = (
        sa.CheckConstraint("(interval_minutes IS NULL) <> (daily_at IS NULL)", name="schedule"),
        # Quality jobs check every provider's data; every other job has one provider.
        sa.CheckConstraint("(kind = 'quality') = (provider_id IS NULL)", name="provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(sa.String(128), unique=True)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("provider.id"))
    kind: Mapped[JobKind] = mapped_column(enum_type(JobKind, "kind"))
    interval_minutes: Mapped[int | None]
    daily_at: Mapped[time | None] = mapped_column(sa.Time)
    # Poll only instruments whose exchange is in its regular session.
    market_hours_only: Mapped[bool] = mapped_column(server_default=sa.false())
    # Minutes after a failed run to try once more; null for no retry.
    retry_minutes: Mapped[int | None]
    # Filters, windows and data-quality settings; see SeedJob.settings().
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"))
    enabled: Mapped[bool] = mapped_column(server_default=sa.true())

    last_started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_status: Mapped[CollectorStatus | None] = mapped_column(
        enum_type(CollectorStatus, "last_status")
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    next_run_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
