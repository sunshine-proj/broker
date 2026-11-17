"""Data-quality issues, and the queries the quality checks run (PRD FR-012)."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DataQualityIssue
from app.models.enums import (
    CandleInterval,
    IssueKind,
    IssueResolution,
    IssueStatus,
    ObservationType,
)

# Problems that come and go: closing one and detecting it again reopens it.
# Any other issue, once closed, stays closed.
_REOPENABLE = (IssueKind.STALE.value, IssueKind.DISCREPANCY.value)

# Observation types whose values can be compared over time and across providers.
PRICE_LIKE = (
    ObservationType.PRICE,
    ObservationType.NAV,
    ObservationType.RATE,
    ObservationType.YIELD,
    ObservationType.INDEX_LEVEL,
)


@dataclass(frozen=True)
class IssueIn:
    """A detected problem, to be recorded."""

    kind: IssueKind
    fingerprint: str
    message: str
    instrument_id: uuid.UUID | None = None
    provider_id: uuid.UUID | None = None
    other_provider_id: uuid.UUID | None = None
    observation_type: ObservationType | None = None
    interval: CandleInterval | None = None
    range_start: datetime | None = None
    range_end: datetime | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class Series:
    """A series a job collects: one instrument's values of one type (or candle interval)."""

    instrument_id: uuid.UUID
    symbol: str
    provider_symbol: str
    asset_type: str
    exchange_timezone: str | None
    trading_open: Any
    trading_close: Any
    # Rates recorded only on the dates they change are not checked for staleness or gaps.
    published_on_change: bool


@dataclass(frozen=True)
class Latest:
    instrument_id: uuid.UUID
    observation_type: ObservationType | None
    observed_at: datetime


@dataclass(frozen=True)
class Gap:
    instrument_id: uuid.UUID
    observation_type: ObservationType | None
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Spike:
    instrument_id: uuid.UUID
    provider_id: uuid.UUID
    observation_type: ObservationType
    observed_at: datetime
    value: Decimal
    unit: str
    previous_at: datetime
    previous_value: Decimal


@dataclass(frozen=True)
class Discrepancy:
    instrument_id: uuid.UUID
    symbol: str
    observation_type: ObservationType
    unit: str
    provider_id: uuid.UUID
    other_provider_id: uuid.UUID
    provider: str
    other_provider: str
    value: Decimal
    other_value: Decimal
    observed_at: datetime
    other_observed_at: datetime


def _json(details: dict[str, Any] | None) -> dict[str, Any]:
    """Details as JSON values: decimals and times become strings."""
    return {
        k: v if isinstance(v, (str, int, bool, type(None))) else str(v)
        for k, v in (details or {}).items()
    }


def _merged(details: dict[str, Any]) -> sa.ColumnElement[Any]:
    """The issue's details with these added (JSON object concatenation)."""
    return DataQualityIssue.details.op("||")(sa.literal(_json(details), type_=JSONB))


class SqlQualityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Issues -----------------------------------------------------------------------

    async def report(self, issue: IssueIn, at: datetime) -> bool:
        """Record an open problem; returns True if it is new or was reopened.

        Detecting a problem that is already open refreshes it. A closed stale
        or discrepancy issue is reopened; any other closed issue is left alone.
        """
        table = DataQualityIssue.__table__
        current = (
            await self._session.execute(
                sa.select(table.c.id, table.c.status)
                .where(table.c.fingerprint == issue.fingerprint)
                .with_for_update()
            )
        ).first()
        values = self._values(issue)
        refreshed = {
            "last_seen_at": at,
            "message": values["message"],
            "details": values["details"],
            "range_start": values["range_start"],
            "range_end": values["range_end"],
        }
        if current is None:
            await self._session.execute(
                insert(table).values(
                    **values,
                    status=IssueStatus.OPEN,
                    detected_at=at,
                    last_seen_at=at,
                )
            )
            return True
        if current.status == IssueStatus.OPEN.value:
            await self._session.execute(
                sa.update(table).where(table.c.id == current.id).values(**refreshed)
            )
            return False
        if issue.kind.value not in _REOPENABLE:
            return False
        await self._session.execute(
            sa.update(table)
            .where(table.c.id == current.id)
            .values(
                **refreshed,
                status=IssueStatus.OPEN,
                resolution=None,
                closed_at=None,
                detected_at=at,
            )
        )
        return True

    async def record_event(self, issue: IssueIn, at: datetime) -> None:
        """Record something that happened (a conflict, a spike), closed at once."""
        table = DataQualityIssue.__table__
        values = self._values(issue) | {
            "status": IssueStatus.CLOSED,
            "resolution": IssueResolution.RECORDED,
            "detected_at": at,
            "last_seen_at": at,
            "closed_at": at,
        }
        statement = insert(table).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.fingerprint],
            set_={
                "last_seen_at": statement.excluded.last_seen_at,
                "details": statement.excluded.details,
            },
        )
        await self._session.execute(statement)

    async def close_others(self, kind: IssueKind, keep: Collection[str], at: datetime) -> int:
        """Close the open issues of a kind that were not detected again; returns how many."""
        statement = (
            sa.update(DataQualityIssue)
            .where(DataQualityIssue.kind == kind, DataQualityIssue.status == IssueStatus.OPEN)
            .values(status=IssueStatus.CLOSED, resolution=IssueResolution.CLEARED, closed_at=at)
        )
        if keep:
            statement = statement.where(DataQualityIssue.fingerprint.not_in(list(keep)))
        return (await self._session.execute(statement)).rowcount

    async def close(
        self,
        issue_id: uuid.UUID,
        resolution: IssueResolution,
        at: datetime,
        details: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": IssueStatus.CLOSED,
            "resolution": resolution,
            "closed_at": at,
            "last_seen_at": at,
        }
        if details is not None:
            values["details"] = _merged(details)
        await self._session.execute(
            sa.update(DataQualityIssue).where(DataQualityIssue.id == issue_id).values(**values)
        )

    async def note(self, issue_id: uuid.UUID, at: datetime, details: dict[str, Any]) -> None:
        """Add details to an open issue."""
        await self._session.execute(
            sa.update(DataQualityIssue)
            .where(DataQualityIssue.id == issue_id)
            .values(
                last_seen_at=at,
                details=_merged(details),
            )
        )

    async def open_gaps(self, provider_id: uuid.UUID, limit: int) -> Sequence[DataQualityIssue]:
        """Open gaps of a provider, oldest detection first."""
        statement = (
            sa.select(DataQualityIssue)
            .where(
                DataQualityIssue.kind == IssueKind.GAP,
                DataQualityIssue.status == IssueStatus.OPEN,
                DataQualityIssue.provider_id == provider_id,
            )
            .order_by(DataQualityIssue.detected_at, DataQualityIssue.range_start)
            .limit(limit)
        )
        return (await self._session.execute(statement)).scalars().all()

    @staticmethod
    def _values(issue: IssueIn) -> dict[str, Any]:
        return {
            "kind": issue.kind,
            "fingerprint": issue.fingerprint,
            "message": issue.message,
            "instrument_id": issue.instrument_id,
            "provider_id": issue.provider_id,
            "other_provider_id": issue.other_provider_id,
            "observation_type": issue.observation_type,
            "interval": issue.interval,
            "range_start": issue.range_start,
            "range_end": issue.range_end,
            "details": _json(issue.details),
        }

    # --- What the checks read ---------------------------------------------------------

    async def series(
        self, provider_id: uuid.UUID, asset_types: Collection[str] | None
    ) -> Sequence[Series]:
        """The enabled instruments a provider collects, with their exchange sessions."""
        query = """
            SELECT i.id, i.symbol, m.provider_symbol, i.asset_type,
                   e.timezone, e.trading_open, e.trading_close,
                   coalesce((i.metadata->>'published_on_change')::boolean, false)
              FROM instrument_provider_mapping m
              JOIN instrument i ON i.id = m.instrument_id
              LEFT JOIN exchange e ON e.id = i.exchange_id
             WHERE m.provider_id = :provider AND m.enabled AND i.enabled
        """
        params: dict[str, Any] = {"provider": provider_id}
        if asset_types:
            query += " AND i.asset_type = ANY(:asset_types)"
            params["asset_types"] = list(asset_types)
        rows = await self._session.execute(sa.text(query + " ORDER BY i.symbol"), params)
        return [Series(*row) for row in rows]

    async def latest_observations(
        self,
        provider_id: uuid.UUID,
        instrument_ids: Sequence[uuid.UUID],
        observation_types: Collection[ObservationType] | None,
    ) -> Sequence[Latest]:
        """The newest value of each series, through each series' index."""
        if not instrument_ids:
            return []
        types = [t.value for t in (observation_types or ObservationType)]
        rows = await self._session.execute(
            sa.text(
                """
                SELECT ids.id, t.observation_type, o.observed_at
                  FROM unnest(CAST(:ids AS uuid[])) AS ids(id)
                 CROSS JOIN unnest(CAST(:types AS text[])) AS t(observation_type)
                 CROSS JOIN LATERAL (
                       SELECT observed_at FROM market_observation mo
                        WHERE mo.instrument_id = ids.id AND mo.provider_id = :provider
                          AND mo.observation_type = t.observation_type
                        ORDER BY mo.observed_at DESC LIMIT 1) o
                """
            ),
            {"ids": list(instrument_ids), "types": types, "provider": provider_id},
        )
        return [Latest(row[0], ObservationType(row[1]), row[2]) for row in rows]

    async def latest_candles(
        self, provider_id: uuid.UUID, instrument_ids: Sequence[uuid.UUID], interval: CandleInterval
    ) -> Sequence[Latest]:
        if not instrument_ids:
            return []
        rows = await self._session.execute(
            sa.text(
                """
                SELECT ids.id, c.timestamp
                  FROM unnest(CAST(:ids AS uuid[])) AS ids(id)
                 CROSS JOIN LATERAL (
                       SELECT timestamp FROM ohlcv
                        WHERE instrument_id = ids.id AND provider_id = :provider
                          AND interval = :interval
                        ORDER BY timestamp DESC LIMIT 1) c
                """
            ),
            {"ids": list(instrument_ids), "provider": provider_id, "interval": interval.value},
        )
        return [Latest(row[0], None, row[1]) for row in rows]

    async def observation_gaps(
        self,
        provider_id: uuid.UUID,
        instrument_ids: Sequence[uuid.UUID],
        observation_types: Collection[ObservationType] | None,
        since: datetime,
        longer_than_seconds: int,
    ) -> Sequence[Gap]:
        """Consecutive values since `since` further apart than the given wall-clock time."""
        if not instrument_ids:
            return []
        query = """
            SELECT instrument_id, observation_type, previous, observed_at FROM (
                SELECT instrument_id, observation_type, observed_at,
                       lag(observed_at) OVER (PARTITION BY instrument_id, observation_type
                                              ORDER BY observed_at) AS previous
                  FROM market_observation
                 WHERE provider_id = :provider AND instrument_id = ANY(:ids)
                   AND observed_at >= :since {types}
            ) s
             WHERE observed_at - previous
                   > make_interval(secs => CAST(:seconds AS double precision))
        """
        params: dict[str, Any] = {
            "provider": provider_id,
            "ids": list(instrument_ids),
            "since": since,
            "seconds": float(longer_than_seconds),
        }
        types = ""
        if observation_types:
            types = "AND observation_type = ANY(:types)"
            params["types"] = [t.value for t in observation_types]
        rows = await self._session.execute(sa.text(query.format(types=types)), params)
        return [Gap(row[0], ObservationType(row[1]), row[2], row[3]) for row in rows]

    async def candle_gaps(
        self,
        provider_id: uuid.UUID,
        instrument_ids: Sequence[uuid.UUID],
        interval: CandleInterval,
        since: datetime,
        longer_than_seconds: int,
    ) -> Sequence[Gap]:
        if not instrument_ids:
            return []
        rows = await self._session.execute(
            sa.text(
                """
                SELECT instrument_id, previous, timestamp FROM (
                    SELECT instrument_id, timestamp,
                           lag(timestamp) OVER (PARTITION BY instrument_id ORDER BY timestamp)
                               AS previous
                      FROM ohlcv
                     WHERE provider_id = :provider AND instrument_id = ANY(:ids)
                       AND interval = :interval AND timestamp >= :since
                ) s
                 WHERE timestamp - previous
                       > make_interval(secs => CAST(:seconds AS double precision))
                """
            ),
            {
                "provider": provider_id,
                "ids": list(instrument_ids),
                "interval": interval.value,
                "since": since,
                "seconds": float(longer_than_seconds),
            },
        )
        return [Gap(row[0], None, row[1], row[2]) for row in rows]

    async def count_in_range(
        self,
        provider_id: uuid.UUID,
        instrument_id: uuid.UUID,
        start: datetime,
        end: datetime,
        *,
        observation_type: ObservationType | None = None,
        interval: CandleInterval | None = None,
    ) -> int:
        """Values of one series strictly between start and end."""
        if interval is not None:
            query = """SELECT count(*) FROM ohlcv WHERE provider_id = :provider
                         AND instrument_id = :instrument AND interval = :kind
                         AND timestamp > :start AND timestamp < :end"""
            kind = interval.value
        else:
            assert observation_type is not None
            query = """SELECT count(*) FROM market_observation WHERE provider_id = :provider
                         AND instrument_id = :instrument AND observation_type = :kind
                         AND observed_at > :start AND observed_at < :end"""
            kind = observation_type.value
        count = await self._session.scalar(
            sa.text(query),
            {
                "provider": provider_id,
                "instrument": instrument_id,
                "kind": kind,
                "start": start,
                "end": end,
            },
        )
        return int(count or 0)

    async def spikes(self, since: datetime, percent: Decimal, points: Decimal) -> Sequence[Spike]:
        """Values stored since `since` that moved more than the thresholds from the one before.

        Values in percent (rates, yields) are compared in percentage points,
        everything else relative to the previous value.
        """
        rows = await self._session.execute(
            sa.text(
                """
                SELECT o.instrument_id, o.provider_id, o.observation_type, o.observed_at,
                       o.value, o.unit, p.observed_at, p.value
                  FROM market_observation o
                 CROSS JOIN LATERAL (
                       SELECT observed_at, value FROM market_observation x
                        WHERE x.instrument_id = o.instrument_id
                          AND x.provider_id = o.provider_id
                          AND x.observation_type = o.observation_type
                          AND x.observed_at < o.observed_at
                        ORDER BY x.observed_at DESC LIMIT 1) p
                 WHERE o.ingested_at >= :since
                   AND o.observed_at >= CAST(:since AS timestamptz) - INTERVAL '8 days'
                   AND o.observation_type = ANY(:types)
                   AND o.quality_status = 'ok'
                   AND CASE WHEN o.unit = 'percent'
                            THEN abs(o.value - p.value) > :points
                            ELSE p.value <> 0 AND abs(o.value / p.value - 1) * 100 > :percent
                       END
                """
            ),
            {
                "since": since,
                "types": [t.value for t in PRICE_LIKE],
                "points": points,
                "percent": percent,
            },
        )
        return [
            Spike(r[0], r[1], ObservationType(r[2]), r[3], r[4], r[5], r[6], r[7]) for r in rows
        ]

    async def discrepancies(self) -> Sequence[Discrepancy]:
        """The latest values of each instrument from every pair of its providers."""
        rows = await self._session.execute(
            sa.text(
                """
                SELECT d.instrument_id, d.symbol, d.observation_type, d.unit,
                       p.id, o.id, d.provider, d.other_provider, d.value, d.other_value,
                       d.observed_at, d.other_observed_at
                  FROM v_provider_discrepancy d
                  JOIN provider p ON p.name = d.provider
                  JOIN provider o ON o.name = d.other_provider
                """
            )
        )
        return [Discrepancy(r[0], r[1], ObservationType(r[2]), *r[3:]) for r in rows]
