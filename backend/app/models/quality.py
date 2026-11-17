"""Data-quality issues (PRD FR-012)."""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import (
    CandleInterval,
    IssueKind,
    IssueResolution,
    IssueStatus,
    ObservationType,
    enum_type,
)


class DataQualityIssue(Base):
    """One data-quality problem, found by a check or recorded during ingestion.

    `fingerprint` identifies the problem, so detecting it again updates the
    same row instead of adding another.
    """

    __tablename__ = "data_quality_issue"
    __table_args__ = (
        sa.Index("ix_data_quality_issue_status_kind", "status", "kind"),
        sa.Index("ix_data_quality_issue_detected_at", "detected_at"),
        # Open issues have no resolution; closed ones always do.
        sa.CheckConstraint("(status = 'open') = (resolution IS NULL)", name="closed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    kind: Mapped[IssueKind] = mapped_column(enum_type(IssueKind, "kind"))
    status: Mapped[IssueStatus] = mapped_column(enum_type(IssueStatus, "status"))
    resolution: Mapped[IssueResolution | None] = mapped_column(
        enum_type(IssueResolution, "resolution")
    )
    fingerprint: Mapped[str] = mapped_column(sa.String(400), unique=True)
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("instrument.id"))
    provider_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("provider.id"))
    # The second provider of a discrepancy.
    other_provider_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("provider.id"))
    observation_type: Mapped[ObservationType | None] = mapped_column(
        enum_type(ObservationType, "observation_type")
    )
    # Candle interval, for candle gaps and conflicts.
    interval: Mapped[CandleInterval | None] = mapped_column(
        enum_type(CandleInterval, "interval", length=8)
    )
    range_start: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    range_end: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    message: Mapped[str] = mapped_column(sa.Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"))
    detected_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
