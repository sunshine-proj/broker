"""Compression settings for the time-series tables.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17

TimescaleDB stores compressed chunks in its columnstore. Rows are grouped by
series (`segmentby`) and ordered newest first within each group, which is how
history and latest-value queries read them. This migration only enables
compression; when chunks are compressed, and whether old data is dropped, are
policies applied at startup from the environment (`python -m app.cli
storage-policies`).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = {
    "market_observation": ("instrument_id, provider_id, observation_type", "observed_at DESC"),
    "ohlcv": ("instrument_id, provider_id, interval", "timestamp DESC"),
}


def upgrade() -> None:
    for table, (segment_by, order_by) in TABLES.items():
        op.execute(
            f"ALTER TABLE {table} SET (timescaledb.enable_columnstore = true, "
            f"timescaledb.segmentby = '{segment_by}', timescaledb.orderby = '{order_by}')"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"CALL remove_columnstore_policy('{table}', if_exists => true)")
        op.execute(
            f"SELECT decompress_chunk(c, if_compressed => true) FROM show_chunks('{table}') c"
        )
        op.execute(f"ALTER TABLE {table} SET (timescaledb.enable_columnstore = false)")
