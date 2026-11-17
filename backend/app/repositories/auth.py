"""User accounts, sessions and API tokens (PRD FR-017).

The running backend reads, inserts and updates; deleting old sessions is the
owner's (`python -m app.cli prune-sessions`, run at every start).
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiToken, AppUser, UserSession
from app.models.enums import UserRole


class SqlAuthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_user(self, username: str) -> AppUser | None:
        return await self._session.scalar(sa.select(AppUser).where(AppUser.username == username))

    async def list_users(self) -> Sequence[AppUser]:
        return list(await self._session.scalars(sa.select(AppUser).order_by(AppUser.username)))

    async def create_user(self, *, username: str, password_hash: str, role: UserRole) -> uuid.UUID:
        user = AppUser(username=username, password_hash=password_hash, role=role)
        self._session.add(user)
        await self._session.flush()
        return user.id

    async def update_user(self, user_id: uuid.UUID, **values: Any) -> None:
        await self._session.execute(
            sa.update(AppUser).where(AppUser.id == user_id).values(**values)
        )

    async def add_session(
        self, *, user_id: uuid.UUID, token_hash: str, expires_at: datetime
    ) -> None:
        self._session.add(
            UserSession(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        )
        await self._session.flush()

    async def session_user(self, token_hash: str, now: datetime) -> AppUser | None:
        return await self._session.scalar(
            sa.select(AppUser)
            .join(UserSession, UserSession.user_id == AppUser.id)
            .where(
                UserSession.token_hash == token_hash,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > now,
                AppUser.enabled,
            )
        )

    async def revoke_session(self, token_hash: str, now: datetime) -> None:
        await self._session.execute(
            sa.update(UserSession)
            .where(UserSession.token_hash == token_hash, UserSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def add_token(
        self, *, user_id: uuid.UUID, name: str, token_hash: str, expires_at: datetime | None
    ) -> uuid.UUID:
        token = ApiToken(user_id=user_id, name=name, token_hash=token_hash, expires_at=expires_at)
        self._session.add(token)
        await self._session.flush()
        return token.id

    async def token_user(self, token_hash: str, now: datetime) -> AppUser | None:
        return await self._session.scalar(
            sa.select(AppUser)
            .join(ApiToken, ApiToken.user_id == AppUser.id)
            .where(
                ApiToken.token_hash == token_hash,
                ApiToken.revoked_at.is_(None),
                sa.or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > now),
                AppUser.enabled,
            )
        )

    async def list_tokens(self) -> Sequence[Mapping[str, Any]]:
        statement = (
            sa.select(
                AppUser.username,
                ApiToken.name,
                ApiToken.created_at,
                ApiToken.expires_at,
                ApiToken.revoked_at,
            )
            .join(AppUser, AppUser.id == ApiToken.user_id)
            .order_by(AppUser.username, ApiToken.name)
        )
        return list((await self._session.execute(statement)).mappings())

    async def revoke_token(self, user_id: uuid.UUID, name: str, now: datetime) -> bool:
        result = await self._session.execute(
            sa.update(ApiToken)
            .where(
                ApiToken.user_id == user_id, ApiToken.name == name, ApiToken.revoked_at.is_(None)
            )
            .values(revoked_at=now)
        )
        return bool(result.rowcount)

    async def revoke_credentials(
        self, user_id: uuid.UUID, now: datetime, *, tokens: bool = True
    ) -> None:
        """Revoke the user's sessions and, unless `tokens` is false, API tokens."""
        for model in (UserSession, ApiToken) if tokens else (UserSession,):
            await self._session.execute(
                sa.update(model)
                .where(model.user_id == user_id, model.revoked_at.is_(None))
                .values(revoked_at=now)
            )

    async def prune_sessions(self, now: datetime) -> int:
        """Delete sessions that have expired or were revoked. Needs the owner's rights."""
        result = await self._session.execute(
            sa.delete(UserSession).where(
                sa.or_(UserSession.expires_at <= now, UserSession.revoked_at.is_not(None))
            )
        )
        return result.rowcount
