"""Runs one collection job: lock, record, collect, record.

A run has two steps, so that a manual trigger can answer before the work is done:

1. `prepare` takes the job's lock and records the execution as running.
2. `RunHandle.execute` does the collection, records the outcome and releases the lock.

The lock is a PostgreSQL advisory lock per job name, held on its own
connection for the whole run. It prevents two runs of the same job from
overlapping, whether they come from the scheduler, a manual trigger, the
command line or another process.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.models import CollectionJob
from app.models import Provider as ProviderRow
from app.models.enums import (
    AssetType,
    CandleInterval,
    CollectorStatus,
    ExecutionTrigger,
    JobKind,
    ObservationType,
)
from app.providers.factory import ProviderFactory
from app.repositories.collection_jobs import (
    SqlCollectionJobRepository,
    SqlCollectorExecutionRepository,
)
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository, TrackedSymbol
from app.repositories.market_data import SqlMarketDataRepository
from app.repositories.provider_health import SqlProviderHealthRepository
from app.services.budget import is_open_now
from app.services.collection import CollectionOutcome, CollectionService
from app.services.ingestion import IngestionService
from app.services.provider_health import ProviderHealthService

logger = logging.getLogger(__name__)

# First key of every job lock, so job locks cannot collide with other advisory locks.
_LOCK_NAMESPACE = 7_311_001


class JobNotFoundError(Exception):
    pass


class JobDisabledError(Exception):
    pass


class JobAlreadyRunningError(Exception):
    pass


@dataclass(frozen=True)
class RunResult:
    execution_id: uuid.UUID
    job: str
    status: CollectorStatus
    records_written: int
    error: str | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class JobRunner:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        provider_factory: ProviderFactory,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._engine = engine
        self._sessions = session_factory
        self._providers = provider_factory
        self._clock = clock

    async def prepare(self, job_name: str, trigger: ExecutionTrigger) -> "RunHandle | None":
        """Lock the job and record a running execution.

        Returns None, recording nothing, when a scheduled or retry run of a
        market-hours job finds every exchange closed. Raises JobNotFoundError,
        JobDisabledError or JobAlreadyRunningError when the run cannot start.
        """
        async with self._sessions() as session:
            job = await SqlCollectionJobRepository(session).get(job_name)
            if job is None:
                raise JobNotFoundError(job_name)
            if not job.enabled:
                raise JobDisabledError(job_name)
            provider = await session.get(ProviderRow, job.provider_id)
            assert provider is not None

        connection = await self._engine.connect()
        try:
            locked = await connection.scalar(
                sa.select(sa.func.pg_try_advisory_lock(_LOCK_NAMESPACE, sa.func.hashtext(job_name)))
            )
            if not locked:
                raise JobAlreadyRunningError(job_name)

            symbols = await self._symbols(job, trigger)
            if symbols is not None and not symbols:
                logger.debug("No exchange open; run skipped", extra={"job": job_name})
                await _unlock(connection, job_name)
                await connection.close()
                return None

            now = self._clock()
            async with self._sessions() as session:
                execution_id = await SqlCollectorExecutionRepository(session).start(
                    job_name=job_name, provider_id=provider.id, trigger=trigger, at=now
                )
                await SqlCollectionJobRepository(session).record_start(job_name, now)
                await session.commit()
        except BaseException:
            await connection.close()
            raise

        logger.info(
            "Collection job started",
            extra={"job": job_name, "trigger": trigger.value, "execution_id": str(execution_id)},
        )
        return RunHandle(self, connection, job, provider, symbols, execution_id, trigger)

    async def run(self, job_name: str, trigger: ExecutionTrigger) -> RunResult | None:
        """Prepare and execute in one step."""
        handle = await self.prepare(job_name, trigger)
        return None if handle is None else await handle.execute()

    async def mark_interrupted(self) -> int:
        """Close executions left running by a process that stopped."""
        async with self._sessions() as session:
            count = await SqlCollectorExecutionRepository(session).mark_interrupted(self._clock())
            await session.commit()
        if count:
            logger.warning("Interrupted executions closed", extra={"count": count})
        return count

    async def _symbols(self, job: CollectionJob, trigger: ExecutionTrigger) -> list[str] | None:
        """The symbols to collect, or None for all of the provider's (indicators jobs).

        Market-hours filtering applies to scheduled and retry runs only; a
        manual run collects every instrument of the job.
        """
        if job.kind is JobKind.INDICATORS:
            return None
        asset_types = job.settings.get("asset_types")
        async with self._sessions() as session:
            tracked = await SqlInstrumentRepository(session).list_tracked(
                job.provider_id,
                asset_types=[AssetType(t) for t in asset_types] if asset_types else None,
            )
        if job.market_hours_only and trigger is not ExecutionTrigger.MANUAL:
            now = self._clock()
            tracked = [t for t in tracked if _is_open(t, now)]
        return [t.provider_symbol for t in tracked]

    async def _collect(
        self, job: CollectionJob, provider: ProviderRow, symbols: list[str] | None
    ) -> CollectionOutcome:
        settings = job.settings
        end = self._clock()
        start = end - timedelta(days=int(settings.get("lookback_days", 0)))
        async with self._sessions() as session:
            instruments = SqlInstrumentRepository(session)
            economic_data = SqlEconomicDataRepository(session)
            service = CollectionService(
                factory=self._providers,
                instruments=instruments,
                economic_data=economic_data,
                ingestion=IngestionService(
                    instruments=instruments,
                    market_data=SqlMarketDataRepository(session),
                    economic_data=economic_data,
                    clock=self._clock,
                ),
                health=ProviderHealthService(
                    SqlProviderHealthRepository(session), clock=self._clock
                ),
            )
            if job.kind is JobKind.QUOTES:
                types = settings.get("observation_types")
                outcome = await service.collect_quotes(
                    provider,
                    {ObservationType(t) for t in types} if types else None,
                    symbols=symbols,
                )
            elif job.kind is JobKind.CANDLES:
                outcome = await service.collect_candles(
                    provider,
                    CandleInterval(settings["candle_interval"]),
                    start,
                    end,
                    symbols=symbols,
                )
            elif job.kind is JobKind.HISTORY:
                outcome = await service.collect_quote_history(provider, start, end, symbols=symbols)
            else:
                outcome = await service.collect_indicators(provider, start, end)
            # Committed even after a provider failure: the data that did arrive,
            # and the provider's health record, are kept.
            await session.commit()
        return outcome


class RunHandle:
    """A prepared run: the job is locked and its execution is recorded as running."""

    def __init__(
        self,
        runner: JobRunner,
        connection: AsyncConnection,
        job: CollectionJob,
        provider: ProviderRow,
        symbols: list[str] | None,
        execution_id: uuid.UUID,
        trigger: ExecutionTrigger,
    ) -> None:
        self._runner = runner
        self._connection = connection
        self.job = job
        self._provider = provider
        self._symbols = symbols
        self.execution_id = execution_id
        self.trigger = trigger

    async def execute(self) -> RunResult:
        """Collect, record the outcome, and release the job's lock."""
        runner = self._runner
        name = self.job.name
        try:
            try:
                outcome = await runner._collect(self.job, self._provider, self._symbols)
            except Exception as exc:
                # A bug, not a provider failure: record it and keep the scheduler alive.
                logger.exception("Collection job crashed", extra={"job": name})
                status, written = CollectorStatus.FAILED, 0
                error: str | None = f"{type(exc).__name__}: {exc}"
            else:
                written = outcome.result.written
                if outcome.succeeded:
                    status, error = CollectorStatus.SUCCESS, None
                else:
                    status = CollectorStatus.FAILED
                    error = _summarise(outcome)

            finished = runner._clock()
            async with runner._sessions() as session:
                await SqlCollectorExecutionRepository(session).finish(
                    self.execution_id,
                    at=finished,
                    status=status,
                    records_written=written,
                    error=error,
                )
                await SqlCollectionJobRepository(session).record_finish(
                    name, finished, status, error
                )
                await session.commit()
        finally:
            try:
                await _unlock(self._connection, name)
            finally:
                await self._connection.close()

        log = logger.info if status is CollectorStatus.SUCCESS else logger.warning
        log(
            "Collection job finished",
            extra={
                "job": name,
                "status": status.value,
                "records_written": written,
                "error": error,
                "execution_id": str(self.execution_id),
            },
        )
        return RunResult(self.execution_id, name, status, written, error)


def _is_open(symbol: TrackedSymbol, at: datetime) -> bool:
    if symbol.exchange_timezone is None:
        return True
    return is_open_now(symbol.exchange_timezone, symbol.trading_open, symbol.trading_close, at)


def _summarise(outcome: CollectionOutcome) -> str:
    """One line describing a run with provider failures."""
    failed = len(outcome.errors)
    distinct = list(dict.fromkeys(outcome.errors))
    detail = "; ".join(distinct[:3]) + ("; ..." if len(distinct) > 3 else "")
    return f"{failed} of {max(outcome.requested, failed)} provider calls failed: {detail}"


async def _unlock(connection: AsyncConnection, job_name: str) -> None:
    await connection.scalar(
        sa.select(sa.func.pg_advisory_unlock(_LOCK_NAMESPACE, sa.func.hashtext(job_name)))
    )
