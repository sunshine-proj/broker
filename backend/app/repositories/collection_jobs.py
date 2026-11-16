"""Collection jobs and their execution history."""

import uuid
from collections.abc import Sequence
from datetime import datetime, time
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CollectionJob, CollectorExecution
from app.models.enums import CollectorStatus, ExecutionTrigger, JobKind

# Stored error text is capped; provider messages can be arbitrarily long.
_MAX_ERROR_LENGTH = 2000


class SqlCollectionJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        name: str,
        provider_id: uuid.UUID,
        kind: JobKind,
        interval_minutes: int | None,
        daily_at: time | None,
        market_hours_only: bool,
        retry_minutes: int | None,
        settings: dict[str, Any],
        enabled: bool,
    ) -> uuid.UUID:
        values = {
            "provider_id": provider_id,
            "kind": kind,
            "interval_minutes": interval_minutes,
            "daily_at": daily_at,
            "market_hours_only": market_hours_only,
            "retry_minutes": retry_minutes,
            "settings": settings,
            "enabled": enabled,
        }
        table = CollectionJob.__table__
        statement = (
            insert(table)
            .values(name=name, **values)
            .on_conflict_do_update(index_elements=[table.c.name], set_=values)
            .returning(table.c.id)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def disable_others(self, names: Sequence[str]) -> int:
        """Disable every job not named; returns how many were disabled."""
        statement = (
            sa.update(CollectionJob)
            .where(CollectionJob.name.not_in(names), CollectionJob.enabled)
            .values(enabled=False, next_run_at=None)
        )
        return (await self._session.execute(statement)).rowcount

    async def get(self, name: str) -> CollectionJob | None:
        statement = (
            sa.select(CollectionJob)
            .where(CollectionJob.name == name)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list(self, *, enabled: bool | None = None) -> Sequence[CollectionJob]:
        statement = (
            sa.select(CollectionJob)
            .order_by(CollectionJob.name)
            .execution_options(populate_existing=True)
        )
        if enabled is not None:
            statement = statement.where(CollectionJob.enabled == enabled)
        return (await self._session.execute(statement)).scalars().all()

    async def record_start(self, name: str, at: datetime) -> None:
        await self._session.execute(
            sa.update(CollectionJob)
            .where(CollectionJob.name == name)
            .values(last_started_at=at, last_status=CollectorStatus.RUNNING)
        )

    async def record_finish(
        self, name: str, at: datetime, status: CollectorStatus, error: str | None
    ) -> None:
        await self._session.execute(
            sa.update(CollectionJob)
            .where(CollectionJob.name == name)
            .values(
                last_finished_at=at,
                last_status=status,
                last_error=error[:_MAX_ERROR_LENGTH] if error else None,
            )
        )

    async def set_next_run(self, name: str, at: datetime | None) -> None:
        await self._session.execute(
            sa.update(CollectionJob).where(CollectionJob.name == name).values(next_run_at=at)
        )


class SqlCollectorExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(
        self,
        *,
        job_name: str,
        provider_id: uuid.UUID,
        trigger: ExecutionTrigger,
        at: datetime,
    ) -> uuid.UUID:
        table = CollectorExecution.__table__
        statement = (
            insert(table)
            .values(
                job_name=job_name,
                provider_id=provider_id,
                trigger=trigger,
                started_at=at,
                status=CollectorStatus.RUNNING,
            )
            .returning(table.c.id)
        )
        return (await self._session.execute(statement)).scalar_one()

    async def finish(
        self,
        execution_id: uuid.UUID,
        *,
        at: datetime,
        status: CollectorStatus,
        records_written: int,
        error: str | None,
    ) -> None:
        await self._session.execute(
            sa.update(CollectorExecution)
            .where(CollectorExecution.id == execution_id)
            .values(
                finished_at=at,
                status=status,
                records_written=records_written,
                error=error[:_MAX_ERROR_LENGTH] if error else None,
            )
        )

    async def mark_interrupted(self, at: datetime) -> int:
        """Close executions left running by a process that stopped; returns how many."""
        statement = (
            sa.update(CollectorExecution)
            .where(CollectorExecution.status == CollectorStatus.RUNNING)
            .values(
                status=CollectorStatus.FAILED,
                finished_at=at,
                error="interrupted: the process stopped before the run finished",
            )
        )
        return (await self._session.execute(statement)).rowcount

    async def get(self, execution_id: uuid.UUID) -> CollectorExecution | None:
        return await self._session.get(CollectorExecution, execution_id, populate_existing=True)

    async def list(
        self, *, job_name: str | None = None, limit: int = 100
    ) -> Sequence[CollectorExecution]:
        statement = (
            sa.select(CollectorExecution)
            .order_by(CollectorExecution.started_at.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        if job_name is not None:
            statement = statement.where(CollectorExecution.job_name == job_name)
        return (await self._session.execute(statement)).scalars().all()
