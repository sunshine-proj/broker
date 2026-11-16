"""Job runner and scheduler against the test database.

The runner opens its own connections (one per run for the job lock), so these
tests commit for real. Every table they touch is emptied before and after
each test.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.collectors.runner import (
    JobAlreadyRunningError,
    JobDisabledError,
    JobNotFoundError,
    JobRunner,
)
from app.collectors.scheduler import CollectorScheduler
from app.config.settings import Settings
from app.database import create_session_factory
from app.models import CollectionJob, CollectorExecution, MarketObservation, ProviderHealth
from app.models.enums import CollectorStatus, ExecutionTrigger, ProviderHealthStatus
from app.providers.factory import ADAPTERS, ProviderFactory
from app.repositories.collection_jobs import SqlCollectionJobRepository
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository
from app.repositories.providers import SqlProviderRepository
from app.schemas.seed import SeedFile
from app.services.seed import SeedError, apply_seed
from tests.providers_support import ExampleProvider

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Monday 2026-11-16, 10:00 in New York: the session is open.
OPEN = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)
# Saturday 2026-11-14: closed.
CLOSED = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)
# 1794816000 is 2026-11-16T08:00:00Z.
QUOTE_TIME = 1794816000


SEED = {
    "exchanges": [
        {
            "code": "XTEST",
            "name": "Test Exchange",
            "timezone": "America/New_York",
            "trading_open": "09:30",
            "trading_close": "16:00",
        }
    ],
    "providers": [
        {
            "name": "example",
            "base_url": "https://api.example.test",
            "configuration": {"max_retries": 0},
        }
    ],
    "instruments": [
        {
            "symbol": "AAPL",
            "name": "Apple",
            "asset_type": "stock",
            "exchange": "XTEST",
            "currency": "USD",
            "providers": {"example": "AAPL.X"},
        },
        {
            "symbol": "EUR/USD",
            "name": "Euro / US Dollar",
            "asset_type": "forex",
            "currency": "USD",
            "providers": {"example": "EURUSD.X"},
        },
    ],
    "jobs": [
        {
            "name": "listed_quotes",
            "provider": "example",
            "kind": "quotes",
            "asset_types": ["stock"],
            "interval_minutes": 15,
            "market_hours_only": True,
        },
        {
            "name": "all_quotes",
            "provider": "example",
            "kind": "quotes",
            "daily_at": "16:00",
            "retry_minutes": 60,
        },
    ],
}


class FakeApi:
    """The example provider's API: answers every requested symbol, or fails."""

    def __init__(self) -> None:
        self.status = 200
        self.requested: list[list[str]] = []
        self.release: asyncio.Event | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        symbols = request.url.params["symbols"].split(",")
        self.requested.append(symbols)
        if self.release is not None:
            await self.release.wait()
        if self.status != 200:
            return httpx.Response(self.status)
        data = [{"symbol": s, "price": "100.5", "ts": QUOTE_TIME} for s in symbols]
        return httpx.Response(200, content=json.dumps({"data": data}))


@dataclass
class World:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    api: FakeApi
    providers: ProviderFactory
    clock: list[datetime]

    def runner(self) -> JobRunner:
        return JobRunner(
            engine=self.engine,
            session_factory=self.sessions,
            provider_factory=self.providers,
            clock=lambda: self.clock[0],
        )

    async def scalar(self, statement: sa.Executable) -> object:
        async with self.sessions() as session:
            return (await session.execute(statement)).scalar()

    async def executions(self) -> list[CollectorExecution]:
        async with self.sessions() as session:
            statement = sa.select(CollectorExecution).order_by(CollectorExecution.started_at)
            return list((await session.execute(statement)).scalars())

    async def job(self, name: str) -> CollectionJob:
        async with self.sessions() as session:
            job = await SqlCollectionJobRepository(session).get(name)
            assert job is not None
            return job


async def _apply(sessions: async_sessionmaker[AsyncSession], data: dict) -> None:
    async with sessions() as session:
        await apply_seed(
            SeedFile.model_validate(data),
            providers=SqlProviderRepository(session),
            instruments=SqlInstrumentRepository(session),
            economic_data=SqlEconomicDataRepository(session),
            jobs=SqlCollectionJobRepository(session),
        )
        await session.commit()


@pytest.fixture
async def world(
    migrated_database: Settings, empty_database: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[World]:
    # The example adapter stands in for a real provider, for the budget check too.
    monkeypatch.setitem(ADAPTERS, "example", ExampleProvider)
    engine = empty_database
    sessions = create_session_factory(engine)
    api = FakeApi()
    providers = ProviderFactory(
        migrated_database, http=httpx.AsyncClient(transport=httpx.MockTransport(api))
    )
    await _apply(sessions, SEED)
    try:
        yield World(engine, sessions, api, providers, [OPEN])
    finally:
        await providers.aclose()


# --- Running jobs ---------------------------------------------------------------------------


async def test_successful_run_is_recorded(world: World) -> None:
    result = await world.runner().run("all_quotes", ExecutionTrigger.SCHEDULED)

    assert result is not None
    assert (result.status, result.records_written, result.error) == (
        CollectorStatus.SUCCESS,
        2,
        None,
    )
    (execution,) = await world.executions()
    assert execution.id == result.execution_id
    assert (execution.job_name, execution.status, execution.trigger) == (
        "all_quotes",
        CollectorStatus.SUCCESS,
        ExecutionTrigger.SCHEDULED,
    )
    assert (execution.started_at, execution.finished_at) == (OPEN, OPEN)
    assert execution.records_written == 2
    job = await world.job("all_quotes")
    assert (job.last_status, job.last_started_at, job.last_finished_at, job.last_error) == (
        CollectorStatus.SUCCESS,
        OPEN,
        OPEN,
        None,
    )
    assert await world.scalar(sa.select(sa.func.count()).select_from(MarketObservation)) == 2


async def test_failed_run_is_recorded_with_its_error(world: World) -> None:
    world.api.status = 503

    result = await world.runner().run("all_quotes", ExecutionTrigger.SCHEDULED)

    assert result is not None and result.status is CollectorStatus.FAILED
    assert "ProviderUnavailableError: server error (HTTP 503)" in (result.error or "")
    (execution,) = await world.executions()
    assert execution.status is CollectorStatus.FAILED
    assert execution.error == result.error
    job = await world.job("all_quotes")
    assert (job.last_status, job.last_error) == (CollectorStatus.FAILED, result.error)
    health = await world.scalar(sa.select(ProviderHealth.status))
    assert health == ProviderHealthStatus.DEGRADED


async def test_job_filters_instruments_by_asset_type(world: World) -> None:
    await world.runner().run("listed_quotes", ExecutionTrigger.SCHEDULED)

    assert world.api.requested == [["AAPL.X"]]


async def test_market_hours_job_is_skipped_while_closed(world: World) -> None:
    world.clock[0] = CLOSED

    result = await world.runner().run("listed_quotes", ExecutionTrigger.SCHEDULED)

    assert result is None
    assert world.api.requested == []
    assert await world.executions() == []


async def test_manual_run_ignores_trading_hours(world: World) -> None:
    world.clock[0] = CLOSED

    result = await world.runner().run("listed_quotes", ExecutionTrigger.MANUAL)

    assert result is not None and result.status is CollectorStatus.SUCCESS
    (execution,) = await world.executions()
    assert execution.trigger is ExecutionTrigger.MANUAL


async def test_a_job_cannot_run_twice_at_once(world: World) -> None:
    runner = world.runner()
    first = await runner.prepare("all_quotes", ExecutionTrigger.SCHEDULED)
    assert first is not None

    with pytest.raises(JobAlreadyRunningError):
        await runner.prepare("all_quotes", ExecutionTrigger.MANUAL)
    # Another runner, as another process would have, is locked out too.
    with pytest.raises(JobAlreadyRunningError):
        await world.runner().prepare("all_quotes", ExecutionTrigger.MANUAL)
    # Other jobs are not affected.
    other = await runner.run("listed_quotes", ExecutionTrigger.SCHEDULED)
    assert other is not None

    await first.execute()
    again = await runner.run("all_quotes", ExecutionTrigger.MANUAL)
    assert again is not None and again.status is CollectorStatus.SUCCESS


async def test_the_lock_is_released_when_a_run_crashes(world: World) -> None:
    runner = world.runner()

    async def crash(*args: object) -> None:
        raise RuntimeError("bug in the collector")

    runner._collect = crash  # type: ignore[method-assign]
    result = await runner.run("all_quotes", ExecutionTrigger.SCHEDULED)

    assert result is not None and result.status is CollectorStatus.FAILED
    assert result.error == "RuntimeError: bug in the collector"
    assert await world.runner().run("all_quotes", ExecutionTrigger.SCHEDULED) is not None


async def test_unknown_and_disabled_jobs_do_not_run(world: World) -> None:
    async with world.sessions() as session:
        await session.execute(
            sa.update(CollectionJob).where(CollectionJob.name == "all_quotes").values(enabled=False)
        )
        await session.commit()

    with pytest.raises(JobNotFoundError):
        await world.runner().prepare("nope", ExecutionTrigger.MANUAL)
    with pytest.raises(JobDisabledError):
        await world.runner().prepare("all_quotes", ExecutionTrigger.MANUAL)


async def test_executions_left_running_are_closed_as_interrupted(world: World) -> None:
    async with world.sessions() as session:
        session.add(
            CollectorExecution(
                job_name="all_quotes",
                started_at=OPEN - timedelta(hours=1),
                status=CollectorStatus.RUNNING,
            )
        )
        await session.commit()

    assert await world.runner().mark_interrupted() == 1

    (execution,) = await world.executions()
    assert execution.status is CollectorStatus.FAILED
    assert (execution.error or "").startswith("interrupted")


# --- Seed --------------------------------------------------------------------------------------


async def test_seed_stores_jobs_and_disables_removed_ones(world: World) -> None:
    job = await world.job("listed_quotes")
    assert (job.kind, job.interval_minutes, job.market_hours_only) == ("quotes", 15, True)
    assert job.settings == {"asset_types": ["stock"]}
    daily = await world.job("all_quotes")
    assert (daily.daily_at, daily.retry_minutes) == (time(16, 0), 60)

    await _apply(world.sessions, {**SEED, "jobs": SEED["jobs"][1:]})

    assert (await world.job("listed_quotes")).enabled is False
    assert (await world.job("all_quotes")).enabled is True


async def test_seed_over_budget_is_refused_before_writing(world: World) -> None:
    over = {
        **SEED,
        "providers": [
            {
                **SEED["providers"][0],
                "configuration": {"rate_limits": [{"limit": 10, "period": "day"}]},
            }
        ],
        "instruments": [{**SEED["instruments"][0], "name": "Renamed"}, SEED["instruments"][1]],
    }

    with pytest.raises(SeedError, match="provider example would use"):
        await _apply(world.sessions, over)

    from app.models import Instrument

    assert (
        await world.scalar(sa.select(Instrument.name).where(Instrument.symbol == "AAPL")) == "Apple"
    )


# --- Scheduler --------------------------------------------------------------------------------


def scheduler(
    world: World, now: Callable[[], datetime] = lambda: datetime.now(UTC)
) -> CollectorScheduler:
    return CollectorScheduler(runner=world.runner(), session_factory=world.sessions, clock=now)


async def test_scheduler_schedules_enabled_jobs_and_records_next_runs(world: World) -> None:
    collector = scheduler(world)
    try:
        names = await collector.start()

        assert sorted(names) == ["all_quotes", "listed_quotes"]
        listed = await world.job("listed_quotes")
        assert listed.next_run_at is not None
        assert listed.next_run_at.minute % 15 == 0
        daily = await world.job("all_quotes")
        assert daily.next_run_at is not None
        assert (daily.next_run_at.hour, daily.next_run_at.minute) == (16, 0)
    finally:
        await collector.shutdown()


async def test_failed_scheduled_run_gets_one_retry(world: World) -> None:
    world.api.status = 503
    # Make the regular daily run far enough away for a retry to be worth it.
    later = (datetime.now(UTC) + timedelta(hours=12)).time().replace(second=0, microsecond=0)
    async with world.sessions() as session:
        await session.execute(
            sa.update(CollectionJob)
            .where(CollectionJob.name == "all_quotes")
            .values(daily_at=later)
        )
        await session.commit()
    collector = scheduler(world)
    try:
        await collector.start()

        await collector._run_scheduled("all_quotes", ExecutionTrigger.SCHEDULED)

        retry = collector._scheduler.get_job("all_quotes:retry")
        assert retry is not None
        expected = datetime.now(UTC) + timedelta(minutes=60)
        assert abs((retry.next_run_time - expected).total_seconds()) < 60

        # A failed retry is not retried again.
        collector._scheduler.remove_job("all_quotes:retry")
        await collector._run_scheduled("all_quotes", ExecutionTrigger.RETRY)
        assert collector._scheduler.get_job("all_quotes:retry") is None
    finally:
        await collector.shutdown()
    statuses = [(e.trigger, e.status) for e in await world.executions()]
    assert statuses == [
        (ExecutionTrigger.SCHEDULED, CollectorStatus.FAILED),
        (ExecutionTrigger.RETRY, CollectorStatus.FAILED),
    ]


async def test_one_failing_job_does_not_stop_another(world: World) -> None:
    collector = scheduler(world)
    try:
        await collector.start()
        world.api.status = 503
        await collector._run_scheduled("all_quotes", ExecutionTrigger.SCHEDULED)
        world.api.status = 200
        await collector._run_scheduled("listed_quotes", ExecutionTrigger.SCHEDULED)
    finally:
        await collector.shutdown()

    statuses = {e.job_name: e.status for e in await world.executions()}
    assert statuses == {
        "all_quotes": CollectorStatus.FAILED,
        "listed_quotes": CollectorStatus.SUCCESS,
    }


async def test_manual_trigger_runs_in_the_background(world: World) -> None:
    world.api.release = asyncio.Event()
    collector = scheduler(world)
    try:
        handle = await collector.trigger_manual("all_quotes")

        # Recorded as running before the work is done, and a second trigger is refused.
        (execution,) = await world.executions()
        assert (execution.id, execution.status) == (handle.execution_id, CollectorStatus.RUNNING)
        with pytest.raises(JobAlreadyRunningError):
            await collector.trigger_manual("all_quotes")

        world.api.release.set()
        for _ in range(100):
            (execution,) = await world.executions()
            if execution.status is not CollectorStatus.RUNNING:
                break
            await asyncio.sleep(0.05)
        assert execution.status is CollectorStatus.SUCCESS
    finally:
        await collector.shutdown()
