"""Watchlists, groups and yield spreads, as the seed file defines them."""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InstrumentList, InstrumentListMember, YieldSpread


class SqlListRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_list(
        self,
        *,
        kind: str,
        name: str,
        title: str,
        description: str | None,
        enabled: bool,
        instrument_ids: Sequence[uuid.UUID],
    ) -> uuid.UUID:
        """Create or update a list and replace its members, in the given order."""
        table = InstrumentList.__table__
        values = {"kind": kind, "title": title, "description": description, "enabled": enabled}
        statement = (
            insert(table)
            .values(name=name, **values)
            .on_conflict_do_update(index_elements=[table.c.name], set_=values)
            .returning(table.c.id)
        )
        list_id = (await self._session.execute(statement)).scalar_one()
        await self._session.execute(
            sa.delete(InstrumentListMember).where(InstrumentListMember.list_id == list_id)
        )
        if instrument_ids:
            await self._session.execute(
                insert(InstrumentListMember.__table__).values(
                    [
                        {"list_id": list_id, "instrument_id": instrument_id, "position": position}
                        for position, instrument_id in enumerate(instrument_ids, start=1)
                    ]
                )
            )
        return list_id

    async def upsert_spread(
        self,
        *,
        name: str,
        title: str,
        long: tuple[uuid.UUID, uuid.UUID],
        short: tuple[uuid.UUID, uuid.UUID],
        enabled: bool,
    ) -> uuid.UUID:
        """Create or update a spread; legs are (instrument id, provider id)."""
        table = YieldSpread.__table__
        values = {
            "title": title,
            "long_instrument_id": long[0],
            "long_provider_id": long[1],
            "short_instrument_id": short[0],
            "short_provider_id": short[1],
            "enabled": enabled,
        }
        statement = (
            insert(table)
            .values(name=name, **values)
            .on_conflict_do_update(index_elements=[table.c.name], set_=values)
            .returning(table.c.id)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def disable_others(self, list_names: Sequence[str], spread_names: Sequence[str]) -> int:
        """Disable lists and spreads no longer in the seed file; returns how many."""
        disabled = 0
        for model, names in ((InstrumentList, list_names), (YieldSpread, spread_names)):
            statement = sa.update(model).where(model.enabled, model.name.not_in(list(names)))
            disabled += (await self._session.execute(statement.values(enabled=False))).rowcount
        return disabled
