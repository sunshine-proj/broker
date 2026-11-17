"""TimescaleDB compression and retention policies (PRD section 6, Retention).

Applied on every startup, after migrations, from the environment. Each policy
is replaced, so changing a setting and restarting is enough. Migration 0007
enables compression; this decides when it happens and whether old data is
dropped. Economic observations are a plain table and are never dropped.
"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

TABLES = ("market_observation", "ohlcv")


@dataclass(frozen=True)
class StoragePolicies:
    # Compress chunks whose data is older than this many days; None: never.
    compress_after_days: int | None = 30
    # Drop market observations or candles older than this many days; None: keep forever.
    observation_retention_days: int | None = None
    candle_retention_days: int | None = None

    def retention(self, table: str) -> int | None:
        return (
            self.observation_retention_days
            if table == "market_observation"
            else self.candle_retention_days
        )


async def apply_storage_policies(connection: AsyncConnection, policies: StoragePolicies) -> None:
    """Replace the compression and retention policies of both time-series tables.

    The caller commits. Every number is a validated positive integer, so the
    statements are built with it directly.
    """
    for table in TABLES:
        await connection.exec_driver_sql(
            f"CALL remove_columnstore_policy('{table}', if_exists => true)"
        )
        if policies.compress_after_days is not None:
            days = int(policies.compress_after_days)
            await connection.exec_driver_sql(
                f"CALL add_columnstore_policy('{table}', after => INTERVAL '{days} days')"
            )
        await connection.exec_driver_sql(
            f"SELECT remove_retention_policy('{table}', if_exists => true)"
        )
        retention = policies.retention(table)
        if retention is not None:
            days = int(retention)
            await connection.exec_driver_sql(
                f"SELECT add_retention_policy('{table}', drop_after => INTERVAL '{days} days')"
            )
        logger.info(
            "Storage policies applied",
            extra={
                "table": table,
                "compress_after_days": policies.compress_after_days,
                "retention_days": retention,
            },
        )
