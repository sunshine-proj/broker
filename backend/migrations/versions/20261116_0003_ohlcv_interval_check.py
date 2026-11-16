"""Restrict ohlcv.interval to the normalized candle intervals.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INTERVALS = ("1min", "5min", "15min", "30min", "1h", "4h", "1day", "1week", "1month")


def upgrade() -> None:
    allowed = ", ".join(f"'{interval}'" for interval in INTERVALS)
    # op.f marks the name as final, so the naming convention does not prefix it again.
    op.create_check_constraint(op.f("ck_ohlcv_interval"), "ohlcv", f"interval IN ({allowed})")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_ohlcv_interval"), "ohlcv", type_="check")
