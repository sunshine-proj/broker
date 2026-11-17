"""Database backups: one row per backup run, written by the backup service.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "database_backup",
        sa.Column("id", sa.Uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("started_at", TIMESTAMPTZ, nullable=False),
        sa.Column("finished_at", TIMESTAMPTZ, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("file_name", sa.String(255)),
        sa.Column("size_bytes", sa.BigInteger),
        # How often the backup service runs, so a missed backup can be told apart.
        sa.Column("interval_seconds", sa.Integer, nullable=False),
        sa.Column("error", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_database_backup"),
        sa.CheckConstraint(
            "status IN ('success', 'failed')", name=op.f("ck_database_backup_status")
        ),
    )
    op.create_index("ix_database_backup_started_at", "database_backup", ["started_at"])


def downgrade() -> None:
    op.drop_table("database_backup")
