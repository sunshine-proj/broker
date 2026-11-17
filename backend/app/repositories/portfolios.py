"""Portfolios and their holdings, as the private portfolio file defines them."""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Instrument, InstrumentProviderMapping, Portfolio, PortfolioHolding, Provider
from app.repositories.interfaces import HoldingRecord, HoldingTarget, PortfolioRecord


class SqlPortfolioRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def holding_targets(self) -> Sequence[HoldingTarget]:
        statement = (
            sa.select(
                Instrument.symbol,
                Provider.name.label("provider"),
                Instrument.id.label("instrument_id"),
                Provider.id.label("provider_id"),
                Instrument.asset_type,
            )
            .join(
                InstrumentProviderMapping, InstrumentProviderMapping.instrument_id == Instrument.id
            )
            .join(Provider, Provider.id == InstrumentProviderMapping.provider_id)
        )
        return [HoldingTarget(**row) for row in (await self._session.execute(statement)).mappings()]

    async def replace_all(self, portfolios: Sequence[PortfolioRecord]) -> None:
        """Make the stored portfolios exactly `portfolios`, holdings included."""
        await self._session.execute(
            sa.delete(Portfolio).where(Portfolio.name.not_in([p.name for p in portfolios]))
        )
        table = Portfolio.__table__
        for portfolio in portfolios:
            statement = (
                insert(table)
                .values(name=portfolio.name, base_currency=portfolio.base_currency)
                .on_conflict_do_update(
                    index_elements=[table.c.name],
                    set_={"base_currency": portfolio.base_currency, "loaded_at": sa.func.now()},
                )
                .returning(table.c.id)
            )
            portfolio_id: uuid.UUID = (await self._session.execute(statement)).scalar_one()
            await self._session.execute(
                sa.delete(PortfolioHolding).where(PortfolioHolding.portfolio_id == portfolio_id)
            )
            if portfolio.holdings:
                await self._session.execute(
                    insert(PortfolioHolding.__table__).values(
                        [_holding_row(portfolio_id, h) for h in portfolio.holdings]
                    )
                )


def _holding_row(portfolio_id: uuid.UUID, holding: HoldingRecord) -> dict[str, object]:
    return {
        "portfolio_id": portfolio_id,
        "instrument_id": holding.instrument_id,
        "provider_id": holding.provider_id,
        "quantity": holding.quantity,
        "cost_basis": holding.cost_basis,
        "cost_currency": holding.cost_currency,
        "acquired_on": holding.acquired_on,
        "note": holding.note,
    }
