import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Provider


class SqlProviderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self, *, name: str, base_url: str, enabled: bool, configuration: dict[str, Any]
    ) -> uuid.UUID:
        values = {"base_url": base_url, "enabled": enabled, "configuration": configuration}
        statement = (
            insert(Provider)
            .values(name=name, **values)
            .on_conflict_do_update(index_elements=[Provider.name], set_=values)
            .returning(Provider.id)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def get_by_name(self, name: str) -> Provider | None:
        statement = sa.select(Provider).where(Provider.name == name)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list(self, *, enabled: bool | None = None) -> Sequence[Provider]:
        statement = sa.select(Provider).order_by(Provider.name)
        if enabled is not None:
            statement = statement.where(Provider.enabled == enabled)
        return (await self._session.execute(statement)).scalars().all()
