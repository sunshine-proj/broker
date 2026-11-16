"""Storage of economic indicators and their observations.

Economic observations are revisable (PRD section 7): a different value for a
period that already has one is stored as a further row with a later
`ingested_at`. Earlier values are kept.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import batched
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EconomicIndicator, EconomicObservation
from app.models.enums import IndicatorFrequency
from app.repositories.interfaces import RevisionWriteResult
from app.schemas.market_data import EconomicObservationIn

_BATCH_SIZE = 1000


class SqlEconomicDataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_indicator(
        self,
        *,
        code: str,
        name: str,
        country: str | None,
        unit: str,
        frequency: IndicatorFrequency,
        provider_id: uuid.UUID,
        provider_series_code: str,
        enabled: bool,
        metadata: dict[str, Any],
    ) -> uuid.UUID:
        table = EconomicIndicator.__table__
        values = {
            "name": name,
            "country": country,
            "unit": unit,
            "frequency": frequency,
            "provider_id": provider_id,
            "provider_series_code": provider_series_code,
            "enabled": enabled,
            "metadata": metadata,
        }
        statement = (
            insert(table)
            .values(code=code, **values)
            .on_conflict_do_update(index_elements=[table.c.code], set_=values)
            .returning(table.c.id)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def get_indicator_by_code(self, code: str) -> EconomicIndicator | None:
        statement = sa.select(EconomicIndicator).where(EconomicIndicator.code == code)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_indicators(
        self,
        *,
        provider_id: uuid.UUID | None = None,
        enabled: bool | None = None,
        limit: int | None = 100,
        offset: int = 0,
    ) -> Sequence[EconomicIndicator]:
        statement = sa.select(EconomicIndicator).order_by(EconomicIndicator.code).offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        if provider_id is not None:
            statement = statement.where(EconomicIndicator.provider_id == provider_id)
        if enabled is not None:
            statement = statement.where(EconomicIndicator.enabled == enabled)
        return (await self._session.execute(statement)).scalars().all()

    async def add_observations(
        self,
        observations: Sequence[EconomicObservationIn],
        *,
        ingested_at: datetime | None = None,
    ) -> RevisionWriteResult:
        if not observations:
            return RevisionWriteResult()
        ingested_at = ingested_at or datetime.now(UTC)

        # A period repeated within the batch: the first occurrence wins.
        incoming: dict[tuple[uuid.UUID, datetime], EconomicObservationIn] = {}
        for observation in observations:
            incoming.setdefault((observation.indicator_id, observation.observed_at), observation)

        current = await self._current_values(list(incoming))

        new_rows = []
        inserted = revised = 0
        for period, observation in incoming.items():
            if period not in current:
                inserted += 1
            elif current[period] != observation.value:
                revised += 1
            else:
                continue
            new_rows.append({**observation.model_dump(), "ingested_at": ingested_at})

        table = EconomicObservation.__table__
        for batch in batched(new_rows, _BATCH_SIZE):
            await self._session.execute(insert(table).values(list(batch)).on_conflict_do_nothing())

        return RevisionWriteResult(
            inserted=inserted, revised=revised, unchanged=len(incoming) - inserted - revised
        )

    async def get_observations(
        self,
        indicator_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        include_revisions: bool = False,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[EconomicObservation]:
        """Observations with start <= observed_at < end, oldest period first.

        By default only the current value of each period is returned. With
        `include_revisions`, every stored value is, oldest revision first.
        """
        statement = sa.select(EconomicObservation).where(
            EconomicObservation.indicator_id == indicator_id,
            EconomicObservation.observed_at >= start,
            EconomicObservation.observed_at < end,
        )
        if include_revisions:
            statement = statement.order_by(
                EconomicObservation.observed_at, EconomicObservation.ingested_at
            )
        else:
            statement = statement.ext(distinct_on(EconomicObservation.observed_at)).order_by(
                EconomicObservation.observed_at, EconomicObservation.ingested_at.desc()
            )
        statement = statement.limit(limit).offset(offset)
        return (await self._session.execute(statement)).scalars().all()

    async def _current_values(
        self, periods: Sequence[tuple[uuid.UUID, datetime]]
    ) -> dict[tuple[uuid.UUID, datetime], Any]:
        """The latest stored value for each of the given (indicator, period) pairs."""
        table = EconomicObservation.__table__
        current: dict[tuple[uuid.UUID, datetime], Any] = {}
        for batch in batched(periods, _BATCH_SIZE):
            statement = (
                sa.select(table.c.indicator_id, table.c.observed_at, table.c.value)
                .where(sa.tuple_(table.c.indicator_id, table.c.observed_at).in_(batch))
                .ext(distinct_on(table.c.indicator_id, table.c.observed_at))
                .order_by(table.c.indicator_id, table.c.observed_at, table.c.ingested_at.desc())
            )
            for indicator_id, observed_at, value in await self._session.execute(statement):
                current[(indicator_id, observed_at)] = value
        return current
