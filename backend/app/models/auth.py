"""User accounts, their sessions and API tokens (PRD FR-017).

Tokens are stored only as SHA-256 hashes, passwords only as scrypt hashes.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.enums import UserRole, enum_type


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (sa.CheckConstraint("username = lower(username)", name="username_lower"),)

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    username: Mapped[str] = mapped_column(sa.String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(sa.String(255))
    role: Mapped[UserRole] = mapped_column(
        enum_type(UserRole, "role"), server_default=UserRole.VIEWER.value
    )
    enabled: Mapped[bool] = mapped_column(server_default=sa.true())
    failed_logins: Mapped[int] = mapped_column(server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class UserSession(Base):
    """A signed-in browser: its cookie holds the token whose hash is stored here."""

    __tablename__ = "user_session"
    __table_args__ = (sa.Index("ix_user_session_expires_at", "expires_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("app_user.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ApiToken(Base):
    """A personal token for scripts, sent as `Authorization: Bearer <token>`."""

    __tablename__ = "api_token"
    __table_args__ = (sa.UniqueConstraint("user_id", "name", name="uq_api_token_user_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=sa.text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("app_user.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(sa.String(128))
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
