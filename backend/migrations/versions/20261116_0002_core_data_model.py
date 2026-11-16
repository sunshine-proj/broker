"""Core data model: reference entities, time-series hypertables, operations.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Enumeration values are spelled out here on purpose: a migration is a
# snapshot and must not change when the application's enums do.
ASSET_TYPES = (
    "stock",
    "etf",
    "crypto",
    "forex",
    "government_bond_yield",
    "interest_rate",
    "commodity",
    "index",
)
OBSERVATION_TYPES = (
    "price",
    "nav",
    "rate",
    "yield",
    "index_level",
    "market_cap",
    "volume",
    "circulating_supply",
    "total_supply",
    "dominance",
)
FRESHNESS = ("real_time", "delayed", "end_of_day")
QUOTE_KINDS = ("traded", "indicative", "reference")
QUALITY_STATUSES = ("ok", "suspect")
FREQUENCIES = ("daily", "weekly", "monthly", "quarterly", "annual")
COLLECTOR_STATUSES = ("running", "success", "failed")
PROVIDER_HEALTH_STATUSES = ("healthy", "degraded", "down")

TIMESTAMPTZ = sa.DateTime(timezone=True)
UUID_DEFAULT = sa.text("gen_random_uuid()")
EMPTY_JSON = sa.text("'{}'::jsonb")
NOW = sa.func.now()


def _enum_check(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    # op.f marks the name as final, so the naming convention does not prefix it again.
    return sa.CheckConstraint(f"{column} IN ({allowed})", name=op.f(f"ck_{table}_{column}"))


def upgrade() -> None:
    op.create_table(
        "exchange",
        sa.Column("id", sa.Uuid, server_default=UUID_DEFAULT, nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("country", sa.String(2)),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_exchange"),
        sa.UniqueConstraint("code", name="uq_exchange_code"),
    )

    op.create_table(
        "provider",
        sa.Column("id", sa.Uuid, server_default=UUID_DEFAULT, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("base_url", sa.String(255), nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("configuration", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_provider"),
        sa.UniqueConstraint("name", name="uq_provider_name"),
    )

    op.create_table(
        "instrument",
        sa.Column("id", sa.Uuid, server_default=UUID_DEFAULT, nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("asset_type", sa.String(32), nullable=False),
        sa.Column("exchange_id", sa.Uuid),
        sa.Column("currency", sa.String(10)),
        sa.Column("metadata", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_instrument"),
        sa.ForeignKeyConstraint(
            ["exchange_id"], ["exchange.id"], name="fk_instrument_exchange_id_exchange"
        ),
        sa.UniqueConstraint(
            "asset_type",
            "symbol",
            "exchange_id",
            name="uq_instrument_identity",
            postgresql_nulls_not_distinct=True,
        ),
        _enum_check("instrument", "asset_type", ASSET_TYPES),
    )

    op.create_table(
        "instrument_provider_mapping",
        sa.Column("instrument_id", sa.Uuid, nullable=False),
        sa.Column("provider_id", sa.Uuid, nullable=False),
        sa.Column("provider_symbol", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("metadata", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.PrimaryKeyConstraint(
            "instrument_id", "provider_id", name="pk_instrument_provider_mapping"
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name="fk_instrument_provider_mapping_instrument_id_instrument",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["provider.id"],
            name="fk_instrument_provider_mapping_provider_id_provider",
        ),
    )
    op.create_index(
        "ix_instrument_provider_mapping_provider_id",
        "instrument_provider_mapping",
        ["provider_id"],
    )

    op.create_table(
        "market_observation",
        sa.Column("instrument_id", sa.Uuid, nullable=False),
        sa.Column("provider_id", sa.Uuid, nullable=False),
        sa.Column("observation_type", sa.String(32), nullable=False),
        sa.Column("observed_at", TIMESTAMPTZ, nullable=False),
        sa.Column("ingested_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.Column("value", sa.Numeric, nullable=False),
        sa.Column("currency", sa.String(10)),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("freshness", sa.String(32), nullable=False),
        sa.Column("quote_kind", sa.String(32), nullable=False),
        sa.Column("quality_status", sa.String(32), server_default="ok", nullable=False),
        sa.Column("metadata", JSONB),
        sa.PrimaryKeyConstraint(
            "instrument_id",
            "provider_id",
            "observation_type",
            "observed_at",
            name="pk_market_observation",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name="fk_market_observation_instrument_id_instrument",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["provider.id"], name="fk_market_observation_provider_id_provider"
        ),
        _enum_check("market_observation", "observation_type", OBSERVATION_TYPES),
        _enum_check("market_observation", "freshness", FRESHNESS),
        _enum_check("market_observation", "quote_kind", QUOTE_KINDS),
        _enum_check("market_observation", "quality_status", QUALITY_STATUSES),
    )
    op.execute(
        "SELECT create_hypertable('market_observation', "
        "by_range('observed_at', INTERVAL '7 days'), create_default_indexes => false)"
    )
    op.create_index(
        "ix_market_observation_instrument_type_time",
        "market_observation",
        ["instrument_id", "observation_type", "observed_at"],
    )

    op.create_table(
        "ohlcv",
        sa.Column("instrument_id", sa.Uuid, nullable=False),
        sa.Column("provider_id", sa.Uuid, nullable=False),
        sa.Column("interval", sa.String(8), nullable=False),
        sa.Column("timestamp", TIMESTAMPTZ, nullable=False),
        sa.Column("open", sa.Numeric, nullable=False),
        sa.Column("high", sa.Numeric, nullable=False),
        sa.Column("low", sa.Numeric, nullable=False),
        sa.Column("close", sa.Numeric, nullable=False),
        sa.Column("volume", sa.Numeric),
        sa.Column("currency", sa.String(10)),
        sa.Column("ingested_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint(
            "instrument_id", "provider_id", "interval", "timestamp", name="pk_ohlcv"
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"], ["instrument.id"], name="fk_ohlcv_instrument_id_instrument"
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["provider.id"], name="fk_ohlcv_provider_id_provider"
        ),
    )
    op.execute(
        "SELECT create_hypertable('ohlcv', "
        "by_range('timestamp', INTERVAL '30 days'), create_default_indexes => false)"
    )
    op.create_index(
        "ix_ohlcv_instrument_interval_time", "ohlcv", ["instrument_id", "interval", "timestamp"]
    )

    op.create_table(
        "economic_indicator",
        sa.Column("id", sa.Uuid, server_default=UUID_DEFAULT, nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("country", sa.String(2)),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("frequency", sa.String(32), nullable=False),
        sa.Column("provider_id", sa.Uuid, nullable=False),
        sa.Column("provider_series_code", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("metadata", JSONB, server_default=EMPTY_JSON, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_economic_indicator"),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["provider.id"], name="fk_economic_indicator_provider_id_provider"
        ),
        sa.UniqueConstraint("code", name="uq_economic_indicator_code"),
        sa.UniqueConstraint(
            "provider_id", "provider_series_code", name="uq_economic_indicator_provider_series"
        ),
        _enum_check("economic_indicator", "frequency", FREQUENCIES),
    )

    op.create_table(
        "economic_observation",
        sa.Column("indicator_id", sa.Uuid, nullable=False),
        sa.Column("observed_at", TIMESTAMPTZ, nullable=False),
        sa.Column("ingested_at", TIMESTAMPTZ, nullable=False),
        sa.Column("value", sa.Numeric, nullable=False),
        sa.Column("metadata", JSONB),
        sa.PrimaryKeyConstraint(
            "indicator_id", "observed_at", "ingested_at", name="pk_economic_observation"
        ),
        sa.ForeignKeyConstraint(
            ["indicator_id"],
            ["economic_indicator.id"],
            name="fk_economic_observation_indicator_id_economic_indicator",
        ),
    )

    op.create_table(
        "collector_execution",
        sa.Column("id", sa.Uuid, server_default=UUID_DEFAULT, nullable=False),
        sa.Column("job_name", sa.String(128), nullable=False),
        sa.Column("provider_id", sa.Uuid),
        sa.Column("started_at", TIMESTAMPTZ, nullable=False),
        sa.Column("finished_at", TIMESTAMPTZ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("records_written", sa.Integer, server_default="0", nullable=False),
        sa.Column("error", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_collector_execution"),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["provider.id"], name="fk_collector_execution_provider_id_provider"
        ),
        _enum_check("collector_execution", "status", COLLECTOR_STATUSES),
    )
    op.create_index(
        "ix_collector_execution_job_started", "collector_execution", ["job_name", "started_at"]
    )

    op.create_table(
        "provider_health",
        sa.Column("provider_id", sa.Uuid, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_success_at", TIMESTAMPTZ),
        sa.Column("last_error_at", TIMESTAMPTZ),
        sa.Column("last_error", sa.Text),
        sa.Column("consecutive_failures", sa.Integer, server_default="0", nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("provider_id", name="pk_provider_health"),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["provider.id"], name="fk_provider_health_provider_id_provider"
        ),
        _enum_check("provider_health", "status", PROVIDER_HEALTH_STATUSES),
    )


def downgrade() -> None:
    for table in (
        "provider_health",
        "collector_execution",
        "economic_observation",
        "economic_indicator",
        "ohlcv",
        "market_observation",
        "instrument_provider_mapping",
        "instrument",
        "provider",
        "exchange",
    ):
        op.drop_table(table)
