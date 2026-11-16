import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Exchange, Instrument, InstrumentProviderMapping
from app.models.enums import AssetType


class SqlInstrumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_exchange(
        self, *, code: str, name: str, country: str | None, timezone: str
    ) -> uuid.UUID:
        values = {"name": name, "country": country, "timezone": timezone}
        statement = (
            insert(Exchange)
            .values(code=code, **values)
            .on_conflict_do_update(index_elements=[Exchange.code], set_=values)
            .returning(Exchange.id)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def get_exchange_by_code(self, code: str) -> Exchange | None:
        statement = sa.select(Exchange).where(Exchange.code == code)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def upsert(
        self,
        *,
        symbol: str,
        name: str,
        asset_type: AssetType,
        exchange_id: uuid.UUID | None,
        currency: str | None,
        enabled: bool,
        metadata: dict[str, Any],
    ) -> uuid.UUID:
        table = Instrument.__table__
        values = {"name": name, "currency": currency, "enabled": enabled, "metadata": metadata}
        statement = (
            insert(table)
            .values(symbol=symbol, asset_type=asset_type, exchange_id=exchange_id, **values)
            .on_conflict_do_update(constraint="uq_instrument_identity", set_=values)
            .returning(table.c.id)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def get(self, instrument_id: uuid.UUID) -> Instrument | None:
        return await self._session.get(Instrument, instrument_id)

    async def get_by_symbol(
        self, asset_type: AssetType, symbol: str, exchange_id: uuid.UUID | None = None
    ) -> Instrument | None:
        statement = sa.select(Instrument).where(
            Instrument.asset_type == asset_type,
            Instrument.symbol == symbol,
            Instrument.exchange_id.is_not_distinct_from(exchange_id),
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list(
        self,
        *,
        asset_type: AssetType | None = None,
        enabled: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Instrument]:
        statement = (
            sa.select(Instrument)
            .order_by(Instrument.asset_type, Instrument.symbol, Instrument.id)
            .limit(limit)
            .offset(offset)
        )
        if asset_type is not None:
            statement = statement.where(Instrument.asset_type == asset_type)
        if enabled is not None:
            statement = statement.where(Instrument.enabled == enabled)
        return (await self._session.execute(statement)).scalars().all()

    async def upsert_mapping(
        self,
        *,
        instrument_id: uuid.UUID,
        provider_id: uuid.UUID,
        provider_symbol: str,
        enabled: bool = True,
    ) -> None:
        values = {"provider_symbol": provider_symbol, "enabled": enabled}
        statement = (
            insert(InstrumentProviderMapping)
            .values(instrument_id=instrument_id, provider_id=provider_id, **values)
            .on_conflict_do_update(
                index_elements=[
                    InstrumentProviderMapping.instrument_id,
                    InstrumentProviderMapping.provider_id,
                ],
                set_=values,
            )
        )
        await self._session.execute(statement)

    async def list_mappings(
        self, provider_id: uuid.UUID, *, enabled_only: bool = True
    ) -> Sequence[InstrumentProviderMapping]:
        statement = (
            sa.select(InstrumentProviderMapping)
            .where(InstrumentProviderMapping.provider_id == provider_id)
            .order_by(InstrumentProviderMapping.provider_symbol)
        )
        if enabled_only:
            statement = statement.join(
                Instrument, Instrument.id == InstrumentProviderMapping.instrument_id
            ).where(InstrumentProviderMapping.enabled, Instrument.enabled)
        return (await self._session.execute(statement)).scalars().all()

    async def get_instruments_by_provider_symbol(
        self, provider_id: uuid.UUID
    ) -> dict[str, Instrument]:
        """Enabled instruments the provider is mapped to, keyed by the provider's symbol."""
        statement = (
            sa.select(InstrumentProviderMapping.provider_symbol, Instrument)
            .join(Instrument, Instrument.id == InstrumentProviderMapping.instrument_id)
            .where(
                InstrumentProviderMapping.provider_id == provider_id,
                InstrumentProviderMapping.enabled,
                Instrument.enabled,
            )
        )
        return {symbol: instrument for symbol, instrument in await self._session.execute(statement)}
