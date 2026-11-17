"""User accounts, sessions and API tokens (PRD FR-017).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)
# Spelled out here, not imported from the application.
ROLES = ("viewer", "admin")


def _owned(table: str) -> list[sa.Column | sa.Constraint]:
    """Columns and constraints shared by sessions and tokens."""
    return [
        sa.Column("id", sa.Uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", TIMESTAMPTZ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("token_hash", name=f"uq_{table}_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=f"fk_{table}_user_id_app_user", ondelete="CASCADE"
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.Uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(32), server_default="viewer", nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.Column("failed_logins", sa.Integer, server_default="0", nullable=False),
        sa.Column("locked_until", TIMESTAMPTZ),
        sa.Column("last_login_at", TIMESTAMPTZ),
        sa.Column("created_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_app_user"),
        sa.UniqueConstraint("username", name="uq_app_user_username"),
        sa.CheckConstraint(
            "role IN (" + ", ".join(f"'{r}'" for r in ROLES) + ")", name=op.f("ck_app_user_role")
        ),
        sa.CheckConstraint("username = lower(username)", name=op.f("ck_app_user_username_lower")),
    )
    op.create_table(
        "user_session",
        *_owned("user_session"),
        sa.Column("expires_at", TIMESTAMPTZ, nullable=False),
    )
    op.create_index("ix_user_session_expires_at", "user_session", ["expires_at"])
    op.create_table(
        "api_token",
        *_owned("api_token"),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("expires_at", TIMESTAMPTZ),
        sa.UniqueConstraint("user_id", "name", name="uq_api_token_user_name"),
    )


def downgrade() -> None:
    op.drop_table("api_token")
    op.drop_table("user_session")
    op.drop_table("app_user")
