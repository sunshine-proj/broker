"""Storage of market observations and candles.

Both are immutable (PRD section 7): a row whose key is already stored is never
overwritten. If the incoming values differ from the stored ones, the stored row
is kept, the difference is logged as a data-quality warning and returned to the
caller, which records it as a data-quality issue.
"""

import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import batched
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OHLCV, MarketObservation
from app.models.enums import CandleInterval, ObservationType, QualityStatus
from app.repositories.interfaces import Conflict, WriteResult
from app.schemas.market_data import MarketObservationIn, OHLCVIn

logger = logging.getLogger(__name__)

# Keeps each statement well below PostgreSQL's limit of 32,767 bind parameters.
_BATCH_SIZE = 1000

_OBSERVATION_KEY = ("instrument_id", "provider_id", "observation_type", "observed_at")
_OBSERVATION_VALUES = ("value",)
_CANDLE_KEY = ("instrument_id", "provider_id", "interval", "timestamp")
_CANDLE_VALUES = ("open", "high", "low", "close", "volume")

Row = Mapping[str, Any]


class SqlMarketDataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_observations(self, observations: Sequence[MarketObservationIn]) -> WriteResult:
        rows = [observation.model_dump() for observation in observations]
        return await self._insert_immutable(
            MarketObservation.__table__, rows, _OBSERVATION_KEY, _OBSERVATION_VALUES
        )

    async def add_candles(self, candles: Sequence[OHLCVIn]) -> WriteResult:
        rows = [candle.model_dump() for candle in candles]
        return await self._insert_immutable(OHLCV.__table__, rows, _CANDLE_KEY, _CANDLE_VALUES)

    async def mark_suspect(self, keys: Sequence[Mapping[str, Any]]) -> int:
        """Set quality_status = suspect on the observations with these keys.

        The only change ever made to a stored observation (PRD FR-012).
        """
        if not keys:
            return 0
        table = MarketObservation.__table__
        key = sa.tuple_(*(table.c[c] for c in _OBSERVATION_KEY))
        updated = 0
        for batch in batched(keys, _BATCH_SIZE):
            statement = (
                sa.update(table)
                .where(key.in_([tuple(k[c] for c in _OBSERVATION_KEY) for k in batch]))
                .where(table.c.quality_status != QualityStatus.SUSPECT)
                .values(quality_status=QualityStatus.SUSPECT)
            )
            updated += (await self._session.execute(statement)).rowcount
        return updated

    async def get_observations(
        self,
        instrument_id: uuid.UUID,
        observation_type: ObservationType,
        *,
        start: datetime,
        end: datetime,
        provider_id: uuid.UUID | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[MarketObservation]:
        """Observations with start <= observed_at < end, oldest first."""
        statement = (
            sa.select(MarketObservation)
            .where(
                MarketObservation.instrument_id == instrument_id,
                MarketObservation.observation_type == observation_type,
                MarketObservation.observed_at >= start,
                MarketObservation.observed_at < end,
            )
            .order_by(MarketObservation.observed_at, MarketObservation.provider_id)
            .limit(limit)
            .offset(offset)
        )
        if provider_id is not None:
            statement = statement.where(MarketObservation.provider_id == provider_id)
        return (await self._session.execute(statement)).scalars().all()

    async def get_latest_observations(
        self, instrument_ids: Sequence[uuid.UUID], observation_type: ObservationType
    ) -> Sequence[MarketObservation]:
        """The most recent observation per instrument and provider."""
        if not instrument_ids:
            return []
        statement = (
            sa.select(MarketObservation)
            .where(
                MarketObservation.instrument_id.in_(instrument_ids),
                MarketObservation.observation_type == observation_type,
            )
            .ext(distinct_on(MarketObservation.instrument_id, MarketObservation.provider_id))
            .order_by(
                MarketObservation.instrument_id,
                MarketObservation.provider_id,
                MarketObservation.observed_at.desc(),
            )
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_candles(
        self,
        instrument_id: uuid.UUID,
        interval: CandleInterval,
        *,
        start: datetime,
        end: datetime,
        provider_id: uuid.UUID | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[OHLCV]:
        """Candles with start <= timestamp < end, oldest first."""
        statement = (
            sa.select(OHLCV)
            .where(
                OHLCV.instrument_id == instrument_id,
                OHLCV.interval == interval,
                OHLCV.timestamp >= start,
                OHLCV.timestamp < end,
            )
            .order_by(OHLCV.timestamp, OHLCV.provider_id)
            .limit(limit)
            .offset(offset)
        )
        if provider_id is not None:
            statement = statement.where(OHLCV.provider_id == provider_id)
        return (await self._session.execute(statement)).scalars().all()

    async def _insert_immutable(
        self,
        table: sa.Table,
        rows: Sequence[Row],
        key_columns: tuple[str, ...],
        value_columns: tuple[str, ...],
    ) -> WriteResult:
        if not rows:
            return WriteResult()

        def key_of(row: Row) -> tuple[Any, ...]:
            return tuple(row[column] for column in key_columns)

        def values_of(row: Row) -> tuple[Any, ...]:
            return tuple(row[column] for column in value_columns)

        unchanged = 0
        conflicts: list[tuple[Row, tuple[Any, ...]]] = []

        # A key repeated within the batch: the first occurrence wins.
        unique: dict[tuple[Any, ...], Row] = {}
        for row in rows:
            first = unique.setdefault(key_of(row), row)
            if first is not row:
                if values_of(first) == values_of(row):
                    unchanged += 1
                else:
                    conflicts.append((row, values_of(first)))

        key = [table.c[column] for column in key_columns]
        inserted_keys: set[tuple[Any, ...]] = set()
        for batch in batched(unique.values(), _BATCH_SIZE):
            statement = insert(table).values(list(batch)).on_conflict_do_nothing().returning(*key)
            inserted_keys.update(tuple(r) for r in await self._session.execute(statement))

        # Rows that were already stored: compare them with what came in.
        existing_keys = [k for k in unique if k not in inserted_keys]
        for batch in batched(existing_keys, _BATCH_SIZE):
            statement = sa.select(*key, *(table.c[c] for c in value_columns)).where(
                sa.tuple_(*key).in_(batch)
            )
            for stored in await self._session.execute(statement):
                stored_key = tuple(stored[: len(key_columns)])
                stored_values = tuple(stored[len(key_columns) :])
                incoming = unique[stored_key]
                if values_of(incoming) == stored_values:
                    unchanged += 1
                else:
                    conflicts.append((incoming, stored_values))

        for incoming, stored_values in conflicts:
            logger.warning(
                "Conflicting value for an existing %s row; stored value kept",
                table.name,
                extra={
                    "table": table.name,
                    "key": {c: str(incoming[c]) for c in key_columns},
                    "stored": dict(zip(value_columns, map(str, stored_values), strict=True)),
                    "incoming": {c: str(incoming[c]) for c in value_columns},
                },
            )

        return WriteResult(
            inserted=len(inserted_keys),
            unchanged=unchanged,
            conflicting=len(conflicts),
            conflicts=tuple(
                Conflict(
                    key={c: incoming[c] for c in key_columns},
                    stored=dict(zip(value_columns, stored_values, strict=True)),
                    incoming={c: incoming[c] for c in value_columns},
                )
                for incoming, stored_values in conflicts
            ),
        )
