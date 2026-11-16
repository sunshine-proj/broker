"""Collection jobs, execution triggers and exchange session hours.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_KINDS = ("quotes", "candles", "history", "indicators")
TRIGGERS = ("scheduled", "manual", "retry")
COLLECTOR_STATUSES = ("running", "success", "failed")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.add_column("exchange", sa.Column("trading_open", sa.Time))
    op.add_column("exchange", sa.Column("trading_close", sa.Time))

    op.add_column(
        "collector_execution",
        sa.Column("trigger", sa.String(32), server_default="scheduled", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_collector_execution_trigger"), "collector_execution", _in("trigger", TRIGGERS)
    )

    op.create_table(
        "collection_job",
        sa.Column("id", sa.Uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("provider_id", sa.Uuid, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("interval_minutes", sa.Integer),
        sa.Column("daily_at", sa.Time),
        sa.Column("market_hours_only", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("retry_minutes", sa.Integer),
        sa.Column("settings", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_status", sa.String(32)),
        sa.Column("last_error", sa.Text),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_collection_job"),
        sa.UniqueConstraint("name", name="uq_collection_job_name"),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["provider.id"], name="fk_collection_job_provider_id_provider"
        ),
        sa.CheckConstraint(_in("kind", JOB_KINDS), name=op.f("ck_collection_job_kind")),
        sa.CheckConstraint(
            _in("last_status", COLLECTOR_STATUSES), name=op.f("ck_collection_job_last_status")
        ),
        sa.CheckConstraint(
            "(interval_minutes IS NULL) <> (daily_at IS NULL)",
            name=op.f("ck_collection_job_schedule"),
        ),
    )


def downgrade() -> None:
    op.drop_table("collection_job")
    op.drop_constraint(op.f("ck_collector_execution_trigger"), "collector_execution", type_="check")
    op.drop_column("collector_execution", "trigger")
    op.drop_column("exchange", "trading_close")
    op.drop_column("exchange", "trading_open")
